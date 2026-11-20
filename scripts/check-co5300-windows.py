#!/usr/bin/env python3
"""Compile the actual landscape encoder with a SPI capture stub.
Host-only test: checks descriptors, windows, rotation and RGB565 bytes.
It does not emulate the panel or establish on-board performance.
"""
import argparse
import os
from pathlib import Path
import re
import subprocess
import tempfile

parser = argparse.ArgumentParser()
parser.add_argument("source", type=Path)
args = parser.parse_args()
source = args.source.read_text()
masked = re.sub(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"',
                lambda m: " " * len(m.group()), source, flags=re.S)


def block(marker):
    start = source.index(marker)
    opening = masked.index("{", start)
    depth = 0
    for end in range(opening, len(masked)):
        depth += (masked[end] == "{") - (masked[end] == "}")
        if depth == 0:
            return source[start:end + 1] + (";" if marker.startswith("struct ") else "")
    raise ValueError("Unclosed C block")


header = r"""
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <stdarg.h>
#include <sys/types.h>
typedef uint8_t u8;
typedef uint16_t u16;
typedef uint32_t u32;
typedef uint64_t u64;
typedef int spinlock_t;
struct fb_deferred_io { unsigned long delay; };
struct gpio_desc;
struct mutex { int unused; };
struct device { int unused; };
struct spi_controller { u32 min_speed_hz, max_speed_hz; };
struct spi_device {
    struct device dev;
    struct spi_controller *controller;
    void *data;
    u32 max_speed_hz;
};
struct device_attribute { int unused; };
#define to_spi_device(dev) ((struct spi_device *)(dev))
static void *spi_get_drvdata(struct spi_device *spi) { return spi->data; }
static int sysfs_emit(char *buf, const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(buf, 4096, fmt, ap);
    va_end(ap);
    return n;
}
static int kstrtou32(const char *buf, int base, u32 *value) {
    char *end;
    if (*buf == '-') return -EINVAL;
    errno = 0;
    unsigned long long n = strtoull(buf, &end, base);
    if (errno || end == buf || n > UINT32_MAX ||
        (*end && strcmp(end, "\n"))) return -EINVAL;
    *value = n;
    return 0;
}
struct fb_info {
    void *par;
    char *screen_buffer;
    struct { u32 line_length, smem_len; } fix;
};
struct fb_deferred_io_pageref {
    unsigned long offset;
    struct fb_deferred_io_pageref *next;
};
struct list_head { struct fb_deferred_io_pageref *first; };
struct spi_transfer {
    const void *tx_buf;
    unsigned int len;
    unsigned char tx_nbits;
    bool cs_change;
};
#define READ_ONCE(x) (x)
#define WRITE_ONCE(x, v) ((x) = (v))
#define ARRAY_SIZE(x) (sizeof(x) / sizeof((x)[0]))
#define SPI_NBITS_SINGLE 1
#define SPI_NBITS_QUAD 4
#define dev_info(...) ((void)0)
#define dev_err_ratelimited(...) ((void)0)
#define spin_lock_irqsave(lock, flags) ((void)(lock), (flags) = 0)
#define spin_unlock_irqrestore(lock, flags) ((void)(lock), (void)(flags))
#define list_empty(head) (!(head)->first)
#define list_for_each_entry(ref, head, member) \
    for (ref = (head)->first; ref; ref = ref->next)
#define cond_resched() ((void)0)
#define min(a, b) ((a) < (b) ? (a) : (b))
#define max(a, b) ((a) > (b) ? (a) : (b))
#define min_t(type, a, b) min((type)(a), (type)(b))
#define HZ 100
#define PAGE_SIZE 4096
static u64 ktime_get_ns(void) { static u64 now; return now += 1000; }
static u64 div_u64(u64 n, u32 d) { return n / d; }
static void mutex_lock(struct mutex *m) { (void)m; }
static void mutex_unlock(struct mutex *m) { (void)m; }

static unsigned int calls, phase, first_want, last_want, width, height;
static unsigned int rotation;
static unsigned int xoff, yoff, injected, retries, retry_first, retry_last;
static unsigned int refreshes;
static bool partial_updates;
static bool render_diagnostics = true;
static bool resuming;
static int reset_raw = 1, resume_fail;
static unsigned resume_calls, sleep_ms;
static int gpiod_get_raw_value_cansleep(struct gpio_desc *reset) {
    (void)reset; return reset_raw;
}
static void msleep(unsigned ms) { sleep_ms += ms; }
static void fail(const char *message) {
    fprintf(stderr, "FAIL: %s\n", message);
    exit(2);
}
static u16 pattern(unsigned int y, unsigned int x) {
    return (u16)((y * 101) ^ (x * 31) ^ 0xf800);
}
static int spi_sync_transfer(struct spi_device *dev, struct spi_transfer *t,
                             unsigned int n) {
    (void)dev;
    const u8 *b = t[0].tx_buf;
    if (resuming) {
        static const u8 commands[] = { 0x11, 0x53, 0x51, 0x63, 0x29 };
        unsigned i = resume_calls++;
        if (n != 1 || i >= 5 || t[0].len != (i == 0 || i == 4 ? 4 : 5) ||
            b[0] != 2 || b[1] || b[2] != commands[i] || b[3] ||
            (i > 0 && i < 4 && b[4] != (i == 1 ? 0x20 : 0xff)))
            fail("panel resume command order/brightness/single-lane header");
        return resume_fail == (int)i + 1 ? -EIO : 0;
    }
    if (t[0].tx_nbits != 1 || t[0].cs_change)
        fail("header lane width / CS boundary");
    calls++;
    if (injected && calls == injected)
        return -EIO;
    if (phase < 2) {
        if (n != 1 || t[0].len != 8 || b[0] != 2 ||
            b[1] || b[3] || b[2] != (phase ? 0x2b : 0x2a))
            fail("CASET/RASET frame");
        u32 start = ((u32)b[4] << 8) | b[5];
        u32 end = ((u32)b[6] << 8) | b[7];
        if ((start & 1) || end < start || ((end - start + 1) & 1))
            fail("CO5300 requires even start and extent");
        if (!phase) {
            u32 lo = rotation == 90 || rotation == 270 ? 0 :
                     rotation == 180 ? first_want : height - 1 - last_want;
            u32 hi = rotation == 90 || rotation == 270 ? width - 1 :
                     rotation == 180 ? last_want : height - 1 - first_want;
            if (start != yoff + lo || end != yoff + hi)
                fail("mirrored partial CASET window");
        } else {
            u32 lo = rotation == 90 ? first_want :
                     rotation == 270 ? height - 1 - last_want : 0;
            u32 hi = rotation == 90 ? last_want :
                     rotation == 270 ? height - 1 - first_want : width - 1;
            if (start != xoff + lo || end != xoff + hi)
                fail("native RASET window");
        }
        phase++;
        return 0;
    }
    static const u8 ramwr[] = { 0x32, 0, 0x2c, 0 };
    u32 rows = last_want - first_want + 1;
    if (n != 2 || t[0].len != 4 || memcmp(b, ramwr, 4) ||
        t[1].tx_nbits != 4 || t[1].cs_change || t[1].len != width * rows * 2)
        fail("one single header + quad RGB565 payload in one CS assertion");
    b = t[1].tx_buf;
    /* Independent inverse mapping from GRAM row/column to logical pixels. */
    bool portrait = rotation == 90 || rotation == 270;
    u32 columns = portrait ? width : rows;
    u32 gram_rows = portrait ? rows : width;
    for (u32 r = 0; r < gram_rows; r++) {
        for (u32 c = 0; c < columns; c++) {
            u32 x, y;
            switch (rotation) {
            case 90: x = c; y = first_want + r; break;
            case 180: x = width - 1 - r; y = first_want + c; break;
            case 270: x = width - 1 - c; y = last_want - r; break;
            default: x = r; y = last_want - c; break;
            }
            u16 expected = pattern(y, x);
            u32 off = (r * columns + c) * 2;
            if (b[off] != (expected >> 8) || b[off+1] != (expected & 255))
                fail("clockwise rotation / RGB565 wire bytes");
        }
    }
    phase++;
    return 0;
}
"""
stub = r"""
static void co5300fb_damage(struct fb_info *info, u32 first, u32 last,
                            unsigned long delay) {
    struct co5300fb *p = info->par;
    if (delay == p->defio.delay) {
        if (first || last != p->height - 1) fail("rotation must redraw full frame");
        refreshes++;
        return;
    }
    if (delay != HZ) fail("bounded retry delay");
    retries++;
    retry_first = first;
    retry_last = last;
}
"""
main = r"""
static void settings(void) {
    struct spi_controller ctlr = { .min_speed_hz = 78125, .max_speed_hz = 80000000 };
    struct spi_device spi = { .controller = &ctlr, .max_speed_hz = 40000000 };
    struct co5300fb p = { .spi = &spi, .width = 502, .height = 410,
                         .defio = { .delay = 4 } };
    struct fb_info info = { .par = &p };
    spi.data = &info;
    struct device *dev = &spi.dev;
    char buf[4096];
    const char *valid[] = { "78125", "20000000\n", "80000000" };
    for (u32 i = 0; i < ARRAY_SIZE(valid); i++) {
        size_t len = strlen(valid[i]);
        if (spi_freq_hz_store(dev, NULL, valid[i], len) != (ssize_t)len)
            fail("valid clock / controller bounds");
    }
    if (spi_freq_hz_show(dev, NULL, buf) <= 0 || strcmp(buf, "80000000\n"))
        fail("clock readback");
    const char *invalid[] = { "0", "78124", "80000001", "-1", "4294967296",
                             "banana", "", "40000000 garbage" };
    for (u32 i = 0; i < ARRAY_SIZE(invalid); i++)
        if (spi_freq_hz_store(dev, NULL, invalid[i], strlen(invalid[i])) != -EINVAL ||
            spi.max_speed_hz != 80000000)
            fail("invalid clock must preserve old setting");
    refreshes = 0;
    if (rotation_store(dev, NULL, "180\n", 4) != 4 || p.rotation != 180 || refreshes != 1)
        fail("landscape flip and full refresh");
    if (rotation_show(dev, NULL, buf) <= 0 || strcmp(buf, "180\n"))
        fail("rotation readback");
    if (rotation_store(dev, NULL, "180", 3) != 3 || refreshes != 1)
        fail("unchanged rotation should not flush");
    const char *badrot[] = { "90", "270", "45", "360", "-180", "abc", "4294967296" };
    for (u32 i = 0; i < ARRAY_SIZE(badrot); i++)
        if (rotation_store(dev, NULL, badrot[i], strlen(badrot[i])) != -EINVAL ||
            p.rotation != 180 || refreshes != 1)
            fail("invalid/dimension-changing rotation");
    if (rotation_store(dev, NULL, "0", 1) != 1 || p.rotation || refreshes != 2)
        fail("restore landscape");
    p.rotation = 90; p.width = 410; p.height = 502;
    if (rotation_store(dev, NULL, "270", 3) != 3 || p.rotation != 270 || refreshes != 3 ||
        rotation_store(dev, NULL, "0", 1) != -EINVAL ||
        rotation_store(dev, NULL, "180", 3) != -EINVAL ||
        rotation_store(dev, NULL, "90", 2) != 2 || p.rotation != 90 || refreshes != 4)
        fail("portrait flip must preserve dimensions");
    p.command = calloc(259, 1);
    if (!p.command) fail("command allocation");
    resuming = true;
    unsigned before = refreshes;
    if (panel_resume_store(dev, NULL, "1", 1) != 1 || resume_calls != 5 ||
        sleep_ms != 120 || refreshes != before + 1 || p.rotation != 90)
        fail("explicit panel resume and one redraw");
    for (resume_fail = 1; resume_fail <= 5; resume_fail++) {
        resume_calls = sleep_ms = 0; before = refreshes;
        if (panel_resume_store(dev, NULL, "1", 1) != -EIO ||
            resume_calls != (unsigned)resume_fail || refreshes != before)
            fail("resume failures must propagate without a success redraw");
    }
    resume_fail = 0; resume_calls = sleep_ms = 0;
    reset_raw = 0;
    if (panel_resume_store(dev, NULL, "1", 1) != -EHOSTDOWN || resume_calls)
        fail("held reset must not send panel commands");
    reset_raw = -EIO;
    if (panel_resume_store(dev, NULL, "1", 1) != -EIO || resume_calls)
        fail("reset read errors must propagate");
    reset_raw = 1;
    if (panel_resume_store(dev, NULL, "0", 1) != -EINVAL ||
        panel_resume_store(dev, NULL, "bad", 3) != -EINVAL || resume_calls)
        fail("invalid resume request must not send commands");
    resuming = false; free(p.command);
}
static void capture(u32 first, u32 last) {
    calls = phase = retries = 0;
    first_want = first;
    last_want = last;
}
static void run(struct fb_info *info, struct list_head *pages, u32 first, u32 last) {
    struct co5300fb *p = info->par;
    p->dirty = true;
    p->first = first;
    p->last = last;
    capture(first & ~1U, (last | 1U) < height ? (last | 1U) : height - 1);
    co5300fb_deferred_io(info, pages);
    if (calls != 3 || phase != 3 || retries)
        fail("one window / two commands / one pixel message");
}
static void geometry(u32 w, u32 h, u32 rot) {
    struct spi_device spi = {0};
    struct co5300fb p = { .spi = &spi, .width = w, .height = h,
                         .xoff = 0, .yoff = 22, .rotation = rot };
    width = w; height = h; xoff = p.xoff; yoff = p.yoff;
    rotation = rot;
    p.command = calloc(259, 1);
    p.pixels = malloc(w * h * 2);
    u16 *vmem = malloc(w * h * sizeof(*vmem));
    if (!p.command || !p.pixels || !vmem) fail("allocation");
    for (u32 y = 0; y < h; y++)
        for (u32 x = 0; x < w; x++)
            vmem[y*w+x] = pattern(y, x);
    struct fb_info info = { .par = &p, .screen_buffer = (char *)vmem,
                           .fix = { .line_length = w*2, .smem_len = w*h*2 } };
    struct list_head pages = {0};
    partial_updates = false;
    p.dirty = true; p.first = p.last = h / 2;
    capture(0, h - 1);
    co5300fb_deferred_io(&info, &pages);
    if (phase != 3 || p.stats.full_flushes != 1 || p.stats.bytes != w * h * 2)
        fail("default dirty-row update must refresh the full DMA frame");
    if (p.render_stats.samples != 1 || p.render_stats.bytes != w * h * 2 ||
        !p.render_stats.nonzero_bytes || p.render_stats.sample_valid != (w > 265 && h > 240))
        fail("pre-SPI diagnostic range/count/sample validity");
    if (p.render_stats.sample_valid &&
        (p.render_stats.fb_sample != pattern(240, 265) ||
         p.render_stats.tx_sample != pattern(240, 265) ||
         p.render_stats.sample_offset + 1 >= p.render_stats.bytes))
        fail("belly diagnostic must match actual rendered RGB565 at every rotation");
    p.stats = (struct co5300fb_stats) {0};
    partial_updates = true;
    run(&info, &pages, 0, h-1);
    if (p.stats.full_flushes != 1 || p.stats.bytes != w*h*2 ||
        p.stats.full_total_us != 2 || p.stats.errors)
        fail("full-frame stats");
    for (u32 y = 0; y < h; y++)
        run(&info, &pages, y, y);
    for (u32 y = 1; y < h; y += 2)
        run(&info, &pages, y-1, y);
    if (h == 410) {
        run(&info, &pages, 0, 399);
        run(&info, &pages, 400, 409);
        struct fb_deferred_io_pageref tail = { .offset = w*h*2 - (w*h*2)%PAGE_SIZE };
        struct fb_deferred_io_pageref middle = { .offset = 10*PAGE_SIZE, .next = &tail };
        pages.first = &middle; /* Unsorted offsets plus fbcon damage. */
        p.dirty = true; p.first = 11; p.last = 11;
        capture(10, 409);
        co5300fb_deferred_io(&info, &pages);
        if (phase != 3) fail("union of mmap and fbcon damage");
        pages.first = &tail;
        p.dirty = false;
        u32 start = ((u32)tail.offset / (w*2)) & ~1U;
        capture(start, 409);
        co5300fb_deferred_io(&info, &pages);
        if (phase != 3) fail("last mmap page padding / clipping");
        pages.first = NULL;
    }
    for (u32 i = 1; i <= 3; i++) {
        injected = i;
        p.dirty = true; p.first = 11 < h ? 11 : 1; p.last = p.first;
        capture(p.first & ~1U, p.last | 1U);
        co5300fb_deferred_io(&info, &pages);
        if (calls != i || retries != 1 ||
            retry_first != first_want || retry_last != last_want ||
            p.stats.errors != i)
            fail("transfer failure must retry the complete window");
    }
    injected = 0;
    capture(0, 0); p.dirty = false;
    co5300fb_deferred_io(&info, &pages);
    if (calls) fail("idle callback should not flush");
    p.stopped = true; p.dirty = true;
    co5300fb_deferred_io(&info, &pages);
    if (calls) fail("stopped callback should not flush");
    free(p.command); free(p.pixels); free(vmem);
}
int main(void) {
    settings();
    for (u32 rot = 0; rot <= 270; rot += 90) {
        geometry(rot % 180 ? 410 : 502, rot % 180 ? 502 : 410, rot);
        geometry(2, 2, rot);
        geometry(512, 512, rot);
    }
    puts("PASS: default full refresh and opt-in partial/mmap windows at all rotations, RGB565 rotation,"
         " mixed-lane CS, tile edges, retry, render diagnostics and panel resume/error/reset checks");
    return 0;
}
"""
program = "\n".join([
    header, block("struct co5300fb_stats {"), block("struct co5300fb_render_stats {"),
    block("struct co5300fb {"), stub,
    block("static int co5300fb_command("),
    block("static void co5300fb_render("),
    block("static void co5300fb_measure_render("),
    block("static int co5300fb_flush("),
    block("static ssize_t spi_freq_hz_show("),
    block("static ssize_t spi_freq_hz_store("),
    block("static ssize_t rotation_show("),
    block("static ssize_t rotation_store("),
    block("static ssize_t panel_resume_store("),
    block("static void co5300fb_deferred_io("), main,
])
with tempfile.TemporaryDirectory(prefix="co5300-windows-") as temp:
    test = Path(temp) / "test.c"
    exe = Path(temp) / "test"
    test.write_text(program)
    subprocess.run([os.environ.get("CC", "cc"), "-std=gnu11", "-O1", "-g",
                    "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                    str(test), "-o", str(exe)], check=True)
    env = dict(os.environ, ASAN_OPTIONS="detect_leaks=0")
    raise SystemExit(subprocess.run([str(exe)], env=env).returncode)
