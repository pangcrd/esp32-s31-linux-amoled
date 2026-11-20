#!/usr/bin/env python3
"""Host-only splash test with fake fbdev/VT calls; never opens a real device."""
import os
from pathlib import Path
import subprocess
import tempfile
import runpy
import shlex

repo = Path(__file__).resolve().parent.parent
source = repo / "tools/co5300-splash/co5300-splash.c"
converter = repo / "scripts/make-co5300-logo.py"
functions = runpy.run_path(str(converter))
# Header comments and raster bytes that resemble whitespace/comments.
assert functions["ppm"](b"P6\n# header\n2 1\n255\n #\n\xff\x00\x00") == (
    2, 1, b" #\n\xff\x00\x00")
assert functions["ppm"](b"P6\r\n1 1\r\n255\r\n\x00\xff\x00") == (
    1, 1, b"\x00\xff\x00")
assert functions["ppm"](b"P3\n# source\n1 1\n255\n255 0 0\n") == (1, 1, b"\xff\0\0")
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
static unsigned int w, h, stride, writes, restores, sleeps, scenario, syncs;
static off_t fbpos;
static int acquired, persistent;
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
        if (persistent) fail("persistent mode must not change VT mode");
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
static off_t fake_lseek(int fd, off_t offset, int whence) {
    if (fd != 10000) return lseek(fd, offset, whence);
    if (whence != SEEK_SET || offset < 0 || offset > stride * h) fail("seek bounds");
    if (scenario == 7) { errno = ESPIPE; return -1; }
    fbpos = offset;
    return offset;
}
static ssize_t fake_write(int fd, const void *buf, size_t size) {
    off_t offset = fbpos;
    if (fd != 10000 || (!acquired && !persistent)) fail("drawing without a text/owned VT");
    writes++;
    if (scenario == 4 && writes == 3) { errno = EIO; return -1; }
    if (offset < 0 || (size_t)offset + size > stride * h) fail("framebuffer bounds");
    /* Short writes verify the tool completes each row correctly. */
    if (scenario == 6 && size > 3) size = 3;
    if (scenario != 8) memcpy(frame + offset, buf, size);
    fbpos += size;
    return size;
}
static ssize_t fake_read(int fd, void *buf, size_t size) {
    if (fd != 10000) return read(fd, buf, size);
    if (fbpos < 0 || (size_t)fbpos + size > stride * h) fail("readback bounds");
    if (scenario == 6 && size > 5) size = 5;
    memcpy(buf, frame + fbpos, size);
    fbpos += size;
    return size;
}
static int fake_fsync(int fd) {
    if (fd != 10000) fail("sync wrong fd");
    if (scenario == 9) { errno = EIO; return -1; }
    syncs++;
    return 0;
}
static int fake_nanosleep(const struct timespec *req, struct timespec *rem) {
    if (persistent) fail("persistent mode must exit without holding");
    (void)rem;
    if (req->tv_sec != 2 || req->tv_nsec) fail("splash hold time");
    sleeps++;
    if (scenario == 5) { raise(SIGTERM); errno = EINTR; return -1; }
    return 0;
}
#define open fake_open
#define close fake_close
#define ioctl fake_ioctl
#define lseek fake_lseek
#define write fake_write
#define read fake_read
#define fsync fake_fsync
#define nanosleep fake_nanosleep
#define main splash_main
'''
tail = r'''
#undef main
#undef open
#undef close
#undef ioctl
#undef lseek
#undef write
#undef read
#undef fsync
#undef nanosleep
static void check_image(const char *path, unsigned int iw, unsigned int ih,
                        unsigned int x0, unsigned int y0, int outside) {
    unsigned char image[230 * 240 * 2];
    FILE *file = fopen(path, "rb");
    if (!file || fread(image, 1, iw * ih * 2, file) != iw * ih * 2) fail("image file");
    fclose(file);
    if (x0 < 40 || y0 < 40 || w - x0 - iw < 40 || h - y0 - ih < 40)
        fail("40-pixel safe inset");
    for (unsigned int y = 0; y < h; y++) {
        for (unsigned int byte = 0; byte < stride; byte++) {
            unsigned char want = outside;
            if (y >= y0 && y < y0 + ih && byte >= x0 * 2 && byte < (x0 + iw) * 2)
                want = image[((y - y0) * iw * 2) + byte - x0 * 2];
            else if (outside < 0) continue;
            if (frame[y * stride + byte] != want) fail("centered image/black surround/padded stride");
        }
    }
}
int main(int argc, char **argv) {
    if (argc != 4) fail("test arguments");
    for (unsigned int portrait = 0; portrait < 2; portrait++) {
        w = portrait ? 410 : 502; h = portrait ? 502 : 410;
        stride = w * 2 + 16;
        for (scenario = 0; scenario <= 9; scenario++) {
            memset(frame, 0xa5, sizeof(frame));
            writes = restores = sleeps = syncs = acquired = interrupted = 0;
            char *args[] = { "splash", argv[1], "240", "120", NULL };
            int ret = splash_main(4, args);
            if (scenario == 0 || scenario == 6) {
                if (ret || restores != 1 || sleeps != 1 || syncs != 1) fail("success must sync and restore text");
                check_image(argv[1], 240, 120, (w - 240) / 2, (h - 120) / 2, 0);
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
    w = 502; h = 410; stride = w * 2 + 16; persistent = 1;
    for (scenario = 0; scenario <= 9; scenario++) {
        if (scenario == 5) continue;
        memset(frame, 0xa5, sizeof(frame));
        writes = restores = sleeps = syncs = acquired = interrupted = 0;
        char *args[] = { "splash", "--at", "150,120", "--persist", argv[3], "230", "240", NULL };
        int ret = splash_main(7, args);
        if (scenario == 0 || scenario == 6) {
            if (ret || restores || sleeps || acquired || syncs != 1) fail("persistent mode must sync without a VT switch");
            check_image(argv[3], 230, 240, 150, 120, 0xa5);
            /* SUN12x22 fbcon glyph/cursor updates on banner row 3 and
             * prompt row 4 touch only y=66..109, including backspace and
             * idle cursor blinks. Model those rectangles and check Tux. */
            for (unsigned int blink = 0; blink < 100; blink++) {
                for (unsigned int y = 66; y < 110; y++)
                    memset(frame + y * stride + 48 * 2, blink & 255, 360 * 2);
                check_image(argv[3], 230, 240, 150, 120, -1);
            }
        } else {
            if (!ret || restores || sleeps || acquired) fail("persistent failure must not change VT");
            if (scenario <= 3 && writes) fail("persistent validation before drawing");
        }
    }
    const char *badpos[] = { "39,120", "150,39", "233,120", "150,131",
                             "-1,120", "150,-1", "150,120x", "150,", "150" };
    for (unsigned int i = 0; i < sizeof(badpos) / sizeof(badpos[0]); i++) {
        scenario = writes = restores = sleeps = interrupted = 0;
        char *args[] = { "splash", "--at", (char *)badpos[i], "--persist", argv[3], "230", "240", NULL };
        if (!splash_main(7, args) || writes || restores || sleeps) fail("invalid position must not draw");
    }
    scenario = writes = restores = sleeps = interrupted = 0;
    char *bad[] = { "splash", "--persist", argv[2], "230", "240", NULL };
    if (!splash_main(5, bad) || writes || restores || sleeps) fail("persistent truncated input");
    w = 410; h = 502; stride = w * 2;
    char *portrait[] = { "splash", "--at", "150,120", "--persist", argv[3], "230", "240", NULL };
    if (!splash_main(7, portrait) || writes || restores || sleeps) fail("landscape position must reject portrait inset");
    puts("PASS: splash centering/inset, portrait, padded stride, short writes,"
         " seek/write/readback/fsync, persistent geometry/preserved pixels/typing/idle and VT restoration");
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
    subprocess.run(["python3", str(converter), "--size", "230x240",
                    str(source.parent / "tux-source.ppm"), str(raw)],
                   check=True, capture_output=True)
    tux = (source.parent / "tux.rgb565").read_bytes()
    assert len(tux) == 230 * 240 * 2 and raw.read_bytes() == tux
    # The image must contain black/white plus yellow/orange artwork.
    colors = {int.from_bytes(tux[i:i + 2], "little") for i in range(0, len(tux), 2)}
    assert 0 in colors and 0xffff in colors and len(colors) > 100
    assert any(((c >> 11) & 31) > 24 and ((c >> 5) & 63) > 25 and (c & 31) < 6 for c in colors)
    test.write_text(harness + f'\n#include "{source}"\n' + tail)
    subprocess.run([os.environ.get("CC", "cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L",
                    "-Wall", "-Wextra", "-Werror", "-O1", "-g",
                    "-fsanitize=address,undefined", str(test), "-o", str(exe)], check=True)
    result = subprocess.run([str(exe), str(source.parent / "sample.rgb565"), str(bad),
                             str(source.parent / "tux.rgb565")],
                            capture_output=True, text=True,
                            env=dict(os.environ, ASAN_OPTIONS="detect_leaks=0"))
    print(result.stdout, end="")
    if result.returncode:
        print(result.stderr, end="")
        raise SystemExit(result.returncode)
    # Exercise the actual wrapper with fake tools/devices; both drawing
    # success and failure must finish by execing getty after the clear.
    tty = temp / "tty0"
    event = temp / "event"
    getty = temp / "getty"
    splash = temp / "splash"
    expected_clear = b"\x1b[0m\x1b[2J\x1b[H\x1b[3J\n\n"
    splash.write_text("#!/usr/bin/env python3\nimport os, sys\nfrom pathlib import Path\n"
                      f"assert Path({str(tty)!r}).read_bytes() == {expected_clear!r}\n"
                      "control = Path(os.environ['HOLD_FILE'])\n"
                      "assert not control.exists() or control.read_text() == '1\\n'\n"
                      "assert sys.argv[1:] == ['--at', '150,120', '--persist', "
                      "'/usr/share/co5300-splash/tux.rgb565', '230', '240']\n"
                      f"Path({str(event)!r}).write_text('draw\\n')\n"
                      "if os.environ.get('INTERRUPT') == '1':\n"
                      " import signal\n signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                      " os.kill(os.getppid(), signal.SIGTERM)\n"
                      "sys.exit(int(os.environ['SPLASH_FAIL']))\n")
    getty.write_text("#!/usr/bin/env python3\nimport os, sys\nfrom pathlib import Path\n"
                     "assert sys.argv[1:] == ['-L', 'tty0', '115200', 'vt100']\n"
                     "control = Path(os.environ['HOLD_FILE'])\n"
                     "assert not control.exists() or control.read_text() == '0\\n'\n"
                     f"p = Path({str(event)!r})\nassert p.read_text() == 'draw\\n'\n"
                     "p.write_text(f'draw\\ngetty {os.getpid()}\\n')\n")
    splash.chmod(0o755)
    getty.chmod(0o755)
    wrapper = temp / "wrapper"
    text = (repo / "br2-external/board/esp32s31/amoled-getty").read_text()
    # A tty consumes successive writes; append emulates that on a regular file.
    for controls in (0, 1):
        panel = temp / f"panel{controls}"
        panel.mkdir()
        hold_file = panel / "update_hold"
        if controls:
            hold_file.write_text("0\n")
        wrapper.write_text(text.replace("> /dev/tty0", ">> " + shlex.quote(str(tty)))
                          .replace("panel=/sys/class/graphics/fb0/device", "panel=" + shlex.quote(str(panel)))
                          .replace("/usr/bin/co5300-splash", shlex.quote(str(splash)))
                          .replace("/sbin/getty", shlex.quote(str(getty))))
        for failed in (0, 1):
            tty.write_bytes(b"")
            process = subprocess.Popen(["sh", str(wrapper)], stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE,
                                       env=dict(os.environ, SPLASH_FAIL=str(failed), HOLD_FILE=str(hold_file)))
            out, err = process.communicate(timeout=10)
            assert process.returncode == 0, (out, err)
            assert event.read_text() == f"draw\ngetty {process.pid}\n"
            assert tty.read_bytes() == expected_clear
            assert bool(err) == bool(failed)
        if controls:
            tty.write_bytes(b"")
            process = subprocess.Popen(["sh", str(wrapper)], stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, env=dict(os.environ, SPLASH_FAIL="0",
                                                                       HOLD_FILE=str(hold_file), INTERRUPT="1"))
            out, err = process.communicate(timeout=10)
            assert process.returncode == 1, (out, err)
            assert hold_file.read_text() == "0\n" and event.read_text() == "draw\n"
    # BusyBox 1.37 libbb/login.c print_login_issue emits a newline even
    # before opening /etc/issue. Its trailing newline advances the prompt.
    issue = (repo / "br2-external/board/esp32s31/rootfs-overlay-coreboard1/etc/issue").read_bytes()
    assert b"\x1b" not in issue and issue.count(b"\n") == 1
    assert max(len(line) for line in issue.split(b"\n")) < 502 // 12
    start_row = expected_clear.count(b"\n")
    banner_row = start_row + 1
    prompt_row = banner_row + issue.count(b"\n")
    assert banner_row * 22 >= 40 and (prompt_row + 1) * 22 <= 120
    assert (3 + 1 + issue.count(b"\n") + 1) * 22 > 120, "old three-line spacing overlaps Tux"
    print("PASS: login transaction holds before drawing, releases before exec getty on success/failure,"
          " releases on interruption, supports older kernels; BusyBox issue newline puts text above Tux")
