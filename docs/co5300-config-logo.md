# CO5300 runtime settings and boot logos

The CO5300 driver now exposes settings through the SPI parent of fb0:

```sh
PANEL=/sys/class/graphics/fb0/device
cat "$PANEL/spi_freq_hz" "$PANEL/rotation"
echo 20000000 > "$PANEL/spi_freq_hz"
echo 180 > "$PANEL/rotation"
```

Run writes as root. `spi_freq_hz` is the requested decimal frequency in Hz;
it accepts nonzero values between the SPI controller's `min_speed_hz` and
`max_speed_hz`. Hardware divider rounding can lower the actual clock.
Out-of-range or malformed values return `EINVAL` and retain the old setting.
The board default stays **40000000 Hz**; higher frequencies need board testing.
Changes wait for the current complete window, then affect subsequent transfers.
No reboot or rebuild is needed. Settings reset to DT defaults at reboot.

`rotation` accepts **0 or 180** on the default **502x410** landscape framebuffer.
A changed value schedules a full redraw; stride, dimensions, fbcon layout and
existing mmap mappings stay stable. Both rotations preserve the GRAM rectangle
and even CASET/RASET starts and extents. The symmetric 40-pixel safe inset and
the existing SUN12x22 login indentation remain valid after a flip.

For portrait, add `rotation = <90>;` or `<270>` to `panel@0` in
`linux/arch/riscv/boot/dts/espressif/esp32s31_coreboard1_amoled.dts`, rebuild the
kernel with its built-in DTB, flash and reboot. Keep `chipone,width/height`
and offsets as they are: these describe the original landscape GRAM rectangle.
The driver then exposes **410x502** and accepts **90/270** flips at runtime.
Changing between portrait and landscape through sysfs returns `EINVAL`.
Angles are relative to the current landscape view, not fbcon's own rotation.

The supplier initialization records, reset sequence, 40 MHz DT default and
SPI/GDMA implementation are unchanged. ABI details are in
`linux/Documentation/ABI/testing/sysfs-driver-co5300fb`.

## Kernel Tux

`linux-amoled.fragment` enables `CONFIG_LOGO=y` and
`CONFIG_LOGO_LINUX_CLUT224=y`. `fbcon=font:SUN12x22,logo-pos:center,logo-count:1`
centers one standard 80x80 Tux, inside the 40-pixel inset. It is relatively small
on this panel; the custom splash below is 240x120. Boot text can scroll the
kernel logo away, so watch from power-on. The normal login prompt follows.
Keep fbcon takeover enabled and do not add `quiet` or `logo.nologo` if testing
Tux. The host check compiles the logo and fbcon; visibility needs board testing.

## Custom splash

`tools/co5300-splash/co5300-splash.c` reads a raw **little-endian RGB565** image,
with no header: `width * height * 2` bytes, row-major, top-left first. For example,
red is `00 f8`, green is `e0 07`, blue is `1f 00`. The kernel handles panel
rotation and MSB-first wire encoding; do not pre-rotate or byte-swap the image.

The tool validates fbdev format, dimensions and file size before drawing,
centers the image on a black background, and limits it to **422x330** landscape
or **330x422** portrait. It uses the actual framebuffer stride. It temporarily
sets tty0 to graphics mode to prevent fbcon from overwriting the splash, holds
it for 1..10 seconds (default 2), restores the text console and exits. Handled
SIGINT/SIGTERM/SIGHUP and write failures also restore the console. On demand:

```sh
co5300-splash /usr/share/co5300-splash/logo.rgb565 240 120 2
```

The Buildroot package installs the tool, sample, `/etc/default/co5300-splash`
and `/etc/init.d/S02co5300-splash`. `scripts/enable-amoled.sh` enables the package
only in the opt-in AMOLED output. The init script runs during rcS, before getty;
errors continue boot. Set `ENABLED=0` to disable it. `IMAGE`, `WIDTH`, `HEIGHT`
and `SECONDS` are configurable without rebuilding. The existing getty clears
and indents the prompt after startup.

Replace the image using an 8-bit binary PPM (P6), converted on your host:

```sh
# Export your artwork at a size within the inset. ImageMagick is optional:
magick artwork.png -resize 422x330 -background black -alpha remove -alpha off \
  -depth 8 PPM:logo.ppm
# Ensure binary P6 output (ImageMagick's default; do not use ASCII P3).
python3 scripts/make-co5300-logo.py logo.ppm logo.rgb565
# The converter prints WIDTH and HEIGHT; put those in the board's config.
# Recreate the supplied placeholder:
python3 scripts/make-co5300-logo.py --sample tools/co5300-splash/sample.rgb565
```

## Build and flash on the user's host

Use the existing configured AMOLED output and working loader/OpenSBI artifacts.
These are the same flash-only build steps used for the DMA update. Do not run
`make rootfs` afterward: it reapplies the plain board defconfig.

```sh
cd /path/to/esp32-s31-linux-amoled
git fetch origin
git switch fix/co5300-config-logo
export ESP32S31_VERSION="$(make -s version)"
BR_OUT="$PWD/build/br-output-coreboard1-amoled"
if [ ! -f "$BR_OUT/.config" ]; then
  make -C external/buildroot O="$BR_OUT" BR2_EXTERNAL="$PWD/br2-external" coreboard1_defconfig
fi
sh scripts/enable-amoled.sh "$BR_OUT"
make -C external/buildroot O="$BR_OUT" BR2_EXTERNAL="$PWD/br2-external"
KERNEL="$BR_OUT/build/linux-7.3.4"
rg '^(CONFIG_FB_CO5300|CONFIG_LOGO|CONFIG_LOGO_LINUX_CLUT224|CONFIG_FRAMEBUFFER_CONSOLE|CONFIG_ESP32S31_AXI_GDMA)=y$' "$KERNEL/.config"
test -s "$KERNEL/drivers/video/logo/logo_linux_clut224.o"
"$BR_OUT/host/bin/riscv32-buildroot-linux-musl-nm" "$KERNEL/vmlinux" | rg 'logo_linux_clut224|fb_show_logo'
sh scripts/copy-images.sh "$BR_OUT/images" build/coreboard1 shared/partitions-diskroot.csv
make -s flash-args BOARD=coreboard1 > build/coreboard1/flash-args.txt
esptool --chip esp32s31 merge-bin --flash-size 32MB \
  -o build/coreboard1/flash.bin @build/coreboard1/flash-args.txt
esptool --chip esp32s31 -p PORT -b 921600 write-flash 0x0 build/coreboard1/flash.bin
```

Replace `PORT` with the download port. No USB disk rewrite is needed for sysfs
or kernel Tux. The custom userspace splash needs its files on the existing USB
root filesystem. Copy them over your existing SSH connection (replace `BOARD_IP`):

```sh
scp -O "$BR_OUT/target/usr/bin/co5300-splash" \
  tools/co5300-splash/sample.rgb565 tools/co5300-splash/co5300-splash.conf \
  tools/co5300-splash/S02co5300-splash root@BOARD_IP:/tmp/
ssh root@BOARD_IP
# On the board:
mkdir -p /usr/share/co5300-splash /etc/default
cp /tmp/co5300-splash /usr/bin/co5300-splash
cp /tmp/sample.rgb565 /usr/share/co5300-splash/logo.rgb565
cp /tmp/co5300-splash.conf /etc/default/co5300-splash
cp /tmp/S02co5300-splash /etc/init.d/S02co5300-splash
chmod 755 /usr/bin/co5300-splash /etc/init.d/S02co5300-splash
sync
```

This updates four files in place; it uses the existing USB filesystem. For fresh
root filesystems Buildroot installs them automatically. Replacing only the logo
and its dimensions later requires no kernel rebuild or reboot.

## Acceptance on the board

```sh
PANEL=/sys/class/graphics/fb0/device
cat "$PANEL/spi_freq_hz" "$PANEL/rotation" "$PANEL/flush_stats"
find /sys/bus/platform/devices -name dma_stats -exec cat '{}' ';'
echo 20000000 > "$PANEL/spi_freq_hz"
cat "$PANEL/spi_freq_hz"                       # 20000000
echo 180 > "$PANEL/rotation"
cat "$PANEL/rotation"                          # 180
sleep 1
cat "$PANEL/flush_stats"
find /sys/bus/platform/devices -name dma_stats -exec cat '{}' ';'
# These must fail with Invalid argument and retain previous settings:
echo 0 > "$PANEL/spi_freq_hz"
echo 4294967295 > "$PANEL/spi_freq_hz"
echo 45 > "$PANEL/rotation"
echo 90 > "$PANEL/rotation"                    # default landscape mode
cat "$PANEL/spi_freq_hz" "$PANEL/rotation"     # still 20000000 / 180
echo 0 > "$PANEL/rotation"
echo 40000000 > "$PANEL/spi_freq_hz"
co5300-splash /usr/share/co5300-splash/logo.rgb565 240 120 2
reboot
```

Confirm the screen works at 20 MHz; rotation flips the entire view and the
prompt remains readable inside the rounded corners. `flush_stats` and
`dma_stats` should keep advancing with no new errors/fallbacks/timeouts.
After restoring 40 MHz, use `sh scripts/test-amoled-speed.sh` copied to the board
to compare full-frame timings with the board-tested baseline. At reboot settings
return to 40 MHz / 0; Tux appears during kernel boot, the custom splash appears
before getty, then the clean login prompt. An existing USB rootfs needs the
four splash files installed above to test the custom boot logo.

Host validation covers the actual renderer/window code at all four rotations,
sysfs bounds and redraw behavior, and the splash's centering, stride, format
checks, short writes and console restoration on failure/SIGTERM:

```sh
python3 scripts/check-co5300-windows.py linux/drivers/video/fbdev/co5300fb.c
python3 scripts/check-co5300-splash.py
cc -std=c11 -D_POSIX_C_SOURCE=200809L -Wall -Wextra -Werror -O2 \
  -o /tmp/co5300-splash tools/co5300-splash/co5300-splash.c
```

No firmware was built or hardware tested by the coding agent. The kernel host
check compiles against Linux 7.3.4 headers, including Tux and fbcon; it does not
replace the user's RISC-V build or panel verification.
