#!/usr/bin/env python3
"""Compare actual Linux and ESP-IDF CO5300 encoders with captured SPI calls.

Usage: check-co5300-idf.py IDF_SOURCE CO5300_COMPONENT
The source arguments are local references; no download or device access occurs.
Checks bytes, lanes and logical CS boundaries, not physical panel reception.
"""
import argparse
import os
from pathlib import Path
import re
import subprocess
import tempfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("idf", type=Path)
parser.add_argument("component", type=Path)
args = parser.parse_args()
repo = Path(__file__).resolve().parent.parent
linux = (repo / "linux/drivers/video/fbdev/co5300fb.c").read_text()
panel = (args.component / "esp_lcd_co5300_spi.c").read_text()
transport = (args.idf / "components/esp_lcd/spi/esp_lcd_panel_io_spi.c").read_text()
common = (args.idf / "components/esp_lcd/priv_include/esp_lcd_common.h").read_text()
ll = (args.idf / "components/esp_hal_gpspi/esp32s31/include/hal/spi_ll.h").read_text()


def block(source, name):
    masked = re.sub(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"',
                    lambda m: " " * len(m[0]), source, flags=re.S)
    match = re.search(r"^[^\n;{}]*\b" + re.escape(name) + r"\([^;{}]*\)\s*\{", masked, re.M)
    if not match:
        raise ValueError(f"Function not found: {name}")
    depth = 0
    for end in range(match.end() - 1, len(masked)):
        depth += (masked[end] == "{") - (masked[end] == "}")
        if not depth:
            return source[match.start():end + 1]
    raise ValueError(f"Unclosed function: {name}")


header = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
typedef uint8_t u8;
typedef uint16_t u16;
typedef uint32_t u32;
typedef int esp_err_t;
#define ESP_OK 0
#define ESP_RETURN_ON_ERROR(expr, ...) do { int e = (expr); if (e) return e; } while (0)
#define ESP_GOTO_ON_ERROR(expr, label, ...) do { if ((expr) != ESP_OK) goto label; } while (0)
#define __containerof(p,t,m) ((t *)((char *)(p) - offsetof(t,m)))
#define __containerof_const(p,t,m) __containerof(p,t,m)
#define BIT(n) (1U << (n))
#define SPI_TRANS_CS_KEEP_ACTIVE BIT(0)
#define SPI_TRANS_MODE_QIO BIT(1)
#define SPI_TRANS_MODE_OCT BIT(2)
#define SPI_TRANS_MULTILINE_CMD BIT(3)
#define SPI_TRANS_MULTILINE_ADDR BIT(4)
#define SPI_TRANS_DMA_USE_PSRAM BIT(5)
#define portMAX_DELAY 0xffffffffU
#define LCD_CMD_CASET 0x2a
#define LCD_CMD_RASET 0x2b
#define LCD_CMD_RAMWR 0x2c
#define LCD_OPCODE_WRITE_CMD 0x02ULL
#define LCD_OPCODE_WRITE_COLOR 0x32ULL
#define SPI_NBITS_SINGLE 1
#define SPI_NBITS_QUAD 4
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#define READ_ONCE(a) (a)
#define cond_resched() ((void)0)
#define min(a,b) ((a)<(b)?(a):(b))
#define dev_info(...) ((void)0)
struct spi_device { int unused; };
struct spi_transfer { const void *tx_buf; u32 len; u8 tx_nbits; bool cs_change; };
struct co5300fb {
    struct spi_device *spi;
    u8 *command, *pixels;
    u32 width, height, rotation;
    bool quiet_commands;
};
struct fb_info { struct co5300fb *par; char *screen_buffer; };
typedef struct { int unused; } esp_lcd_panel_io_t;
typedef esp_lcd_panel_io_t *esp_lcd_panel_io_handle_t;
typedef struct { int unused; } esp_lcd_panel_t;
typedef struct {
    esp_lcd_panel_t base;
    esp_lcd_panel_io_handle_t io;
    int x_gap, y_gap;
    u8 fb_bits_per_pixel;
    struct { bool use_qspi_interface; } flags;
} co5300_panel_t;
typedef struct {
    unsigned flags, length;
    const void *tx_buffer;
    void *user;
} spi_transaction_t;
typedef struct {
    spi_transaction_t base;
    struct { unsigned dc_gpio_level, en_trans_done_cb; } flags;
} lcd_spi_trans_descriptor_t;
typedef struct {
    esp_lcd_panel_io_t base;
    void *spi_dev;
    size_t spi_trans_max_bytes, num_trans_inflight, queue_size;
    int lcd_cmd_bits, lcd_param_bits;
    struct { bool dc_cmd_level, dc_data_level, dc_param_level;
             bool octal_mode, quad_mode, psram_dma_direct; } flags;
    lcd_spi_trans_descriptor_t trans_pool[16];
} esp_lcd_panel_io_spi_t;
typedef struct { struct { unsigned cs_keep_active; } misc; } spi_dev_t;
static void fail(const char *s) { fprintf(stderr, "FAIL: %s\n", s); exit(2); }
'''
mocks = r'''
struct trace {
    u8 bytes[411660], lanes[411660];
    size_t len, ends[3];
    unsigned groups, chunks, payload;
};
static struct trace linux_trace, idf_trace;
static struct trace *current;
static bool acquired;
static spi_transaction_t *queued[16];
static unsigned queued_count, recycled;
static void emit(const void *data, size_t len, unsigned lanes, bool keep) {
    if (current->len + len > sizeof(current->bytes)) fail("frame overrun");
    memcpy(current->bytes + current->len, data, len);
    memset(current->lanes + current->len, lanes, len);
    current->len += len;
    if (lanes == 4) {
        if (len > 32768 || (len & 1)) fail("RGB565 chunk limit/alignment");
        current->chunks++; current->payload += len;
    }
    if (!keep) {
        if (current->groups >= 3) fail("extra CS boundary");
        current->ends[current->groups++] = current->len;
    }
}
static int spi_sync_transfer(struct spi_device *spi, struct spi_transfer *t, unsigned n) {
    (void)spi;
    for (unsigned i = 0; i < n; i++) {
        if (t[i].cs_change) fail("CO5300 should not request a CS toggle inside RAMWR");
        size_t done = 0;
        do {
            size_t chunk = min(t[i].len - done, 32768);
            emit((const u8 *)t[i].tx_buf + done, chunk, t[i].tx_nbits,
                 done + chunk < t[i].len || i + 1 < n);
            done += chunk;
        } while (done < t[i].len);
    }
    return 0;
}
static int spi_device_acquire_bus(void *dev, unsigned wait) {
    (void)dev; (void)wait;
    if (acquired) fail("nested IDF bus acquisition");
    acquired = true; return 0;
}
static void spi_device_release_bus(void *dev) { (void)dev; acquired = false; }
static int capture_idf(spi_transaction_t *t) {
    if (!acquired || (t->length & 7)) fail("IDF bus ownership/bit length");
    spi_dev_t hw = {0};
    spi_ll_master_keep_cs(&hw, !!(t->flags & SPI_TRANS_CS_KEEP_ACTIVE));
    emit(t->tx_buffer, t->length / 8, (t->flags & SPI_TRANS_MODE_QIO) ? 4 : 1,
         hw.misc.cs_keep_active);
    return 0;
}
static int spi_device_polling_transmit(void *dev, spi_transaction_t *t) {
    (void)dev; return capture_idf(t);
}
static int spi_device_queue_trans(void *dev, spi_transaction_t *t, unsigned wait) {
    (void)dev; (void)wait;
    if (queued_count == ARRAY_SIZE(queued)) fail("queue overrun");
    queued[queued_count++] = t;
    return capture_idf(t);
}
static int spi_device_get_trans_result(void *dev, spi_transaction_t **t, unsigned wait) {
    (void)dev; (void)wait;
    if (recycled >= queued_count) fail("recycle before queue");
    *t = queued[recycled++]; return 0;
}
static bool esp_ptr_external_ram(const void *p) { (void)p; return false; }
static int panel_io_spi_tx_param(esp_lcd_panel_io_t *, int, const void *, size_t);
static int panel_io_spi_tx_color(esp_lcd_panel_io_t *, int, const void *, size_t);
static int esp_lcd_panel_io_tx_param(esp_lcd_panel_io_handle_t io, int cmd,
                                    const void *param, size_t len) {
    return panel_io_spi_tx_param(io, cmd, param, len);
}
static int esp_lcd_panel_io_tx_color(esp_lcd_panel_io_handle_t io, int cmd,
                                    const void *data, size_t len) {
    return panel_io_spi_tx_color(io, cmd, data, len);
}
'''
main = r'''
int main(void) {
    struct spi_device spi = {0};
    struct co5300fb p = { .spi = &spi, .width = 502, .height = 410 };
    u16 *fb = malloc(411640);
    p.pixels = malloc(411640); p.command = malloc(259);
    if (!fb || !p.pixels || !p.command) fail("allocation");
    /* Cover every RGB565 value, including red f800, green 07e0, blue 001f
     * and white ffff; a solid fill cannot detect byte/word ordering errors. */
    for (unsigned i = 0; i < 502 * 410; i++) fb[i] = (u16)i;
    struct fb_info info = { .par = &p, .screen_buffer = (char *)fb };
    co5300fb_render(&info, 0, 409);
    current = &linux_trace;
    if (co5300fb_write_window(&p, 22, 431, 0, 501, 411640)) fail("Linux window");
    esp_lcd_panel_io_spi_t io = { .spi_trans_max_bytes = 32768, .queue_size = 10,
                                 .lcd_cmd_bits = 32, .lcd_param_bits = 8,
                                 .flags = { .quad_mode = true } };
    co5300_panel_t panel = { .io = &io.base, .x_gap = 22, .fb_bits_per_pixel = 16,
                            .flags = { .use_qspi_interface = true } };
    current = &idf_trace;
    if (panel_co5300_draw_bitmap(&panel.base, 0, 0, 410, 502, p.pixels)) fail("IDF window");
    if (linux_trace.len != 411660 || idf_trace.len != linux_trace.len ||
        linux_trace.groups != 3 || idf_trace.groups != 3 ||
        linux_trace.chunks != 13 || idf_trace.chunks != 13 ||
        linux_trace.payload != 411640 || idf_trace.payload != 411640 ||
        memcmp(linux_trace.bytes, idf_trace.bytes, linux_trace.len) ||
        memcmp(linux_trace.lanes, idf_trace.lanes, linux_trace.len) ||
        memcmp(linux_trace.ends, idf_trace.ends, sizeof(linux_trace.ends)))
        fail("Linux/IDF byte stream, lanes, CS groups or segmentation differ");
    const u8 prefix[] = {2,0,0x2a,0,0,22,1,0xaf, 2,0,0x2b,0,0,0,1,0xf5, 0x32,0,0x2c,0};
    if (memcmp(linux_trace.bytes, prefix, sizeof(prefix)) || linux_trace.ends[0] != 8 ||
        linux_trace.ends[1] != 16 || linux_trace.ends[2] != 411660) fail("native crop/header");
    for (unsigned x = 0; x < 502; x++)
        for (unsigned c = 0; c < 410; c++) {
            u16 want = fb[(409 - c) * 502 + x];
            unsigned off = 20 + (x * 410 + c) * 2;
            if (idf_trace.bytes[off] != want >> 8 || idf_trace.bytes[off + 1] != (want & 255))
                fail("IDF must preserve Linux RGB565 MSB-first rotated bytes");
        }
    free(fb); free(p.pixels); free(p.command);
    puts("PASS: actual Linux and ESP-IDF encoders/transport match byte-for-byte and lane-for-lane;"
         " crop/02+32 headers/three CS groups/13 chunks/411640 bytes/all 65536 RGB565 values");
    return 0;
}
'''
program = [header, block(ll, "spi_ll_master_keep_cs"), mocks,
           block(common, "lcd_com_reverse_buffer_bytes")]
for name in ("co5300fb_command", "co5300fb_write_window", "co5300fb_render"):
    program.append(block(linux, name))
for name in ("spi_lcd_prepare_cmd_buffer", "spi_lcd_prepare_param_buffer",
             "panel_io_spi_tx_param", "panel_io_spi_tx_color"):
    program.append(block(transport, name))
for name in ("tx_param", "tx_color", "panel_co5300_draw_bitmap"):
    program.append(block(panel, name))
program.append(main)
with tempfile.TemporaryDirectory(prefix="co5300-idf-") as directory:
    source = Path(directory) / "check.c"
    exe = Path(directory) / "check"
    source.write_text("\n".join(program))
    subprocess.run([os.environ.get("CC", "cc"), "-std=gnu11", "-O1", "-g",
                    "-fsanitize=address,undefined", str(source), "-o", str(exe)], check=True)
    subprocess.run([str(exe)], check=True, env=dict(os.environ, ASAN_OPTIONS="detect_leaks=0"))
