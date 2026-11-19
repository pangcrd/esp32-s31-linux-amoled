# CO5300 AMOLED on ESP32-S31 Linux — Test Log (2026-11-19 morning)

Board: ESP32-S31 Function-CoreBoard-1 (ESP32-S31-WROOM-3-N16R16V).
Panel: Chipone CO5300 variant C05300AF-51, 410x502, RGB565, QSPI, on a
stacking shield (dupont wires; the user's ext-9 PCB has not arrived yet).
Base: annoyedmilk/esp32-s31-linux @ e27b61f (Linux 7.3.4, Buildroot),
plus the user's Step 2 (GPSPI2 clock/reset) and Phase 1 (GPSPI2 SPI
controller + spidev) work, commit 8a0e2f2 on the fork
pangcrd/esp32-s31-linux-amoled.

This log covers the morning of 2026-11-19 (Asia/Bangkok). The goal is a
Linux boot log visible on the AMOLED via a framebuffer console (fbcon).
The panel works under ESP-IDF (proven 4-5x at 20 MHz, touch OK), so
hardware and wiring are ruled out.

## What was built

An opt-in "amoled" Buildroot variant (separate output dir
`build/br-output-coreboard1-amoled`), built with
`scripts/enable-amoled.sh` + direct Buildroot make (NOT
`scripts/ci-build.sh`). It adds:

- A new framebuffer driver `linux/drivers/video/fbdev/co5300fb.c`
  (deferred I/O, one row per SPI message, CASET/RASET per row, 0x32
  header single-line + pixels quad, MSB-first).
- A new DTS `esp32s31_coreboard1_amoled.dts` (panel@0, compatible
  `chipone,co5300`, 410x502, x-offset 22, supplier init sequence,
  reset-gpios GPIO0 ACTIVE LOW, spi-max-frequency 1 MHz).
- SPI driver fixes in `linux/drivers/spi/spi-esp32s31.c` (FORCE_NORST
  cleared in the clock driver, CS_SETUP_TIME cleared to match IDF,
  deterministic USER1/USER2/INT_ENA/INT_CLR init, IRQ+polling fallback,
  USR-idle check before CS release).
- Opt-in config `linux-amoled.fragment` (BUILTIN_DTB_NAME=amoled,
  FB_CO5300=y, VT=y, FRAMEBUFFER_CONSOLE=y, console=tty0 first).

Build trap hit once: `scripts/ci-build.sh` sets `ESP32S31_VERSION`
from `make -s version`, but the direct-Buildroot sequence does not, so
`post-build.sh` failed with "ESP32S31_VERSION is empty". Fix: export
`ESP32S31_VERSION="$(make -s version)"` in the same shell before the
output-dir make.

## Test 1 — First boot of the amoled image (10:46)

Result: screen black, but the driver probed cleanly.

```
dmesg | grep -i -E 'co5300|spi|UPDATE|TRANS_DONE|USR'
[ 1.485975] co5300fb spi2.0: fb0: 410x502 RGB565 QSPI console at 1000000 Hz
```

- No UPDATE / TRANS_DONE / USR timeouts, no "initialization failed".
- `cat /proc/fb` -> `0 CO5300 QSPI`.
- `cat /proc/cmdline` has `console=tty0` first.
- `dmesg | grep -i -E 'console|fb0'`:
  ```
  [ 1.469511] Console: switching to colour frame buffer device 51x31
  [ 1.469624] fb0: sys_imageblit: framebuffer is not in virtual address space.
  [ 1.469726] fb0: sys_fillrect: framebuffer is not in virtual address space.
  ```
  fbcon DID switch to the framebuffer, but the two "not in virtual
  address space" warnings are the clue.
- During a `dd` loop writing to /dev/fb0, the user measured CLK jumping
  0.5-1.2 V and CS 0 V, so the driver IS clocking data out.

## Fix 1 — FBINFO_VIRTFB / screen_base (10:57)

Root cause: `co5300fb_probe` set `info->screen_buffer` (vmalloc) but
never set `info->screen_base` and never set `info->flags`. The
`sys_imageblit` / `sys_fillrect` ops warn exactly when
`!(flags & FBINFO_VIRTFB)` and go through `screen_base`, so fbcon text
and /dev/fb0 writes went nowhere while deferred I/O flushed rows of
zeros (the untouched vmalloc buffer) — which is why CLK was active yet
nothing appeared.

Fix (2 lines in probe, after the vmalloc check):
```c
info->screen_base = info->screen_buffer;
info->flags = FBINFO_VIRTFB;
```
Reference: mainline `ssd1307fb.c` sets `FBINFO_VIRTFB`.

Verification after rebuild/reflash (11:05-11:10):
- The "virtual address" warnings are GONE from dmesg (only the fb0
  registration line remains, no row errors).
- `grep FBINFO_VIRTFB` in the build-tree co5300fb.c hits line 314.
- `dd if=/dev/urandom of=/dev/fb0 bs=4096 count=101` wrote the full
  framebuffer: 411640 bytes (410x502x2, exact), 1.79 s, ENOSPC only on
  the last partial block (expected overflow past the end of fb0, not a
  write-path failure). `dmesg | tail -3` shows only dwmac eth0 lines.
- The board has NO python3 (minimal Buildroot rootfs), so a red-fill
  test via python was not possible; the dd test was used instead.
- Screen: STILL BLACK.

Conclusion: the write path is proven good. The failure is now at the
panel level (init / display-on), not the framebuffer/driver-write level.

## Fix 2 — Init-sequence order + 0x35 data byte (11:13)

Suspect: the driver sent 0x36/0x3A BEFORE 0xFE, and sent 0x35 with NO
data byte, while the supplier/ESP-IDF order is FE, C4, 3A, 35(00), 53,
51, 63, 11, 29. A 0x35 (TE ON) with no data byte likely leaves the
panel waiting for that byte, so the following commands (53/51/63/11/29)
are misread and the display never turns on.

Fix: init sequence now exactly the supplier order
FE(00), C4(80), 3A(55), 35(00), 53(20), 51(FF), 63(FF), 11 (600 ms)
in BOTH the DTS `chipone,init-sequence` and the driver's default table;
the separate 0x36/0x3A sends were removed from probe (0x36 dropped —
power-up default 0x00 is what ESP-IDF uses). Both sequences were
parse-validated (8 records, DTS bytes == driver defaults).

Result (11:22): screen STILL BLACK. `dmesg | grep -i -E 'co5300|fbcon'`
shows only the fb0 registration line.

## Fix 3 — RST stuck at 0 V (11:24-11:35)

Decisive measurement:
```
cat /sys/kernel/debug/gpio | grep -E "gpio-0 |LCD_RST|QSPI"
 gpio-0   (LCD_RST             |reset               ) out hi ACTIVE LOW
 gpio-35  (QSPI_IO3            )
 gpio-45  (QSPI_SCLK           )
 gpio-46  (QSPI_CS             |spi2 CS0            ) out lo ACTIVE LOW
 gpio-47  (QSPI_IO0            )
 gpio-48  (QSPI_IO1            )
 gpio-49  (QSPI_IO2            )
```
Multimeter at the panel: **RST = 0 V, always.**

"Out hi" is the LOGICAL value; for ACTIVE LOW that is physical 0 V =
reset ASSERTED. The panel was stuck in reset, so it ignored every init
command and every pixel row — which explains the black screen with zero
driver errors. The driver's `devm_gpiod_get(..., GPIOD_OUT_HIGH)` +
`gpiod_set_value_cansleep(reset, 0)` did NOT release RST on this board.
For contrast, `gpio-46 (QSPI_CS |spi2 CS0) out lo ACTIVE LOW` is
correct (logical 0 = physical 3.3 V = CS deasserted when idle), so the
gpiod core does understand ACTIVE LOW; the failure is specific to the
driver's reset path.

Fix: drive RST with RAW (physical) levels — request with `GPIOD_ASIS`,
`gpiod_direction_output_raw(reset, 0)` (physical 0 V = assert), 10 ms,
`gpiod_set_raw_value_cansleep(reset, 1)` (physical 3.3 V = release),
plus a `dev_info` readback to prove the release in dmesg.

Verification after rebuild/reflash (11:35):
- Multimeter at the panel: **RST = 3.3 V**.
- `dmesg | grep 'reset released'`:
  ```
  [ 0.707478] co5300fb spi2.0: reset released, RST raw value=1 (want 1)
  ```
- Screen: STILL BLACK.

Conclusion: the panel is now out of reset, the supplier init was sent
after the release, and the framebuffer/row path runs — yet no image.
The driver reports every transfer as success (no errors), so the
failure is now either the commands/pixels not actually reaching the
panel in the right form, or the panel not acting on them.

## Diagnostic added (11:37) — not yet run

A temporary diagnostic patch `co5300fb-diag-log.patch` (co5300fb.c
only) logs every init command (`DIAG cmd 0x.. len .. -> ret`) and the
first 3 row transfers (`DIAG row y=.. -> ret`), plus any failed row.
After the next rebuild/reflash, `dmesg | grep DIAG` will show whether
the driver really executes each init command and the row transfers and
whether any returns an error. This patch is diagnostic only and should
be removed once the cause is found.

## What is proven (do not re-test)

- Panel hardware + wiring are good (ESP-IDF firmware runs 4-5x at
  20 MHz, touch OK; panel power stable; signals measured correct at
  the panel end during transfers: CS 0 V, CLK/MOSI 1.4-1.6 V, RST
  3.3 V, IO2/IO3 0 V).
- SPI loopback PASSES (IO0 shorted to IO1 at the panel end, 16-byte
  pattern matches exactly at 1 MHz) — the Linux driver's FIFO byte
  order and bit order are proven, and the clock divider delivers the
  requested rate (131 transfers in 5 s at 1 MHz for 4096-byte
  transfers).
- The framebuffer write path is proven (full 411640-byte framebuffer
  written via dd, no errors, no "virtual address" warnings).
- RST is now proven released (3.3 V, dmesg readback raw value=1).
- WP/HD (IO2/IO3) levels are ruled out (floated, tied to 3.3 V, on
  both read and write paths — no change).
- The spidev READ path is CLOSED (the Linux GPSPI driver does not
  tri-state MOSI for RX-only transfers, so a 1-wire bidirectional
  read via spidev is impossible with this driver as-is; quad-read
  returned 7f ff ff for all commands/dummy values). Do not reopen it.
- The init sequence order and the 0x35 data byte are now the supplier
  order (proven under ESP-IDF).

## Open questions for deep analysis

1. The driver reports every `spi_sync_transfer` as success (ret 0),
   yet the panel shows nothing. Is the SPI peripheral actually
   putting the right bytes on the wire for the co5300fb driver's
   transfers (single-line commands AND the 2-transfer row message with
   a single-line header + quad pixels)? The spidev loopback proved
   single-line TX, but quad TX was never loopback-proven, and the
   row message mixes single and quad in one CS assertion.
2. Is there a CS timing / setup-hold issue specific to the GPIO-CS
   path (the ESP-IDF reference uses the peripheral's native CS)?
   The driver clears CS_SETUP_TIME to 0 (matching IDF) — is that
   right for this panel, or does the panel need a non-zero setup time?
3. Does the panel need 0x2A/0x2B (CASET/RASET) sent once during init,
   or a longer delay after reset release / after 0x11 before 0x29?
   The driver waits 150 ms after reset release and 600 ms after 0x11.
4. Is the 0xC4=0x80 command switching the panel into a mode that the
   subsequent single-line commands do not match (e.g. the panel
   expects the init commands in a different wire mode after 0xC4)?
5. Could the deferred-I/O row flush be sending rows that the panel
   discards (e.g. because the window set by CASET/RASET per row is
   wrong, or because the 0x32 pixel header is not accepted in the
   same CS assertion as the preceding CASET/RASET commands)?

## Files changed this morning (to be pushed)

- `linux/drivers/video/fbdev/co5300fb.c` (new driver; includes the
  screenbase fix, the init-seq fix, the reset-raw fix, and the
  diagnostic logging)
- `linux/arch/riscv/boot/dts/espressif/esp32s31_coreboard1_amoled.dts`
  (new; geometry 410x502/x-offset 22 + supplier init sequence)
- `linux/drivers/spi/spi-esp32s31.c` (Claude's SPI fixes + CS_SETUP fix)
- `linux/drivers/clk/clk-esp32s31.c` (FORCE_NORST removed)
- `linux/arch/riscv/boot/dts/espressif/esp32s31_coreboard1.dts`
  (spidev 1 MHz + bus-width 4)
- `linux/arch/riscv/boot/dts/espressif/Makefile` (amoled dtb)
- `linux/patches/0026-fbdev-co5300-qspi-console.patch` (new)
- `linux/Documentation/devicetree/bindings/display/chipone,co5300.yaml` (new)
- `br2-external/board/esp32s31/linux-amoled.fragment` (new)
- `scripts/enable-amoled.sh` (new)
- `docs/amoled-spi-debug.md` (Claude's report, corrected to 410x502)
- `README.md` (pointer to the debug doc)
- `docs/test_log.md` (this file)




## Follow-up source analysis: CO5300 two-row window requirement

See [co5300-black-screen-analysis.md](co5300-black-screen-analysis.md).
The datasheet (pp. 159/161) requires even window extents; main sends
RASET start == end for every row. A patch against main 431a5a8 sends
251 valid two-row windows, preserving the full 411640-byte framebuffer.
Host regression and Linux 7.3.4 compilation pass. Board recovery after
this patch is not yet verified; no earlier measured results were re-tested.
