# CO5300 splash and login Tux

Boot is plain text: kernel `CONFIG_LOGO` is disabled and the optional
`S02co5300-splash` service defaults to `ENABLED=0`.

`amoled-getty` clears to black, prints three blank lines, then runs:

```sh
co5300-splash --at 150,120 --persist /usr/share/co5300-splash/tux.rgb565 230 240
```

It then execs getty even if drawing fails. The image occupies x=150..379,
y=120..359, inside the 40-pixel safe inset on the 502x410 landscape framebuffer.
Portrait mode rejects this fixed position safely and still starts getty.

Framebuffer writes use `lseek` + `write`, including short-write handling.
The tool reads back every target row and calls `fsync` to drain kernel deferred
I/O before reporting success. A mismatched readback or failed sync reports an
error and the wrapper continues to getty. This verifies the pixels in memory
and completion of scheduled work; actual panel visibility needs board testing.

`--persist` validates the image, fbdev format, position and current text VT,
writes only the image rectangle using the framebuffer stride, and exits. It
does not clear, sleep, or change/restore the VT mode. SUN12x22 banner/prompt
glyphs and cursor updates on rows 3/4 touch y=66..109, so normal prompt typing,
backspace and idle cursor blinking leave Tux intact. A full clear, VT switch,
scroll or input wrapping into the image can replace it; it is drawn again on
each getty respawn. This is a login decoration, not an overlay over shell text.

The default timed mode remains available, as does `--at X,Y` with timed mode:

```sh
co5300-splash /usr/share/co5300-splash/logo.rgb565 240 120 2
```

`tux-source.ppm` is Linux 7.3.4's `drivers/video/logo/logo_linux_clut224.ppm`
(80x80). Tux artwork is by Larry Ewing. The supplied raw file is a nearest
neighbour resize that preserves the original black, white and yellow/orange
palette. Reproduce it from the repository root:

```sh
python3 scripts/make-co5300-logo.py --size 230x240 \
  tools/co5300-splash/tux-source.ppm tools/co5300-splash/tux.rgb565
```

Image/source license: GPL-2.0, included in `COPYING.tux`; tool and converter: MIT.
