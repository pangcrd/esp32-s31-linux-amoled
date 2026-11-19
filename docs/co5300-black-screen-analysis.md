# CO5300 black screen: invalid one-row RASET windows

Analyzed against upstream fork main **431a5a89124da346ff56bf9efc1d0503bd99c923**.
The fix preserves that revision's framebuffer, raw-reset and supplier
initialization corrections. The portable patches target main 431a5a8;
the driver on the fix branch already includes both patches.

Read: docs/user_test_log.md and main's docs/amoled-spi-debug.md.
docs/test_log.md does not exist in either checkout; the supplied morning log
is present as docs/user_test_log.md. main-color-test-clean.cpp is absent
from the workspace and latest main. Its location was requested. Exact
application-specific comparison of its drawing calls and brightness call
cannot be claimed without that file.

## Root-cause defect found

The CO5300 datasheet V0.00 requires an even start and an even extent for
the GRAM write window:

- Page 159, CASET: column start and (end - start + 1) divisible by 2.
- Page 161, RASET: row window extent (EP - SP + 1) divisible by 2.
  The row-start restriction has a typographical error in its variable
  name, but its parity requirement and the extent formula are explicit.

Main's co5300fb_row() programs RASET start == end for **every** row.
Thus every window has height 1, violating the RASET restriction; odd row
starts also violate alignment. This is a concrete protocol defect and the
leading explanation for the black screen, rather than another clock,
reset or framebuffer hypothesis. Physical screen recovery after the patch
still needs the user's board run.

The bus/controller can finish transmitting an invalid panel command.
SPI has no acknowledgment of CO5300 command acceptance; ret=0 cannot
establish that the panel accepted its write window.

| Transfer | Main before fix | Corrected transfer |
| --- | --- | --- |
| CASET | 22..431, width 410: valid | Unchanged |
| First RASET | 0..0, height 1: invalid | 0..1, height 2 |
| Next RASET | 1..1, height 1: invalid | 2..3, height 2 |
| Last RASET | 501..501, height 1: invalid | 500..501, height 2 |
| RGB565 payload per window | 820 bytes | 1,640 bytes |
| Windows per full framebuffer | 502 invalid windows | 251 valid windows |
| Total framebuffer payload | 411,640 bytes | 411,640 bytes |

## Concrete fix

[patches/co5300-two-row-windows.patch](../patches/co5300-two-row-windows.patch)
changes only co5300fb.c:

1. Program RASET y..y+1.
2. Copy and send both adjacent rows; allocate a 1,640-byte pixel buffer.
3. Round dirty row ranges outward to pairs, then increment y by 2.
   The unchanged neighbouring row is read from the existing framebuffer.
4. Reject odd geometry/offsets because those cannot produce valid aligned
   full-width two-row windows with this transport. The proven
   410x502 / offset 22,0 configuration already satisfies this.

The same deferred-I/O mechanism, two-transfer RAMWR message, GPIO CS,
single-lane command header, quad pixel payload and RGB565 conversion stay
in use. No DTS/config change is required for the proven configuration.

[patches/co5300-window-diag.patch](../patches/co5300-window-diag.patch)
is a separate diagnostic patch. It logs the actual encoded RASET
start/end, payload length and transfer lane widths for the first three
windows, plus init command bytes. It stops logging every CASET/RASET:
printk on every row feeds messages back into fbcon, schedules more damage,
and can create a continuing redraw/logging loop. That logging issue is
secondary; the screen was already black before the DIAG patch was added.

## Byte stream comparison

The IDF panel-I/O and SPI HAL sources were read at the project's pinned
fff9895c82d744c7237be8847347bdd1b07c6643 revision. The CO5300 vendor
driver was read from Espressif esp-iot-solution at f80a3a20dfba257287fb2712541bae755dec8c97. The app-specific
main-color-test-clean.cpp was unavailable, as noted above.

For an IDF command value (0x02 << 24) | (cmd << 8), the little-endian
integer is byte-reversed by spi_lcd_prepare_cmd_buffer(), producing
02 00 cmd 00 in FIFO/wire order. Linux builds those same four bytes
explicitly. Eight-bit command parameters are not reversed by IDF.

Main's supplier sequence produces:

| Command | Bytes on the intended single-lane TX stream | Delay after command |
| --- | --- | ---: |
| FE | 02 00 FE 00 00 | 0 ms |
| C4 | 02 00 C4 00 80 | 0 ms |
| 3A | 02 00 3A 00 55 | 0 ms |
| 35 | 02 00 35 00 00 | 0 ms |
| 53 | 02 00 53 00 20 | 0 ms |
| 51 | 02 00 51 00 FF | 0 ms |
| 63 | 02 00 63 00 FF | 0 ms |
| 11 | 02 00 11 00 | 600 ms |
| 29 | 02 00 29 00 | 0 ms |

This table matches the framing and supplier order supplied in the user's
proven test description. The patch changes none of those commands.

Corrected first-window transfer:

```text
CS assertion A:
  IO0: 02 00 2A 00 00 16 01 AF          CASET 22..431
CS assertion B:
  IO0: 02 00 2B 00 00 00 00 01          RASET 0..1
CS assertion C:
  IO0: 32 00 2C 00                       32 clocks: 8-bit opcode + 24-bit address
  IO0..3: 1,640 RGB565 bytes             3,280 clocks
```

Each RGB565 pixel is sent high byte then low byte, in row-major order.
For example, native framebuffer red 0xF800 becomes F8 00, or quad
nibbles F,8,0,0. The first row's 820 bytes are immediately followed by
the second row's 820 bytes within the same CS assertion.

| Detail | IDF reference | Linux main | Assessment |
| --- | --- | --- | --- |
| Register header | 02 00 cmd 00, single lane | Same | Match |
| Register payload | 8-bit parameters, single lane | Same | Match |
| Header/parameter transaction boundary | Separate polling transfers with native CS held | One combined transfer | Same bytes and CS framing; different clock-gap placement |
| Pixel header | 32 00 2C 00, single lane | Same | Match |
| Pixel data | QIO / four lanes | tx_nbits=4 | Descriptor-level match; actual quad electrical output has not been proven |
| Pixel bit length | byte_count * 8, hardware handles four lanes | chunk * 8 - 1 in MS_DLEN | Same encoding; do not divide bit length by four again |
| Pixel memory order | Supplied buffer sent in byte order | Native RGB565 explicitly converted to big-endian bytes | Correct for RGB565; exact app buffer contents require the missing CPP |
| Write window | Caller rectangle; valid windows must have even extents | Always height 1 | **Definite datasheet violation; fixed** |
| Clock | User describes 40 MHz clean-test setup / measured good 20 MHz | 1 MHz | Different rate, retained as requested; datasheet specifies write-clock minima, not a minimum frequency |
| Brightness helper | Espressif set_brightness(100) sends 51 FF | Init sends 51 FF | Same value for 100%; app's actual value and timing of that call require the missing CPP |

### Quad and CS register differences flagged

IDF spi_ll_master_set_line_mode() sets BOTH CTRL.FREAD_QUAD and
USER.FWRITE_QUAD for data_lines=4, even for TX-only transactions.
Linux sets FWRITE_QUAD for quad TX but leaves FREAD_QUAD clear when
rx_nbits is zero. That is a real register-image difference. The register
documentation describes FREAD_QUAD as a read-data selector, so the
difference alone does not prove malformed quad TX. It is not bundled
into the window fix as a claimed second root cause.

IDF retains native/internal CS across header and color transfers using
SPI_TRANS_CS_KEEP_ACTIVE, and retains it across intermediate chunks.
Linux's GPIO descriptor retains physical CS across the whole message,
but its internal CS5 KEEP_ACTIVE is disabled by the !gpio_cs condition.
That is another real difference; it is not evidence that physical CS
rises on GPIO46. The SPI core asserts GPIO CS before transfer_one() and
releases it after the final transfer. Main waits for CMD.USR to become
idle before returning.

IDF sets native CS hold to at least one cycle (USER1.CS_HOLD_TIME=1);
Linux sets the CS_HOLD enable with a zero hold count, and physical
GPIO CS timing is controlled by the core. Datasheet page 107 requires
10 ns CS setup/hold. The software ordering contains time before the
first SCLK and after completion, so there is no source-level basis to
claim insufficient GPIO CS setup as the current root cause. No CS route
or timing change is needed to fix the invalid RASET window.

The existing DIAG logs showing ret=0 cannot prove correct quad nibble
output. They should not be presented as such.

### C4 and delays

Datasheet page 216 identifies C4 bit 7 as SPI_WRAM: 1 enables SPI SRAM
writes. It does not switch all later commands into quad/QPI mode.
The separate interface instruction 0x38 enters QPI (page 20).
Therefore C4=80 followed by single-lane 02-prefixed commands is
consistent with the documented protocol and IDF reference.

Page 116 requires at least 5 ms from reset release to commands, and
120 ms before Sleep Out after reset. Page 146 gives 120 ms for the
Sleep Out sequence. The tested 150 ms post-reset and 600 ms after 11h
already exceed those requirements. They are preserved, with no new
delay experiments suggested.

## Verification

- Regression harness executes the actual command encoder, row encoder
  and deferred-I/O flush functions extracted from the selected source.
  The mock SPI receiver checks the datasheet constraints and captures
  command/pixel descriptors. It is not a peripheral or panel emulator.
- Unmodified main fails deterministically:
  Invalid RASET: start=0 end=0 extent=1.
- Functional patch and functional+diagnostic patch both pass:
  251 valid windows, exact 411,640-byte payload, single/quad transfer
  widths, unchanged-neighbour updates, bottom row and straddling damage.
  AddressSanitizer and UndefinedBehaviorSanitizer also pass.
- Patched co5300fb.o compiles under Linux 7.3.4 Kbuild with W=1.
- Both patches apply cleanly to main 431a5a8 and reproduce the tested
  driver byte-for-byte. Both pass checkpatch with 0 errors / 0 warnings.
- No board boot after this fix has been performed here.

Build-host regression command:

```sh
python3 scripts/check-co5300-windows.py linux/drivers/video/fbdev/co5300fb.c
```

The script lives in this workspace. A separate checkout needs a copy of
that script if you want to reproduce the host test. It is not needed
on the ESP32's minimal rootfs.

## Apply and collect one boot result

The working driver in this workspace already includes both patches. For
a fresh checkout of main 431a5a8, apply them and rebuild the existing AMOLED
output. Re-extraction is needed so Buildroot copies the new driver:

```sh
git apply patches/co5300-two-row-windows.patch
git apply patches/co5300-window-diag.patch
export ESP32S31_VERSION="$(make -s version)"
amoled_out="$PWD/build/br-output-coreboard1-amoled"
sh scripts/enable-amoled.sh "$amoled_out"
make -C external/buildroot O="$amoled_out" BR2_EXTERNAL="$PWD/br2-external"
```

Use your existing image assembly and flash procedure. On the board,
collect only software logs:

```sh
dmesg | grep -E 'DIAG|co5300|UPDATE|TRANS_DONE|USR'
```

Expected first three window lines:

```text
DIAG row y=0 xoff=22 RASET=0..1 bytes=1640 lanes=1/4 -> 0
DIAG row y=2 xoff=22 RASET=2..3 bytes=1640 lanes=1/4 -> 0
DIAG row y=4 xoff=22 RASET=4..5 bytes=1640 lanes=1/4 -> 0
```

These prove the running image uses paired windows, the full two-row
buffer and the intended transfer lane widths. They do not prove actual
pad waveforms or panel acknowledgment. The decisive acceptance result is
visible Linux text on the panel. An old y=0/1/2 or bytes=820 result means
the old single-row driver is still being used.

No additional hardware measurements, wiring changes, read-path tests,
reset experiments, init-order experiments or framebuffer-write retests
are part of this plan.

## Source references

- [Chipone CO5300 Datasheet V0.00](https://dl.espressif.com/AE/esp-iot-solution/CO5300_Datasheet_V0.00.pdf):
  pp. 20–21 framing, 107 CS timing, 116 reset timing,
  145–146 Sleep Out, **159/161 window alignment**, 216 C4 SPI_WRAM.
- [Pinned IDF SPI panel I/O](https://github.com/espressif/esp-idf/blob/fff9895c82d744c7237be8847347bdd1b07c6643/components/esp_lcd/spi/esp_lcd_panel_io_spi.c)
- [Pinned IDF SPI LL](https://github.com/espressif/esp-idf/blob/fff9895c82d744c7237be8847347bdd1b07c6643/components/esp_hal_gpspi/esp32s31/include/hal/spi_ll.h)
- [Pinned IDF SPI HAL](https://github.com/espressif/esp-idf/blob/fff9895c82d744c7237be8847347bdd1b07c6643/components/esp_hal_gpspi/spi_hal_iram.c)
- [Espressif CO5300 vendor driver](https://github.com/espressif/esp-iot-solution/blob/f80a3a20dfba257287fb2712541bae755dec8c97/components/display/lcd/esp_lcd_co5300/esp_lcd_co5300_spi.c)
