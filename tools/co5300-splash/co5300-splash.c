// SPDX-License-Identifier: MIT
/* Little-endian RGB565 splash or persistent login image for the CO5300. */
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

#define INSET 40
static volatile sig_atomic_t interrupted;

static void stop(int sig)
{
	(void)sig;
	interrupted = 1;
}

static unsigned int number(const char *s, unsigned int limit)
{
	char *end;
	unsigned long n;

	errno = 0;
	n = strtoul(s, &end, 10);
	if (errno || !*s || *end || !n || n > limit) {
		fprintf(stderr, "Invalid number: %s\n", s);
		exit(1);
	}
	return n;
}

static int position(const char *s, unsigned int *x, unsigned int *y)
{
	char *end;
	unsigned long a, b;
	const char *second;

	errno = 0;
	a = strtoul(s, &end, 10);
	if (errno || end == s || *end != ',' || a > 512)
		return -1;
	second = end + 1;
	b = strtoul(second, &end, 10);
	if (errno || end == second || *end || b > 512)
		return -1;
	*x = a;
	*y = b;
	return 0;
}

static int write_at(int fd, const void *buf, size_t size, off_t offset)
{
	const unsigned char *p = buf;
	off_t pos;

	do {
		pos = lseek(fd, offset, SEEK_SET);
	} while (pos < 0 && errno == EINTR && !interrupted);
	if (pos != offset) {
		if (pos >= 0)
			errno = EIO;
		return -1;
	}

	while (size && !interrupted) {
		ssize_t n = write(fd, p, size);

		if (n < 0 && errno == EINTR)
			continue;
		if (n <= 0) {
			if (!n)
				errno = EIO;
			return -1;
		}
		p += n;
		size -= n;
	}
	if (interrupted) {
		errno = EINTR;
		return -1;
	}
	return 0;
}

static int verify_at(int fd, const void *buf, size_t size, off_t offset)
{
	unsigned char actual[844]; /* Maximum image width is 422 RGB565 pixels. */
	size_t done = 0;
	off_t pos;

	if (size > sizeof(actual)) {
		errno = EINVAL;
		return -1;
	}
	do {
		pos = lseek(fd, offset, SEEK_SET);
	} while (pos < 0 && errno == EINTR && !interrupted);
	if (pos != offset) {
		if (pos >= 0)
			errno = EIO;
		return -1;
	}
	while (done < size && !interrupted) {
		ssize_t n = read(fd, actual + done, size - done);

		if (n < 0 && errno == EINTR)
			continue;
		if (n <= 0) {
			if (!n)
				errno = EIO;
			return -1;
		}
		done += n;
	}
	if (interrupted || memcmp(actual, buf, size)) {
		errno = interrupted ? EINTR : EIO;
		fprintf(stderr, "co5300-splash: framebuffer readback failed at byte %lld\n",
			(long long)offset);
		return -1;
	}
	return 0;
}

int main(int argc, char **argv)
{
	struct fb_fix_screeninfo fix;
	struct fb_var_screeninfo var;
	struct stat st;
	struct sigaction sa = { .sa_handler = stop };
	unsigned int width, height, seconds, x = 0, y = 0, row;
	unsigned char *image = NULL, *black = NULL;
	size_t size, done = 0;
	int input = -1, fb = -1, tty = -1, mode, graphics = 0, ret = 1;
	int persist = 0, positioned = 0, first = 1;
	struct timespec hold;

	while (first < argc && argv[first][0] == '-') {
		if (!strcmp(argv[first], "--persist") && !persist) {
			persist = 1;
			first++;
		} else if (!strcmp(argv[first], "--at") && !positioned && first + 1 < argc) {
			if (position(argv[first + 1], &x, &y))
				goto usage;
			positioned = 1;
			first += 2;
		} else {
			goto usage;
		}
	}
	if (argc - first != 3 && (persist || argc - first != 4))
		goto usage;
	width = number(argv[first + 1], 422);
	height = number(argv[first + 2], 422);
	seconds = argc - first == 4 ? number(argv[first + 3], 10) : 2;
	size = (size_t)width * height * 2;
	input = open(argv[first], O_RDONLY | O_CLOEXEC);
	if (input < 0 || fstat(input, &st) < 0)
		goto error;
	if (st.st_size != (off_t)size) {
		fprintf(stderr, "Image must contain exactly WIDTH * HEIGHT * 2 bytes\n");
		goto out;
	}
	image = malloc(size);
	if (!image)
		goto error;
	while (done < size) {
		ssize_t n = read(input, image + done, size - done);

		if (n < 0 && errno == EINTR)
			continue;
		if (n <= 0) {
			if (!n)
				errno = EIO;
			goto error;
		}
		done += n;
	}
	fb = open("/dev/fb0", O_RDWR | O_CLOEXEC);
	if (fb < 0 || ioctl(fb, FBIOGET_FSCREENINFO, &fix) < 0 ||
	    ioctl(fb, FBIOGET_VSCREENINFO, &var) < 0)
		goto error;
	/* Reject other displays, formats, virtual modes and panned surfaces. */
	if (strncmp(fix.id, "CO5300 QSPI", sizeof(fix.id)) ||
	    fix.type != FB_TYPE_PACKED_PIXELS || fix.visual != FB_VISUAL_TRUECOLOR ||
	    !((var.xres == 502 && var.yres == 410) ||
	      (var.xres == 410 && var.yres == 502)) ||
	    var.xres_virtual != var.xres || var.yres_virtual != var.yres ||
	    var.xoffset || var.yoffset || var.bits_per_pixel != 16 ||
	    var.red.offset != 11 || var.red.length != 5 || var.red.msb_right ||
	    var.green.offset != 5 || var.green.length != 6 || var.green.msb_right ||
	    var.blue.offset || var.blue.length != 5 || var.blue.msb_right ||
	    var.transp.length || var.nonstd || var.grayscale ||
	    fix.line_length < var.xres * 2 || fix.line_length > 4096 ||
	    fix.smem_len < fix.line_length * var.yres ||
	    width > var.xres - 2 * INSET || height > var.yres - 2 * INSET) {
		fprintf(stderr, "Expected CO5300 RGB565; image must fit the 40 px inset\n");
		goto out;
	}
	if (!positioned) {
		x = (var.xres - width) / 2;
		y = (var.yres - height) / 2;
	}
	if (x < INSET || y < INSET ||
	    x > var.xres - INSET - width || y > var.yres - INSET - height) {
		fprintf(stderr, "Image position must stay inside the 40 px inset\n");
		goto out;
	}
	if (!persist) {
		black = calloc(1, fix.line_length);
		if (!black)
			goto error;
	}
	/* The file format is LE; fbdev pixels are native-endian. */
#if __BYTE_ORDER__ == __ORDER_BIG_ENDIAN__
	for (done = 0; done < size; done += 2) {
		unsigned char byte = image[done];

		image[done] = image[done + 1];
		image[done + 1] = byte;
	}
#endif
	tty = open("/dev/tty0", O_RDWR | O_CLOEXEC);
	if (tty < 0 || ioctl(tty, KDGETMODE, &mode) < 0)
		goto error;
	if (mode != KD_TEXT) {
		fprintf(stderr, "Text console is already owned by a graphics application\n");
		goto out;
	}
	sigemptyset(&sa.sa_mask);
	if (sigaction(SIGINT, &sa, NULL) || sigaction(SIGTERM, &sa, NULL) ||
	    sigaction(SIGHUP, &sa, NULL))
		goto error;
	if (!persist) {
		if (interrupted || ioctl(tty, KDSETMODE, KD_GRAPHICS) < 0)
			goto error;
		graphics = 1;
		/* fbcon collects text while graphics mode suppresses redraws. */
		for (row = 0; row < var.yres; row++)
			if (write_at(fb, black, fix.line_length, (off_t)row * fix.line_length))
				goto error;
	}
	/* Persistent mode touches only the image rectangle, then exits in
	 * KD_TEXT. Getty's SUN12x22 text/cursor on rows 3/4 is above y=120;
	 * its partial updates leave these pixels intact. A full VT redraw or
	 * scrolling can replace them; the wrapper redraws on each respawn. */
	for (row = 0; row < height; row++)
		if (write_at(fb, image + (size_t)row * width * 2, width * 2,
			     (off_t)(y + row) * fix.line_length + x * 2))
			goto error;
	/* A successful syscall alone did not establish visible pixels in the
	 * board report. Check the actual target rectangle, then drain defio
	 * through fb_deferred_io_fsync before reporting success to getty. */
	for (row = 0; row < height; row++)
		if (verify_at(fb, image + (size_t)row * width * 2, width * 2,
			      (off_t)(y + row) * fix.line_length + x * 2))
			goto error;
	while (fsync(fb) < 0) {
		if (errno != EINTR || interrupted)
			goto error;
	}
	if (persist) {
		ret = 0;
		goto out;
	}
	hold = (struct timespec) { .tv_sec = seconds };
	while (!interrupted && nanosleep(&hold, &hold) < 0) {
		if (errno != EINTR)
			goto error;
	}
	ret = interrupted ? 1 : 0;
	goto out;
error:
	perror("co5300-splash");
out:
	/* Only timed mode restores the VT; persistent mode never requests a
	 * console redraw that could erase its image. */
	if (graphics && ioctl(tty, KDSETMODE, KD_TEXT) < 0) {
		perror("co5300-splash: restore text console");
		ret = 1;
	}
	if (tty >= 0)
		close(tty);
	if (fb >= 0)
		close(fb);
	if (input >= 0)
		close(input);
	free(black);
	free(image);
	return ret;
usage:
	fprintf(stderr,
		"Usage: %s [--at X,Y] [--persist] IMAGE.rgb565 WIDTH HEIGHT [SECONDS (1..10)]\n"
		"  --persist: draw only the image and exit in text mode; omit SECONDS.\n",
		argv[0]);
	return 1;
}
