#!/usr/bin/env python3
"""Host-test actual Linux sysmem helpers -> CO5300 damage -> captured SPI.

Usage: python3 scripts/check-co5300-fb-io.py PATH_TO_LINUX_SOURCE
Requires the Linux 7.3.4 source used by this port. No real device is opened.
"""
import argparse
import os
from pathlib import Path
import re
import subprocess
import tempfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("kernel", type=Path)
args = parser.parse_args()
repo = Path(__file__).resolve().parent.parent
driver = (repo / "linux/drivers/video/fbdev/co5300fb.c").read_text()
core = args.kernel.resolve() / "drivers/video/fbdev/core"


def block(source, marker):
    masked = re.sub(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"',
                    lambda m: " " * len(m[0]), source, flags=re.S)
    if marker.startswith("struct "):
        start = source.index(marker)
        opening = masked.index("{", start)
    else:
        match = re.search(r"^[^\n;{}]*\b" + re.escape(marker) +
                          r"\([^;{}]*\)\s*\{", masked, flags=re.M)
        if not match:
            raise ValueError(f"Function definition not found: {marker}")
        start, opening = match.start(), match.end() - 1
    depth = 0
    for end in range(opening, len(masked)):
        depth += (masked[end] == "{") - (masked[end] == "}")
        if not depth:
            return source[start:end + 1] + (";" if marker.startswith("struct ") else "")
    raise ValueError(f"Unclosed block: {marker}")


header = r'''
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <sys/types.h>
#include <limits.h>
typedef uint8_t u8;
typedef uint16_t u16;
typedef uint32_t u32;
typedef uint64_t u64;
typedef int spinlock_t;
struct mutex { bool locked; };
struct gpio_desc;
struct device { int unused; };
struct spi_device { struct device dev; void *data; };
struct device_attribute;
#define to_spi_device(dev) ((struct spi_device *)(dev))
static void *spi_get_drvdata(struct spi_device *spi) { return spi->data; }
static int kstrtou32(const char *buf, int base, u32 *value) {
    char *end;
    if (*buf == '-') return -EINVAL;
    errno = 0;
    unsigned long n = strtoul(buf, &end, base);
    if (errno || end == buf || n > UINT32_MAX || (*end && strcmp(end, "\n"))) return -EINVAL;
    *value = n; return 0;
}
struct fb_info;
struct vm_area_struct;
struct fb_fillrect { u32 dx, dy, width, height, color, rop; };
struct fb_copyarea { u32 dx, dy, width, height, sx, sy; };
struct fb_image { u32 dx, dy, width, height, fg_color, bg_color, depth; const char *data; };
struct fb_bitfield { u32 offset, length, msb_right; };
struct fb_var_screeninfo {
    u32 xres, yres, xres_virtual, yres_virtual, xoffset, yoffset, bits_per_pixel, nonstd;
    struct fb_bitfield red, green, blue, transp;
};
struct fb_ops {
    void *owner;
    ssize_t (*fb_read)(struct fb_info *, char *, size_t, loff_t *);
    ssize_t (*fb_write)(struct fb_info *, const char *, size_t, loff_t *);
    void (*fb_fillrect)(struct fb_info *, const struct fb_fillrect *);
    void (*fb_copyarea)(struct fb_info *, const struct fb_copyarea *);
    void (*fb_imageblit)(struct fb_info *, const struct fb_image *);
    int (*fb_mmap)(struct fb_info *, struct vm_area_struct *);
    int (*fb_sync)(struct fb_info *);
    int (*fb_setcolreg)(unsigned, unsigned, unsigned, unsigned, unsigned, struct fb_info *);
    int (*fb_check_var)(struct fb_var_screeninfo *, struct fb_info *);
    void (*fb_destroy)(struct fb_info *);
};
struct fb_deferred_io { unsigned long delay; };
struct fb_deferred_io_pageref { unsigned long offset; struct fb_deferred_io_pageref *next; };
struct list_head { struct fb_deferred_io_pageref *first; };
struct fb_info {
    void *par;
    union { char *screen_buffer; char *screen_base; };
    unsigned long screen_size;
    unsigned flags;
    struct { u32 line_length, smem_len, visual; } fix;
    struct fb_var_screeninfo var;
    const struct fb_ops *fbops;
    void *pseudo_palette;
    struct fb_deferred_io *fbdefio;
    struct { bool pending; } deferred_work;
};
struct spi_transfer { const void *tx_buf; u32 len; u8 tx_nbits; };
#if __SIZEOF_LONG__ == 8
#define BITS_PER_LONG 64
#else
#define BITS_PER_LONG 32
#endif
#define BITS_PER_BYTE 8
#define BITS_TO_BYTES(x) (((x) + 7) / 8)
#define BYTES_TO_BITS(x) ((x) * 8)
#define PTR_ALIGN_DOWN(p, a) ((void *)((uintptr_t)(p) & ~((uintptr_t)(a) - 1)))
#define __LITTLE_ENDIAN 1234
#define FBINFO_VIRTFB 4
#define FB_VISUAL_TRUECOLOR 2
#define FB_VISUAL_DIRECTCOLOR 4
#define ROP_COPY 0
#define ROP_XOR 1
#define SPI_NBITS_SINGLE 1
#define SPI_NBITS_QUAD 4
#define swab64(x) __builtin_bswap64(x)
#define swab32(x) __builtin_bswap32(x)
#define __user
#define THIS_MODULE NULL
#define fb_warn_once(...) ((void)0)
#define dev_info(...) ((void)0)
#define dev_err_ratelimited(...) ((void)0)
#define READ_ONCE(x) (x)
#define ARRAY_SIZE(x) (sizeof(x) / sizeof((x)[0]))
#define min(a,b) ((a) < (b) ? (a) : (b))
#define max(a,b) ((a) > (b) ? (a) : (b))
#define min_t(t,a,b) min((t)(a),(t)(b))
#define spin_lock_irqsave(lock,flags) ((void)(lock), (flags) = 0)
#define spin_unlock_irqrestore(lock,flags) ((void)(lock), (void)(flags))
#define cond_resched() ((void)0)
#define list_empty(head) (!(head)->first)
#define list_for_each_entry(ref,head,member) for(ref=(head)->first;ref;ref=ref->next)
#define HZ 100
#define PAGE_SIZE 4096
static bool partial_updates;
static bool render_diagnostics = true;
static void fail(const char *s);
static struct mutex *locked_mutex;
static unsigned lock_calls, scheduled;
static void mutex_lock(struct mutex *m) {
    if (m->locked || locked_mutex) fail("recursive io_lock");
    m->locked = true; locked_mutex = m; lock_calls++;
}
static void mutex_unlock(struct mutex *m) {
    if (!m->locked || locked_mutex != m) fail("unbalanced io_lock");
    m->locked = false; locked_mutex = NULL;
}
static bool recovering;
static unsigned init_phase, reset_calls, reset_us, sleep_ms;
static int reset_raw;
static int gpiod_direction_output_raw(struct gpio_desc *reset, int value) {
    (void)reset;
    if (!locked_mutex) fail("reset must hold io_lock");
    reset_calls++; reset_raw = value; return 0;
}
static void gpiod_set_raw_value_cansleep(struct gpio_desc *reset, int value) {
    (void)reset;
    if (!locked_mutex) fail("reset release must hold io_lock");
    reset_raw = value;
}
static int gpiod_get_raw_value_cansleep(struct gpio_desc *reset) { (void)reset; return reset_raw; }
static void usleep_range(unsigned low, unsigned high) { (void)high; reset_us += low; }
static void msleep(unsigned ms) { sleep_ms += ms; }
#define GFP_KERNEL 0
static bool device_property_present(struct device *dev, const char *name) {
    (void)dev; (void)name; return false;
}
static int device_property_count_u8(struct device *dev, const char *name) {
    (void)dev; (void)name; return -EINVAL;
}
static void *devm_kmalloc(struct device *dev, size_t size, int flags) {
    (void)dev; (void)size; (void)flags; return NULL;
}
static int device_property_read_u8_array(struct device *dev, const char *name, u8 *out, size_t size) {
    (void)dev; (void)name; (void)out; (void)size; return -EINVAL;
}
static u64 ktime_get_ns(void) { static u64 n; return n += 1000; }
static u64 div_u64(u64 n, u32 d) { return n / d; }
static bool fb_be_math(struct fb_info *info) { (void)info; return false; }
static unsigned long copy_from_user(void *to, const void *from, size_t count) {
    memcpy(to, from, count); return 0;
}
static unsigned long copy_to_user(void *to, const void *from, size_t count) {
    memcpy(to, from, count); return 0;
}
static bool schedule_delayed_work(void *work, unsigned long delay) {
    (void)delay;
    scheduled++;
    bool *pending = work;
    bool old = *pending;
    *pending = true;
    return !old;
}
static int fb_deferred_io_mmap(struct fb_info *info, struct vm_area_struct *vma) {
    (void)info; (void)vma; return 0;
}
static void vfree(void *p) { free(p); }
static void kfree(void *p) { free(p); }
static void framebuffer_release(struct fb_info *info) { (void)info; }
static struct fb_info *active;
static unsigned phase;
static void fail(const char *s) { fprintf(stderr, "FAIL: %s\n", s); exit(2); }
static int spi_sync_transfer(struct spi_device *spi, struct spi_transfer *t, unsigned n) {
    (void)spi;
    if (!locked_mutex) fail("SPI must hold io_lock");
    const u8 *b = t[0].tx_buf;
    if (recovering && init_phase < 10) {
        static const u8 commands[] = { 0x36, 0xfe, 0xc4, 0x3a, 0x34, 0x53, 0x51, 0x63, 0x11, 0x29 };
        static const u8 values[] = { 0, 0, 0x80, 0x55, 0, 0x20, 0xff, 0xff };
        unsigned i = init_phase++;
        if (n != 1 || t[0].len != (i < 8 && i != 4 ? 5 : 4) ||
            t[0].tx_nbits != 1 || b[0] != 2 || b[1] || b[2] != commands[i] || b[3] ||
            (i < 8 && i != 4 && b[4] != values[i])) fail("RGB/native axes and TE-off init before Tux RAMWR");
        return 0;
    }
    if (phase < 2) {
        if (n != 1 || t[0].len != 8 || b[2] != (phase ? 0x2b : 0x2a)) fail("window command");
        unsigned first = ((unsigned)b[4] << 8) | b[5];
        unsigned last = ((unsigned)b[6] << 8) | b[7];
        if (first != (phase ? 0 : 22) || last != (phase ? 501 : 431)) fail("default full GRAM rectangle");
    } else {
        if (n != 2 || t[1].len != 411640 || t[1].tx_nbits != 4) fail("full DMA payload");
        const u8 *pixels = t[1].tx_buf;
        const u16 *fb = (const u16 *)active->screen_buffer;
        for (unsigned x = 0; x < 502; x++)
            for (unsigned i = 0; i < 410; i++) {
                u16 want = fb[(409 - i) * 502 + x];
                unsigned off = (x * 410 + i) * 2;
                if (pixels[off] != (want >> 8) || pixels[off + 1] != (want & 255)) fail("render must read the same sysmem pixels that were written");
            }
    }
    phase++;
    return 0;
}
'''

main = r'''
static void dirty(struct fb_info *info, u32 first, u32 last) {
    struct co5300fb *p = info->par;
    if (!p->dirty || !info->deferred_work.pending || p->first != first || p->last != last)
        fail("actual helper must mark the written byte/glyph range");
}
static void drain(struct fb_info *info, struct list_head *pages) {
    struct co5300fb *p = info->par;
    u64 before = p->stats.full_flushes;
    phase = 0;
    info->deferred_work.pending = false;
    co5300fb_deferred_io(info, pages);
    if (phase != 3 || p->stats.errors || p->stats.full_flushes != before + 1)
        fail("dirty bytes/glyphs must reach one successful full SPI frame");
}
static void drain_held(struct fb_info *info, struct list_head *pages) {
    struct co5300fb *p = info->par;
    u64 before = p->stats.flushes;
    phase = 0;
    info->deferred_work.pending = false;
    co5300fb_deferred_io(info, pages);
    if (phase || p->stats.flushes != before || p->dirty)
        fail("held clear/row-write callbacks must not submit intermediate pixels");
}
int main(int argc, char **argv) {
    if (argc != 2) fail("Tux argument");
    struct spi_device spi = {0};
    struct co5300fb p = { .spi = &spi, .width = 502, .height = 410, .yoff = 22,
                         .defio = { .delay = 4 } };
    struct fb_info info = { .par = &p, .flags = FBINFO_VIRTFB,
        .fix = { .line_length = 1004, .smem_len = 411640, .visual = FB_VISUAL_TRUECOLOR },
        .screen_size = 411640, .var = { .xres = 502, .yres = 410, .bits_per_pixel = 16 },
        .pseudo_palette = p.palette, .fbops = &co5300fb_ops, .fbdefio = &p.defio };
    spi.data = &info;
    info.screen_buffer = calloc(1, 411648);
    p.pixels = calloc(1, 411640); p.command = calloc(1, 259); active = &info;
    if (!info.screen_buffer || !p.pixels || !p.command) fail("allocation");
    struct list_head pages = {0};
    char *white = malloc(411640), *readback = malloc(411640);
    char *tux = malloc(110400);
    if (!white || !readback || !tux) fail("allocation");
    memset(white, 255, 411640);
    loff_t pos = 241490;
    if (info.fbops->fb_write(&info, white, 8, &pos) != 8 || pos != 241498)
        fail("eight-byte seek/write offset");
    dirty(&info, 240, 240);
    pos = 241490;
    if (info.fbops->fb_read(&info, readback, 8, &pos) != 8 || memcmp(readback, white, 8)) fail("eight-byte readback");
    drain(&info, &pages);
    pos = 0;
    if (info.fbops->fb_write(&info, white, 411640, &pos) != 411640 || pos != 411640) fail("full-screen write");
    dirty(&info, 0, 409);
    pos = 0;
    if (info.fbops->fb_read(&info, readback, 411640, &pos) != 411640 || memcmp(readback, white, 411640)) fail("full-screen readback");
    drain(&info, &pages);
    if (update_hold_store(&spi.dev, NULL, "1", 1) != 1) fail("hold login assembly");
    struct fb_fillrect clear = { .width = 502, .height = 410, .color = 0 };
    info.fbops->fb_fillrect(&info, &clear); dirty(&info, 0, 409); drain_held(&info, &pages);
    for (unsigned i = 0; i < 411640; i++) if (info.screen_buffer[i]) fail("clear must write black to the shared backing buffer");
    FILE *file = fopen(argv[1], "rb");
    if (!file || fread(tux, 1, 110400, file) != 110400) fail("Tux read");
    fclose(file);
    for (unsigned y = 0; y < 240; y++) {
        pos = (120 + y) * 1004 + 150 * 2;
        if (info.fbops->fb_write(&info, tux + y * 460, 460, &pos) != 460) fail("Tux row write");
        /* Force the worst scheduling: a callback after every written row. */
        drain_held(&info, &pages);
    }
    for (unsigned y = 0; y < 240; y++) {
        pos = (120 + y) * 1004 + 300;
        if (info.fbops->fb_read(&info, readback, 460, &pos) != 460 || memcmp(readback, tux + y * 460, 460)) fail("Tux rectangle readback");
    }
    if (((u16 *)info.screen_buffer)[240 * 502 + 265] != 0xffff) fail("known white belly at (265,240)");
    u64 committed = p.stats.full_flushes;
    unsigned queued_before = scheduled;
    phase = 0;
    if (update_hold_store(&spi.dev, NULL, "0", 1) != 1 || p.update_held || phase != 3 ||
        p.stats.full_flushes != committed + 1 || p.stats.errors || scheduled != queued_before ||
        info.deferred_work.pending || p.dirty || p.render_stats.sample_offset != 217638 ||
        p.render_stats.tx_sample != 0xffff)
        fail("atomic login commit must send complete Tux after all held callbacks consumed damage");
    /* Mark a subsequent console update pending for the recovery test. */
    co5300fb_damage(&info, 0, 409, p.defio.delay);
    /* Run the actual sysfs store while Tux writes are still pending. Its
     * return must mean Tux has already been sent, without draining work. */
    unsigned locks = lock_calls, queued = scheduled;
    u64 full_flushes = p.stats.full_flushes;
    p.panel_test_active = true; p.panel_test_mode = 2;
    memset(p.pixels, 0, 411640);
    phase = 0; recovering = true;
    if (panel_reinit_draw_store(&spi.dev, NULL, "1\n", 2) != 2 || init_phase != 10 || phase != 3 ||
        reset_calls != 1 || reset_us != 10000 || sleep_ms != 750 || reset_raw != 1 ||
        lock_calls != locks + 1 || locked_mutex || p.io_lock.locked ||
        scheduled != queued || !info.deferred_work.pending || !p.dirty ||
        p.panel_test_active || p.panel_test_mode || p.quiet_commands ||
        p.stats.full_flushes != full_flushes + 1 || p.stats.errors ||
        p.render_stats.fb_sample != 0xffff || p.render_stats.tx_sample != 0xffff ||
        p.render_stats.sample_offset != 217638)
        fail("one-lock synchronous reset/init must submit the actual Tux before returning and preserve pending damage");
    recovering = false;
    drain(&info, &pages);
    if (p.render_stats.fb_sample != 0xffff || p.render_stats.tx_sample != 0xffff ||
        p.render_stats.sample_offset != 217638 || p.render_stats.bytes != 411640 ||
        !p.render_stats.nonzero_bytes)
        fail("known belly must be measured at byte 217638 before SPI submission");
    co5300fb_setcolreg(7, 65535, 65535, 65535, 0, &info);
    char bitmap[44]; memset(bitmap, 255, sizeof(bitmap));
    struct fb_image glyph = { .dx = 48, .dy = 66, .width = 12, .height = 22,
                             .depth = 1, .fg_color = 7, .data = bitmap };
    p.panel_test_active = true;
    info.fbops->fb_imageblit(&info, &glyph); dirty(&info, 66, 87);
    if (((u16 *)info.screen_buffer)[66 * 502 + 48] != 0xffff) fail("actual SUN12x22-format sys_imageblit must write glyph pixels");
    u64 held_flushes = p.stats.flushes, held_samples = p.render_stats.samples;
    phase = 0; info.deferred_work.pending = false;
    co5300fb_deferred_io(&info, &pages);
    if (phase || p.stats.flushes != held_flushes || p.render_stats.samples != held_samples)
        fail("held pattern must suppress SPI while canonical fbcon writes continue");
    p.panel_test_active = false;
    co5300fb_damage(&info, 0, 409, p.defio.delay);
    dirty(&info, 0, 409);
    drain(&info, &pages);
    struct fb_copyarea copy = { .sx = 48, .sy = 66, .dx = 48, .dy = 88, .width = 12, .height = 22 };
    info.fbops->fb_copyarea(&info, &copy); dirty(&info, 88, 109); drain(&info, &pages);
    if (((u16 *)info.screen_buffer)[88 * 502 + 48] != 0xffff ||
        ((u16 *)info.screen_buffer)[240 * 502 + 265] != 0xffff) fail("text/cursor copy must preserve Tux");
    pos = 411638;
    if (info.fbops->fb_write(&info, white, 8, &pos) != 2 || pos != 411640) fail("clipped short write at buffer end");
    dirty(&info, 409, 409); drain(&info, &pages);
    if (info.fbops->fb_write(&info, white, 0, &pos) != 0 || p.dirty) fail("zero-length write must not mark damage");
    pos = -1;
    if (info.fbops->fb_write(&info, white, 8, &pos) >= 0 || p.dirty) fail("invalid offset must not mark damage");
    struct fb_deferred_io_pageref page = { .offset = 241490 & ~4095U };
    pages.first = &page; drain(&info, &pages);
    free(white); free(readback); free(tux); co5300fb_destroy(&info);
    puts("PASS: actual Linux sysmem/deferred macros, 8-byte/full-frame/Tux writes and readback,"
         " synchronous one-lock reset/init/Tux RAMWR before return with pending work preserved,"
         " atomic clear + 240 Tux row writes with callbacks between every row,"
         " glyph/fill/copy damage, held-pattern canonical restoration, mmap pages and full-frame RGB565 SPI");
    return 0;
}
'''

fbh = (args.kernel / "include/linux/fb.h").read_text()
macros = fbh[fbh.index("#define __FB_GEN_DEFAULT_DEFERRED_OPS_RDWR"):
             fbh.index("static inline bool fb_be_math")]
program = [header,
           f'#include "{core / "sysmem.h"}"',
           f'#include "{core / "fb_imageblit.h"}"',
           f'#include "{core / "fb_fillrect.h"}"',
           f'#include "{core / "fb_copyarea.h"}"']
for name in ("fb_sys_read", "fb_sys_write"):
    program.append(block((core / "fb_sys_fops.c").read_text(), name))
for file, name in (("sysimgblt.c", "sys_imageblit"), ("sysfillrect.c", "sys_fillrect"),
                   ("syscopyarea.c", "sys_copyarea")):
    program.append(block((core / file).read_text(), name))
program += [macros, block(driver, "struct co5300fb_stats {"),
            block(driver, "struct co5300fb_render_stats {"),
            block(driver, "struct co5300fb_panel_test_stats {"), block(driver, "struct co5300fb {")]
for name in ("co5300fb_command", "co5300fb_write_window", "co5300fb_render", "co5300fb_measure_render",
             "co5300fb_flush_locked", "co5300fb_flush", "update_hold_store", "co5300fb_init_sequence",
             "co5300fb_reset_and_init", "panel_reinit_draw_store", "co5300fb_damage",
             "co5300fb_damage_range", "co5300fb_damage_area"):
    program.append(block(driver, name))
start = driver.index("FB_GEN_DEFAULT_DEFERRED_SYSMEM_OPS(")
program.append(driver[start:driver.index(")", start) + 1])
for name in ("co5300fb_deferred_io", "co5300fb_setcolreg", "co5300fb_check_var", "co5300fb_destroy"):
    program.append(block(driver, name))
program += [block(driver, "struct fb_ops co5300fb_ops ="), main]
with tempfile.TemporaryDirectory(prefix="co5300-fb-io-") as directory:
    temp = Path(directory)
    c = temp / "check.c"
    exe = temp / "check"
    c.write_text("\n".join(program))
    subprocess.run([os.environ.get("CC", "cc"), "-std=gnu11", "-O1", "-g",
                    "-fsanitize=address,undefined", str(c), "-o", str(exe)], check=True)
    result = subprocess.run([str(exe), str(repo / "tools/co5300-splash/tux.rgb565")],
                            env=dict(os.environ, ASAN_OPTIONS="detect_leaks=0"))
    raise SystemExit(result.returncode)
