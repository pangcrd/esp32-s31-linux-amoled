#!/usr/bin/env python3
"""Host-only splash test with fake fbdev/VT calls; never opens a real device."""
import os
from pathlib import Path
import subprocess
import tempfile
import runpy

repo = Path(__file__).resolve().parent.parent
source = repo / "tools/co5300-splash/co5300-splash.c"
converter = repo / "scripts/make-co5300-logo.py"
functions = runpy.run_path(str(converter))
# Header comments and raster bytes that resemble whitespace/comments.
assert functions["ppm"](b"P6\n# header\n2 1\n255\n #\n\xff\x00\x00") == (
    2, 1, b" #\n\xff\x00\x00")
assert functions["ppm"](b"P6\r\n1 1\r\n255\r\n\x00\xff\x00") == (
    1, 1, b"\x00\xff\x00")
harness = r'''
#include <errno.h>
#include <fcntl.h>
#include <linux/fb.h>
#include <linux/kd.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#include <stdarg.h>

static unsigned char frame[4096 * 502];
static unsigned int w, h, stride, writes, restores, sleeps, scenario;
static int acquired;
static void fail(const char *message) { fprintf(stderr, "FAIL: %s\n", message); exit(2); }
static int fake_open(const char *path, int flags, ...) {
    if (!strcmp(path, "/dev/fb0")) return 10000;
    if (!strcmp(path, "/dev/tty0")) return 10001;
    return open(path, flags);
}
static int fake_close(int fd) { return fd >= 10000 ? 0 : close(fd); }
static int fake_ioctl(int fd, unsigned long request, ...) {
    (void)fd;
    va_list ap;
    va_start(ap, request);
    if (request == FBIOGET_FSCREENINFO) {
        struct fb_fix_screeninfo *fix = va_arg(ap, void *);
        memset(fix, 0, sizeof(*fix));
        strcpy(fix->id, scenario == 1 ? "other panel" : "CO5300 QSPI");
        fix->type = FB_TYPE_PACKED_PIXELS;
        fix->visual = FB_VISUAL_TRUECOLOR;
        fix->line_length = stride;
        fix->smem_len = stride * h;
    } else if (request == FBIOGET_VSCREENINFO) {
        struct fb_var_screeninfo *var = va_arg(ap, void *);
        memset(var, 0, sizeof(*var));
        var->xres = var->xres_virtual = w;
        var->yres = var->yres_virtual = h;
        var->bits_per_pixel = scenario == 2 ? 32 : 16;
        var->red = (struct fb_bitfield) { .offset = 11, .length = 5 };
        var->green = (struct fb_bitfield) { .offset = 5, .length = 6 };
        var->blue.length = 5;
    } else if (request == KDGETMODE) {
        int *mode = va_arg(ap, int *);
        *mode = scenario == 3 ? KD_GRAPHICS : KD_TEXT;
    } else if (request == KDSETMODE) {
        int mode = va_arg(ap, int);
        if (mode == KD_GRAPHICS) {
            if (acquired) fail("duplicate VT acquisition");
            acquired = 1;
        } else if (mode == KD_TEXT) {
            if (!acquired) fail("restore without acquisition");
            restores++;
            acquired = 0;
        } else fail("unexpected VT mode");
    } else fail("unexpected ioctl");
    va_end(ap);
    return 0;
}
static ssize_t fake_pwrite(int fd, const void *buf, size_t size, off_t offset) {
    if (fd != 10000 || !acquired) fail("drawing without owning the VT");
    writes++;
    if (scenario == 4 && writes == 3) { errno = EIO; return -1; }
    if (offset < 0 || (size_t)offset + size > stride * h) fail("framebuffer bounds");
    /* Short writes verify the tool completes each row correctly. */
    if (scenario == 6 && size > 3) size = 3;
    memcpy(frame + offset, buf, size);
    return size;
}
static int fake_nanosleep(const struct timespec *req, struct timespec *rem) {
    (void)rem;
    if (req->tv_sec != 2 || req->tv_nsec) fail("splash hold time");
    sleeps++;
    if (scenario == 5) { raise(SIGTERM); errno = EINTR; return -1; }
    return 0;
}
#define open fake_open
#define close fake_close
#define ioctl fake_ioctl
#define pwrite fake_pwrite
#define nanosleep fake_nanosleep
#define main splash_main
'''
tail = r'''
#undef main
#undef open
#undef close
#undef ioctl
#undef pwrite
#undef nanosleep
static void check_image(const char *path) {
    unsigned char image[240 * 120 * 2];
    FILE *file = fopen(path, "rb");
    if (!file || fread(image, 1, sizeof(image), file) != sizeof(image)) fail("sample file");
    fclose(file);
    unsigned int x0 = (w - 240) / 2, y0 = (h - 120) / 2;
    if (x0 < 40 || y0 < 40 || w - x0 - 240 < 40 || h - y0 - 120 < 40)
        fail("40-pixel safe inset");
    for (unsigned int y = 0; y < h; y++) {
        for (unsigned int byte = 0; byte < stride; byte++) {
            unsigned char want = 0;
            if (y >= y0 && y < y0 + 120 && byte >= x0 * 2 && byte < (x0 + 240) * 2)
                want = image[((y - y0) * 240 * 2) + byte - x0 * 2];
            if (frame[y * stride + byte] != want) fail("centered image/black surround/padded stride");
        }
    }
}
int main(int argc, char **argv) {
    if (argc != 3) fail("test arguments");
    for (unsigned int portrait = 0; portrait < 2; portrait++) {
        w = portrait ? 410 : 502; h = portrait ? 502 : 410;
        stride = w * 2 + 16;
        for (scenario = 0; scenario <= 6; scenario++) {
            memset(frame, 0xa5, sizeof(frame));
            writes = restores = sleeps = acquired = interrupted = 0;
            char *args[] = { "splash", argv[1], "240", "120", NULL };
            int ret = splash_main(4, args);
            if (scenario == 0 || scenario == 6) {
                if (ret || restores != 1 || sleeps != 1) fail("success must restore text");
                check_image(argv[1]);
            } else {
                if (!ret) fail("invalid mode/failure/signal must report failure");
                if (scenario <= 3 && (writes || restores)) fail("validate before touching display");
                if (scenario >= 4 && restores != 1) fail("failure/signal must restore text");
            }
            if (acquired) fail("VT left in graphics mode");
        }
        scenario = 0; writes = restores = 0;
        char *bad[] = { "splash", argv[2], "240", "120", NULL };
        if (!splash_main(4, bad) || writes || restores) fail("truncated input must not draw");
    }
    puts("PASS: splash centering/inset, portrait, padded stride, short writes,"
         " input/format/VT validation and console restoration on failure/SIGTERM");
    return 0;
}
'''
with tempfile.TemporaryDirectory(prefix="co5300-splash-test-") as directory:
    temp = Path(directory)
    test = temp / "test.c"
    exe = temp / "test"
    bad = temp / "truncated.rgb565"
    bad.write_bytes(b"\0\0")
    ppm = temp / "colors.ppm"
    raw = temp / "colors.rgb565"
    ppm.write_bytes(b"P6\n3 1\n255\n\xff\x00\x00\x00\xff\x00\x00\x00\xff")
    subprocess.run(["python3", str(converter), str(ppm), str(raw)],
                   check=True, capture_output=True)
    assert raw.read_bytes() == b"\x00\xf8\xe0\x07\x1f\x00"
    subprocess.run(["python3", str(converter), "--sample", str(raw)],
                   check=True, capture_output=True)
    assert raw.read_bytes() == (source.parent / "sample.rgb565").read_bytes()
    test.write_text(harness + f'\n#include "{source}"\n' + tail)
    subprocess.run([os.environ.get("CC", "cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L",
                    "-Wall", "-Wextra", "-Werror", "-O1", "-g",
                    "-fsanitize=address,undefined", str(test), "-o", str(exe)], check=True)
    result = subprocess.run([str(exe), str(source.parent / "sample.rgb565"), str(bad)],
                            capture_output=True, text=True,
                            env=dict(os.environ, ASAN_OPTIONS="detect_leaks=0"))
    print(result.stdout, end="")
    if result.returncode:
        print(result.stderr, end="")
    raise SystemExit(result.returncode)
