#!/usr/bin/env python3
"""Exercise the actual CO5300 encoder against datasheet window constraints.
Run on the build host, not the ESP32 rootfs. This checks descriptors and
payloads; it does not emulate the ESP32 SPI peripheral or prove wire output.
"""
import argparse
import os
from pathlib import Path
import re
import subprocess
import tempfile

parser = argparse.ArgumentParser()
parser.add_argument("source", type=Path, help="co5300fb.c to validate")
args = parser.parse_args()
source = args.source.read_text()
# Mask comments/strings without shifting offsets while locating C blocks.
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

allocation = re.search(r'p->pixels = kmalloc\([^;]+;', source).group()
header = r"""
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
typedef uint8_t u8;
typedef uint16_t u16;
typedef uint32_t u32;
typedef int spinlock_t;
struct fb_deferred_io { int unused; };
struct gpio_desc;
struct device { int unused; };
struct spi_device { struct device dev; };
struct fb_info { void *par; char *screen_buffer; struct { u32 line_length; } fix; };
struct list_head { bool nonempty; };
struct spi_transfer {
    const void *tx_buf;
    unsigned int len;
    unsigned char tx_nbits;
    bool cs_change;
};
#define READ_ONCE(x) (x)
#define ARRAY_SIZE(x) (sizeof(x) / sizeof((x)[0]))
#define SPI_NBITS_SINGLE 1
#define SPI_NBITS_QUAD 4
#define dev_info(...) ((void)0)
#define dev_err_ratelimited(...) ((void)0)
#define spin_lock_irqsave(lock, flags) ((void)(lock), (flags) = 0)
#define spin_unlock_irqrestore(lock, flags) ((void)(lock), (void)(flags))
#define list_empty(head) (!(head)->nonempty)
#define cond_resched() ((void)0)
#define HZ 100
#define GFP_KERNEL 0
#define kmalloc(size, flags) malloc(size)
static unsigned int spans, phase, current_row;
static bool seen[502];
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
    if (t[0].tx_nbits != 1 || t[0].cs_change)
        fail("header lane width / CS boundary");
    if (phase < 2) {
        if (n != 1 || t[0].len != 8 || b[0] != 2 ||
            b[1] || b[3] || b[2] != (phase ? 0x2b : 0x2a))
            fail("CASET/RASET frame");
        u32 start = ((u32)b[4] << 8) | b[5];
        u32 end = ((u32)b[6] << 8) | b[7];
        if ((start & 1) || end < start || ((end - start + 1) & 1)) {
            fprintf(stderr, "Invalid %s: start=%u end=%u extent=%u\n",
                    phase ? "RASET" : "CASET", start, end, end - start + 1);
            fail("CO5300 datasheet requires even start and extent");
        }
        if (!phase) {
            if (start != 22 || end != 431) fail("column window");
        } else {
            if (start > 500 || end != start + 1) fail("two-row window");
            current_row = start;
        }
        phase++;
        return 0;
    }
    static const u8 ramwr[] = { 0x32, 0, 0x2c, 0 };
    if (n != 2 || t[0].len != 4 || memcmp(b, ramwr, 4) ||
        t[1].tx_nbits != 4 || t[1].cs_change || t[1].len != 1640)
        fail("single header + quad RGB565 payload in one message");
    b = t[1].tx_buf;
    for (u32 i = 0; i < 820; i++) {
        u16 expected = pattern(current_row + i / 410, i % 410);
        if (b[2*i] != (expected >> 8) || b[2*i+1] != (expected & 255))
            fail("both rows and big-endian RGB565 payload");
    }
    seen[current_row] = true;
    spans++;
    phase = 0;
    return 0;
}
"""
stub = r"""
static void co5300fb_damage(struct fb_info *info, u32 first, u32 last,
                            unsigned long delay) {
    (void)info; (void)first; (void)last; (void)delay;
    fail("unexpected retry");
}
"""
main = r"""
static void clear_capture(void) {
    spans = phase = 0;
    memset(seen, 0, sizeof(seen));
}
int main(void) {
    struct spi_device spi = {0};
    struct co5300fb state = { .spi = &spi, .width = 410, .height = 502,
                              .xoff = 22 };
    struct co5300fb *p = &state;
    p->command = calloc(259, 1);
    ALLOCATION
    u16 *vmem = calloc(410 * 502, sizeof(*vmem));
    if (!p->command || !p->pixels || !vmem) fail("allocation");
    for (u32 y = 0; y < 502; y++)
        for (u32 x = 0; x < 410; x++)
            vmem[y * 410 + x] = pattern(y, x);
    struct fb_info info = { .par = p, .screen_buffer = (char *)vmem,
                            .fix = { .line_length = 820 } };
    struct list_head pages = { .nonempty = true };
    co5300fb_deferred_io(&info, &pages);
    if (spans != 251 || phase) fail("full-frame coverage");
    for (u32 y = 0; y < 502; y += 2)
        if (!seen[y]) fail("missing row pair");
    pages.nonempty = false;
    const u32 cases[][4] = {
        { 11, 11, 10, 1 }, { 501, 501, 500, 1 }, { 31, 32, 30, 2 }
    };
    for (u32 i = 0; i < ARRAY_SIZE(cases); i++) {
        clear_capture();
        p->dirty = true;
        p->first = cases[i][0];
        p->last = cases[i][1];
        co5300fb_deferred_io(&info, &pages);
        if (spans != cases[i][3] || !seen[cases[i][2]] || phase)
            fail("partial damage must include its unchanged neighbouring row");
    }
    free(p->command); free(p->pixels); free(vmem);
    puts("PASS: 251 valid two-row windows, exact 411640-byte RGB565 payload,"
         " mixed-lane CS continuity, odd/bottom/straddling damage");
    return 0;
}
"""
program = "\n".join([
    header, block("struct co5300fb {"), stub,
    block("static int co5300fb_command("),
    block("static int co5300fb_row("),
    block("static void co5300fb_deferred_io("),
    main.replace("ALLOCATION", allocation),
])
with tempfile.TemporaryDirectory(prefix="co5300-windows-") as temp:
    test = Path(temp) / "test.c"
    exe = Path(temp) / "test"
    test.write_text(program)
    subprocess.run(["cc", "-std=gnu11", "-O1", "-g",
                    "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                    str(test), "-o", str(exe)], check=True)
    env = dict(os.environ, ASAN_OPTIONS="detect_leaks=0")
    raise SystemExit(subprocess.run([str(exe)], env=env).returncode)
