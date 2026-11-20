#!/usr/bin/env python3
"""Exercise the actual PIO transfer functions with bounded fake MMIO.
Host-only; validates FIFO bytes, control sequencing and path selection.
Real controller latency and panel behaviour still require the board test.
"""
import argparse
import os
from pathlib import Path
import re
import subprocess
import tempfile

parser = argparse.ArgumentParser()
parser.add_argument("source", type=Path)
args = parser.parse_args()
source = args.source.read_text()
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


defines = "\n".join(line for line in source.splitlines()
                    if line.startswith("#define S31_SPI_"))
header = r"""
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
typedef uint8_t u8;
typedef uint32_t u32;
typedef uintptr_t dma_addr_t;
typedef int64_t atomic64_t;
#define __iomem
#define BIT(n) (1U << (n))
#define GENMASK(h, l) ((~0U >> (31-(h))) & (~0U << (l)))
#define FIELD_PREP(mask, val) (((val) << __builtin_ctz(mask)) & (mask))
#define DIV_ROUND_UP(a,b) (((a)+(b)-1)/(b))
#define clamp_t(type,v,l,h) ((v)<(l)?(l):((v)>(h)?(h):(v)))
#define min_t(type,a,b) ((type)(a)<(type)(b)?(type)(a):(type)(b))
#define max_t(type,a,b) ((type)(a)>(type)(b)?(type)(a):(type)(b))
#define SZ_4K 4096
#define SZ_32K 32768
#define SZ_512K 524288
#define READ_ONCE(x) (x)
#define SPI_CPOL BIT(0)
#define SPI_CPHA BIT(1)
#define SPI_CS_HIGH BIT(2)
#define SPI_LSB_FIRST BIT(3)
struct completion { int unused; };
struct dma_chan { int unused; };
struct device { int unused; };
struct clk { int unused; };
struct spi_controller {
    void *data;
    unsigned int num_chipselect;
};
struct spi_device {
    struct spi_controller *controller;
    u32 mode, max_speed_hz;
    bool gpio_cs;
};
struct spi_transfer {
    const void *tx_buf;
    void *rx_buf;
    unsigned int len, speed_hz, effective_speed_hz;
    unsigned int tx_nbits, rx_nbits;
    bool cs_change, last, tx_sg_mapped;
};
static void *spi_controller_get_devdata(struct spi_controller *c) { return c->data; }
static u32 spi_get_chipselect(struct spi_device *spi, unsigned int n) {
    (void)spi; (void)n; return 0;
}
static bool spi_get_csgpiod(struct spi_device *spi, unsigned int n) {
    (void)n; return spi->gpio_cs;
}
static bool spi_transfer_is_last(struct spi_controller *c, struct spi_transfer *t) {
    (void)c; return t->last;
}
static u32 clk_get_rate(struct clk *clk) { (void)clk; return 80000000; }
static unsigned int spi_controller_xfer_timeout(struct spi_controller *c,
                                                 struct spi_transfer *t) {
    (void)c; (void)t; return 200;
}
static bool pio_poll = true;
static bool dma_enable = true;
static unsigned int syncs, chunks, legacy, rescheds, target_calls, dma_calls;
static unsigned int fail_stage, error_calls, stage, ticks, wanted_lanes;
static unsigned int soft_resets;
static bool wanted_keep_tail;
static unsigned int wanted_internal_cs = 2;
static bool error_mode;
static u32 regs[64];
static const u8 *expected;
static size_t expected_len, emitted;
static void fail(const char *s) { fprintf(stderr, "FAIL: %s\n", s); exit(2); }
#define dev_err_ratelimited(...) (error_calls++, error_mode = true)
static void synchronize_irq(int irq) { (void)irq; syncs++; }
static void cond_resched(void) { rescheds++; }
static u32 readl(void *address);
static void writel(u32 value, void *address);
/* Bounded emulated poll, with explicit verification that sleep_us stays zero. */
#define readl_poll_timeout(addr, val, condition, sleep_us, timeout_us) \
    ({ int rc = -ETIMEDOUT; \
       if ((sleep_us) != 0 || !(timeout_us)) fail("poll must have a real deadline and no sleep"); \
       for (unsigned int attempt=0; attempt<16; attempt++) { \
           (val)=readl(addr); if (condition) { rc=0; break; } \
       } rc; })
"""
mmio = r"""
static void emit(void) {
    unsigned int len = (regs[S31_SPI_MS_DLEN/4] + 1)/8;
    if (len > 64 || emitted + len > expected_len) fail("FIFO length");
    const u8 *fifo = (const u8 *)&regs[S31_SPI_W0/4];
    if (expected && memcmp(fifo, expected + emitted, len))
        fail("FIFO byte order / tail / unaligned source");
    if (wanted_lanes &&
        !!(regs[S31_SPI_USER/4] & S31_SPI_FWRITE_QUAD) != (wanted_lanes == 4))
        fail("single / quad lane configuration");
    bool keep = emitted + len < expected_len || wanted_keep_tail;
    if (!!(regs[S31_SPI_MISC/4] & S31_SPI_CS_KEEP_ACTIVE) != keep ||
        (regs[S31_SPI_MISC/4] & S31_SPI_CS_DIS_MASK) !=
        (S31_SPI_CS_DIS_MASK & ~BIT(wanted_internal_cs)))
        fail("internal CS must survive header/data and FIFO boundaries, including GPIO CS");
    if (len % 4) {
        for (unsigned int i=len; i<(len+3)/4*4; i++)
            if (fifo[i]) fail("short FIFO tail must be zero-padded");
    }
    emitted += len;
    chunks++;
}
static u32 readl(void *address) {
    unsigned int off = (u8 *)address - (u8 *)regs;
    if (off == S31_SPI_CMD && stage == 1 && fail_stage != 1) {
        regs[off/4] &= ~S31_SPI_CMD_UPDATE;
        stage = 0;
    }
    if (off == S31_SPI_INT_RAW && stage == 2 && fail_stage != 2 && ++ticks >= 2) {
        emit();
        regs[off/4] |= S31_SPI_TRANS_DONE;
        stage = 3;
    }
    if (off == S31_SPI_CMD && stage == 2 && !error_mode)
        fail("USR must not be polled idle before RAW completion");
    if (off == S31_SPI_CMD && stage == 3 && fail_stage != 3 && ++ticks >= 4) {
        regs[off/4] &= ~S31_SPI_CMD_USR;
        stage = 0;
    }
    return regs[off/4];
}
static void writel(u32 value, void *address) {
    unsigned int off = (u8 *)address - (u8 *)regs;
    if (off == S31_SPI_INT_CLR) {
        regs[S31_SPI_INT_RAW/4] &= ~value;
        return;
    }
    if (off == S31_SPI_SLAVE && (value & S31_SPI_SOFT_RESET)) {
        regs[S31_SPI_CMD/4] = 0;
        stage = 0;
        soft_resets++;
    }
    if (off == S31_SPI_CMD) {
        if (value == S31_SPI_CMD_UPDATE) {
            if (regs[off/4] & S31_SPI_CMD_USR)
                fail("UPDATE before previous shifter idle");
            stage = 1;
        } else if (value == S31_SPI_CMD_USR) {
            if (stage || (regs[S31_SPI_INT_RAW/4] & S31_SPI_TRANS_DONE))
                fail("USR before UPDATE idle / stale completion");
            stage = 2; ticks = 0;
        }
    }
    regs[off/4] = value;
}
"""
stub = r"""
static int esp32s31_spi_wait_transaction(struct esp32s31_spi *s,
                                        unsigned int timeout) {
    (void)s; (void)timeout;
    legacy++;
    emit();
    return 0;
}
static int esp32s31_spi_target_transfer(struct spi_controller *c,
                                        struct spi_device *s, struct spi_transfer *t) {
    (void)c; (void)s; (void)t; target_calls++; return 0;
}
static int esp32s31_spi_dma_transfer(struct esp32s31_spi *s, struct spi_controller *c,
                                    struct spi_transfer *t, u32 misc) {
    (void)s; (void)c; (void)t;
    if (!!(misc & S31_SPI_CS_KEEP_ACTIVE) != wanted_keep_tail ||
        (misc & S31_SPI_CS_DIS_MASK) != (S31_SPI_CS_DIS_MASK & ~BIT(wanted_internal_cs)))
        fail("DMA must retain the same internal CS transaction boundaries as PIO");
    dma_calls++; return 0;
}
static int esp32s31_spi_dma_tx_sg(struct esp32s31_spi *s, struct spi_controller *c,
                                  struct spi_transfer *t, u32 misc) {
    return esp32s31_spi_dma_transfer(s, c, t, misc);
}
static int esp32s31_spi_dma_duplex_transfer(struct esp32s31_spi *s, struct spi_controller *c,
                                          struct spi_transfer *t, u32 misc) {
    return esp32s31_spi_dma_transfer(s, c, t, misc);
}
"""
main = r"""
static void reset_capture(const u8 *bytes, unsigned int len) {
    memset(regs, 0, sizeof(regs));
    /* Simulate a stale RAW completion: fast path must clear it. */
    regs[S31_SPI_INT_RAW/4] = S31_SPI_TRANS_DONE;
    syncs = chunks = legacy = rescheds = stage = ticks = 0;
    error_calls = soft_resets = emitted = target_calls = dma_calls = 0;
    expected = bytes; expected_len = len; error_mode = false;
}
int main(void) {
    struct esp32s31_spi state = { .base = regs, .max_data_lines = 4, .irq = 1 };
    struct spi_controller ctlr = { .data = &state, .num_chipselect = 3 };
    struct spi_device spi = { .controller = &ctlr, .max_speed_hz = 40000000, .gpio_cs = true };
    u8 *storage = malloc(411640 + 8), *rx = malloc(128);
    if (!storage || !rx) fail("allocation");
    for (unsigned int i=0; i<411640+8; i++) storage[i] = (u8)(i*73 + i/13);
    struct spi_transfer t = { .tx_buf = storage, .len = 4, .tx_nbits = 1, .last = true };
    const unsigned int lengths[] = {1,2,3,4,15,16,31,63,64,65,127,128,4095,4096,4097,411640};
    for (unsigned int lane=1; lane<=4; lane+=3) {
        wanted_lanes = lane;
        t.tx_nbits = lane;
        for (unsigned int align=0; align<4; align++) {
            t.tx_buf = storage + align;
            for (unsigned int n=0; n<sizeof(lengths)/sizeof(lengths[0]); n++) {
                t.len = lengths[n];
                reset_capture(t.tx_buf,t.len);
                if (esp32s31_spi_transfer_one(&ctlr,&spi,&t)) fail("fast TX return");
                if (emitted != t.len || chunks != (t.len+63)/64 ||
                    syncs != 1 || legacy || error_calls ||
                    rescheds != ((t.len+63)/64)/64 ||
                    (regs[S31_SPI_CMD/4] & S31_SPI_CMD_USR) ||
                    (regs[S31_SPI_INT_RAW/4] & S31_SPI_TRANS_DONE))
                    fail("fast FIFO completion / transfer-wide IRQ sync / reschedule");
            }
        }
    }
    /* Match the SPI API at transfer boundaries for GPIO and native CS:
     * intermediate cs_change toggles CS; final cs_change retains it. */
    for (unsigned int gpio=0; gpio<2; gpio++) {
        spi.gpio_cs = gpio; wanted_internal_cs = gpio ? 2 : 0;
        for (unsigned int last=0; last<2; last++) {
            for (unsigned int change=0; change<2; change++) {
                t.last = last; t.cs_change = change; t.len = 137;
                wanted_keep_tail = last ? change : !change;
                reset_capture(t.tx_buf, t.len);
                if (esp32s31_spi_transfer_one(&ctlr, &spi, &t) || emitted != 137 || chunks != 3)
                    fail("CS retention across multiple FIFO chunks");
            }
        }
    }
    spi.gpio_cs = true; wanted_internal_cs = 2;
    t.last = true; t.cs_change = false; wanted_keep_tail = false;
    /* Recovery from UPDATE, TRANS_DONE or USR stalls uses the actual reset helper. */
    for (fail_stage=1; fail_stage<=3; fail_stage++) {
        t.len=64; reset_capture(t.tx_buf,t.len);
        if (esp32s31_spi_transfer_one(&ctlr,&spi,&t) != -ETIMEDOUT ||
            error_calls != 1 || soft_resets != 1 || stage ||
            regs[S31_SPI_CMD/4] || regs[S31_SPI_INT_RAW/4])
            fail("timeout recovery");
    }
    fail_stage=0;
    t.len=64;
    t.speed_hz=1000000; reset_capture(t.tx_buf,t.len);
    if (esp32s31_spi_transfer_one(&ctlr,&spi,&t) || legacy!=1 || syncs)
        fail("low-speed legacy path");
    t.speed_hz=40000000; pio_poll=false; reset_capture(t.tx_buf,t.len);
    if (esp32s31_spi_transfer_one(&ctlr,&spi,&t) || legacy!=1)
        fail("runtime fallback");
    pio_poll=true; t.rx_buf=rx; t.tx_nbits=1; t.rx_nbits=1; wanted_lanes=1;
    reset_capture(t.tx_buf,t.len);
    if (esp32s31_spi_transfer_one(&ctlr,&spi,&t) || legacy!=1)
        fail("full duplex stays on legacy path");
    t.tx_buf=NULL; wanted_lanes=0; reset_capture(NULL,t.len);
    if (esp32s31_spi_transfer_one(&ctlr,&spi,&t) || legacy!=1)
        fail("RX stays on legacy path");
    if (esp32s31_spi_max_transfer_size(&spi)!=SZ_512K)
        fail("PIO framebuffer transfer limit");
    state.target=true;
    if (esp32s31_spi_max_transfer_size(&spi)!=64) fail("target FIFO limit");
    if (esp32s31_spi_transfer_one(&ctlr,&spi,&t) || target_calls!=1)
        fail("target path");
    state.target=false; state.tx_dma=state.rx_dma=(struct dma_chan *)1;
    if (esp32s31_spi_max_transfer_size(&spi)!=S31_SPI_DMA_MAX) fail("host DMA MS_DLEN limit");
    t.tx_buf=storage; t.rx_buf=NULL; t.len=128; reset_capture(storage,t.len);
    if (esp32s31_spi_transfer_one(&ctlr,&spi,&t) || dma_calls!=1 || syncs)
        fail("DMA path preserved");
    /* Actual transfer_one selects the flags passed into SG DMA for all
     * thirteen RGB565 chunks, preserving the header-to-data transaction. */
    unsigned int left = 411640, total = 0, transfers = 0;
    t.tx_nbits = 4; wanted_lanes = 4; t.tx_sg_mapped = true;
    while (left) {
        t.len = left > 32768 ? 32768 : left;
        t.last = left == t.len; wanted_keep_tail = !t.last;
        reset_capture(storage + total, t.len);
        t.tx_buf = storage + total;
        if (esp32s31_spi_transfer_one(&ctlr, &spi, &t) || dma_calls != 1)
            fail("RGB565 SG DMA CS boundary");
        total += t.len; left -= t.len; transfers++;
    }
    if (total != 411640 || transfers != 13) fail("full RGB565 frame segmentation");
    t.tx_sg_mapped = false; wanted_keep_tail = false;
    state.tx_dma=state.rx_dma=NULL;
    t.len=SZ_512K+1;
    if (esp32s31_spi_transfer_one(&ctlr,&spi,&t)!=-EMSGSIZE)
        fail("oversize transfer rejection");
    t.len=64; t.tx_nbits=4; t.rx_buf=rx; t.rx_nbits=1;
    if (esp32s31_spi_transfer_one(&ctlr,&spi,&t)!=-EINVAL)
        fail("quad full duplex rejected");
    free(storage); free(rx);
    puts("PASS: exact full-frame/aligned/unaligned/tail FIFO bytes; single/quad;"
         " GPIO/native internal CS retention across header/FIFO/13 DMA chunks;"
         " UPDATE -> RAW -> USR; timeout reset; IRQ sync once; legacy/DMA/target limits");
    return 0;
}
"""
program = "\n".join([
    header, defines, block("struct esp32s31_spi {"), mmio, stub,
    block("static u32 esp32s31_spi_set_clock("),
    block("static size_t esp32s31_spi_max_transfer_size("),
    block("static void esp32s31_spi_target_reset("),
    block("static int esp32s31_spi_transaction_error("),
    block("static int esp32s31_spi_poll_transaction("),
    block("static int esp32s31_spi_transfer_one("), main,
])
with tempfile.TemporaryDirectory(prefix="esp32s31-pio-") as temp:
    test = Path(temp) / "test.c"
    exe = Path(temp) / "test"
    test.write_text(program)
    subprocess.run([os.environ.get("CC", "cc"), "-std=gnu11", "-O1", "-g",
                    "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                    str(test), "-o", str(exe)], check=True)
    env = dict(os.environ, ASAN_OPTIONS="detect_leaks=0")
    raise SystemExit(subprocess.run([str(exe)], env=env).returncode)
