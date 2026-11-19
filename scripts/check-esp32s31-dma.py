#!/usr/bin/env python3
"""Host regression for actual AXI GDMA descriptor/lifecycle and SPI TX-SG code.
Fake MMIO validates software contracts, not silicon timing or panel output.
"""
import argparse
import os
from pathlib import Path
import re
import subprocess
import tempfile

parser = argparse.ArgumentParser()
parser.add_argument("gdma", type=Path)
parser.add_argument("spi", type=Path)
args = parser.parse_args()


def extract(source, marker):
    masked = re.sub(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"',
                    lambda m: " " * len(m.group()), source, flags=re.S)
    start = source.index(marker)
    opening = masked.index("{", start)
    depth = 0
    for end in range(opening, len(masked)):
        depth += (masked[end] == "{") - (masked[end] == "}")
        if not depth:
            return source[start:end + 1] + (";" if marker.startswith("struct ") else "")
    raise ValueError(marker)


gdma = args.gdma.read_text()
spi = args.spi.read_text()
defines = "\n".join(x for x in gdma.splitlines() if x.startswith("#define S31_AXI_"))
common = r"""
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
typedef uint8_t u8;
typedef uint32_t u32;
typedef uint32_t __le32;
typedef uintptr_t dma_addr_t;
typedef int64_t atomic64_t;
typedef int dma_cookie_t;
typedef int spinlock_t;
#define __iomem
#define __aligned(n)
#define BIT(n) (1U << (n))
#define GENMASK(h,l) ((~0U >> (31-(h))) & (~0U << (l)))
#define FIELD_PREP(mask,v) (((v) << __builtin_ctz(mask)) & (mask))
#define U32_MAX UINT32_MAX
#define SZ_4K 4096
#define SZ_32K 32768
#define SZ_512K 524288
#define GFP_NOWAIT 0
#define min_t(type,a,b) ((type)(a)<(type)(b)?(type)(a):(type)(b))
#define DIV_ROUND_UP(a,b) (((a)+(b)-1)/(b))
#define upper_32_bits(a) ((uint64_t)(a) >> 32)
#define lower_32_bits(a) ((uint32_t)(a))
#define cpu_to_le32(a) (a)
#define READ_ONCE(x) (x)
#define IS_ALIGNED(x,n) (!((x)&((n)-1)))
#define container_of(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))
#define struct_size(p,m,n) (sizeof(*(p))+sizeof((p)->m[0])*(n))
#define kzalloc(size, flags) calloc(1,size)
#define kfree free
#define IRQ_NONE 0
#define IRQ_HANDLED 1
typedef int irqreturn_t;
enum dma_transfer_direction { DMA_MEM_TO_DEV=1, DMA_DEV_TO_MEM=2 };
enum dma_status { DMA_COMPLETE, DMA_IN_PROGRESS, DMA_ERROR };
enum dmaengine_tx_result { DMA_TRANS_NOERROR, DMA_TRANS_READ_FAILED, DMA_TRANS_WRITE_FAILED };
enum { DMA_SLAVE_BUSWIDTH_4_BYTES=4, DMA_PREP_INTERRUPT=1, DMA_CTRL_ACK=2 };
struct device { int unused; };
struct dma_pool { int unused; };
struct clk;
struct completion { bool signalled; };
struct dma_chan { void *owner; dma_cookie_t completed; };
struct dma_device { struct device *dev; };
struct dma_slave_config {
    enum dma_transfer_direction direction;
    dma_addr_t src_addr, dst_addr;
    u32 src_addr_width, dst_addr_width, src_maxburst, dst_maxburst;
    bool device_fc;
};
struct list_head { int unused; };
struct dma_async_tx_descriptor {
    struct dma_chan *chan;
    dma_cookie_t cookie;
    void (*callback)(void *);
    void *callback_param;
};
struct virt_dma_desc {
    struct dma_async_tx_descriptor tx;
    struct list_head node;
    struct { enum dmaengine_tx_result result; size_t residue; } tx_result;
};
struct virt_dma_chan {
    struct dma_chan chan;
    spinlock_t lock;
    struct virt_dma_desc *pending;
};
struct scatterlist { dma_addr_t addr; size_t len; };
struct dma_tx_state { size_t residue; };
struct of_phandle_args { int args_count; u32 args[2]; };
struct of_dma { void *of_dma_data; };
#define for_each_sg(list, sg, count, index) \
    for(index=0; index<count && ((sg)=&(list)[index],1); index++)
#define sg_dma_address(sg) ((sg)->addr)
#define sg_dma_len(sg) ((sg)->len)
#define list_del(node) ((void)(node))
static int held, alloc_count, alloc_calls, fail_alloc, callback_count, log_count;
static bool channel_get_fail;
static u32 regs[256];
static bool barrier;
static void fail(const char *s) { fprintf(stderr,"FAIL: %s\n",s); exit(2); }
#define dev_err_ratelimited(...) (log_count++)
#define spin_lock_irqsave(lock, flags) ((void)(lock), (flags)=0, held++)
#define spin_unlock_irqrestore(lock, flags) ((void)(lock), (void)(flags), held--)
#define spin_lock_irq(lock) ((void)(lock), held++)
#define spin_unlock_irq(lock) ((void)(lock), held--)
#define lockdep_assert_held(lock) ((void)(lock))
static void dma_wmb(void) { barrier=true; }
static u32 readl(void *p) { return *(u32 *)p; }
static void writel(u32 v,void *p) { *(u32 *)p=v; }
#define readl_poll_timeout(addr,val,cond,sleep,timeout) \
    ({ int ret=-ETIMEDOUT; \
       if(sleep || !timeout) fail("bounded zero-sleep polling"); \
       for(unsigned i=0;i<8;i++){val=readl(addr);if(cond){ret=0;break;}} ret; })
static void *dma_pool_zalloc(struct dma_pool *pool,int flags,dma_addr_t *addr) {
    (void)pool;(void)flags;
    static unsigned serial;
    alloc_calls++;
    if(fail_alloc && alloc_calls==fail_alloc) return NULL;
    void *p=calloc(1,16);
    if(p){*addr=0x2f040000+16*serial++;alloc_count++;}
    return p;
}
static void dma_pool_free(struct dma_pool *pool,void *p,dma_addr_t addr) {
    (void)pool;(void)addr;free(p);alloc_count--;
}
static struct virt_dma_desc *vchan_next_desc(struct virt_dma_chan *vc) {
    struct virt_dma_desc *vd=vc->pending;vc->pending=NULL;return vd;
}
static struct dma_async_tx_descriptor *vchan_tx_prep(struct virt_dma_chan *vc,
                                                     struct virt_dma_desc *vd,unsigned long flags) {
    (void)flags;vd->tx.chan=&vc->chan;return &vd->tx;
}
static void vchan_cookie_complete(struct virt_dma_desc *vd) {
    vd->tx.chan->completed=vd->tx.cookie;
    callback_count++;
    if(vd->tx.callback) vd->tx.callback(vd->tx.callback_param);
}
static enum dma_status dma_cookie_status(struct dma_chan *ch,dma_cookie_t cookie,
                                         struct dma_tx_state *state) {
    (void)state;return ch->completed==cookie?DMA_COMPLETE:DMA_IN_PROGRESS;
}
static struct virt_dma_desc *vchan_find_desc(struct virt_dma_chan *vc,dma_cookie_t cookie) {
    (void)vc;(void)cookie;return NULL;
}
static void dma_set_residue(struct dma_tx_state *state,size_t n) { state->residue=n; }
static struct dma_chan *dma_get_slave_channel(struct dma_chan *ch) {
    if(held) fail("allocator called with peripheral-claim spinlock held");
    return channel_get_fail?NULL:ch;
}
"""
structs = "\n".join(extract(gdma, m) for m in [
    "struct s31_axi_hw_desc {", "struct s31_axi_node {", "struct s31_axi_desc {",
    "struct s31_axi_chan {", "struct s31_axi_dma {",
])
functions = "\n".join(extract(gdma, m) for m in [
    "static struct s31_axi_chan *s31_axi_chan(",
    "static struct s31_axi_desc *s31_axi_desc(",
    "static void __iomem *s31_axi_reg(",
    "static void s31_axi_free_desc(",
    "static int s31_axi_stop(",
    "static void s31_axi_start(",
    "static bool s31_axi_valid_address(",
    "s31_axi_prep_slave_sg(",
    "static int s31_axi_config(",
    "static irqreturn_t s31_axi_irq(",
    "static enum dma_status s31_axi_tx_status(",
    "static struct dma_chan *s31_axi_xlate(",
])
# prep function's return type is on its preceding line.
functions = functions.replace("s31_axi_prep_slave_sg(", "static struct dma_async_tx_descriptor *\ns31_axi_prep_slave_sg(", 1)
setup = r"""
static struct s31_axi_dma dma;
static struct device device;
static void init_dma(void) {
    memset(&dma,0,sizeof(dma));memset(regs,0,sizeof(regs));
    dma.base=regs;dma.device.dev=&device;
    for(unsigned i=0;i<6;i++){
        struct s31_axi_chan *ch=&dma.channels[i];
        ch->dma=&dma;ch->id=i/2;ch->tx=i&1;ch->periph=U32_MAX;
        ch->vc.chan.owner=ch;ch->error_cookie=-EBUSY;
        regs[(ch->tx?S31_AXI_TX_RESET_AVAIL(ch->id):S31_AXI_RX_RESET_AVAIL(ch->id))/4]=1;
    }
}
static struct s31_axi_desc *desc_from_tx(struct dma_async_tx_descriptor *tx) {
    return container_of(tx,struct s31_axi_desc,vd.tx);
}
static void inspect(struct s31_axi_desc *desc,struct scatterlist *sg,unsigned nents,bool tx) {
    unsigned index=0;
    for(unsigned i=0;i<nents;i++){
        size_t left=sg[i].len;dma_addr_t addr=sg[i].addr;
        while(left){
            struct s31_axi_node *node=&desc->nodes[index];
            u32 ctrl=node->hw->control;
            unsigned size=ctrl&4095, length=(ctrl>>12)&4095;
            unsigned want=left>4032?4032:left;
            if(size!=want || length!=(tx?want:0) || !(ctrl&BIT(31)) ||
               node->hw->buffer!=addr || node->hw->padding || (node->dma&7))
                fail("exact LLI size/length/address/owner/alignment/RGB byte order");
            if(!!(ctrl&BIT(30))!=(tx && index+1==desc->count))
                fail("only last TX descriptor marks EOF; RX length/EOF start at zero");
            u32 next=index+1<desc->count?desc->nodes[index+1].dma:0;
            if(node->hw->next!=next)fail("complete descriptor chain");
            left-=want;addr+=want;index++;
        }
    }
    if(index!=desc->count)fail("missing/trailing descriptor");
}
"""
provider_main = r"""
int main(void) {
    init_dma();
    _Static_assert(sizeof(struct s31_axi_hw_desc)==16,"padded 8-byte aligned descriptor");
    const unsigned lengths[]={1,63,64,4031,4032,4095,4096,32768,411640,524288};
    for(unsigned t=0;t<2;t++){
        struct s31_axi_chan *ch=&dma.channels[t];ch->periph=1;
        for(unsigned i=0;i<sizeof(lengths)/sizeof(lengths[0]);i++){
            struct scatterlist sg={.addr=0x50001003,.len=lengths[i]};
            struct dma_async_tx_descriptor *tx=s31_axi_prep_slave_sg(&ch->vc.chan,&sg,1,
                ch->tx?DMA_MEM_TO_DEV:DMA_DEV_TO_MEM,0,NULL);
            if(!tx)fail("valid unencrypted PSRAM SG rejected");
            inspect(desc_from_tx(tx),&sg,1,ch->tx);
            s31_axi_free_desc(&desc_from_tx(tx)->vd);
            if(alloc_count)fail("descriptor leak");
        }
    }
    struct s31_axi_chan *ch=&dma.channels[1];
    struct scatterlist pages[9];
    for(unsigned i=0;i<9;i++)pages[i]=(struct scatterlist){0x50010000+i*8192, i==8?24:4096};
    for(unsigned nth=1;nth<=18;nth++){
        alloc_calls=0;fail_alloc=nth;
        struct dma_async_tx_descriptor *tx=s31_axi_prep_slave_sg(&ch->vc.chan,pages,9,DMA_MEM_TO_DEV,0,NULL);
        if(tx)s31_axi_free_desc(&desc_from_tx(tx)->vd);
        if(alloc_count)fail("partial allocation failure leak");
    }
    fail_alloc=0;
    struct scatterlist bads[]={{0x53ffffff,2},{0x100000000ULL,64},{0x50000000,524289},{0x50000000,0}};
    for(unsigned i=0;i<4;i++)
        if(s31_axi_prep_slave_sg(&ch->vc.chan,&bads[i],1,DMA_MEM_TO_DEV,0,NULL))
            fail("invalid/wrapped/oversized memory range");
    struct scatterlist good={0x50001000,64};
    if(s31_axi_prep_slave_sg(&ch->vc.chan,&good,1,DMA_DEV_TO_MEM,0,NULL))
        fail("TX channel accepted RX direction");
    for(unsigned error=0;error<2;error++){
        struct dma_async_tx_descriptor *tx=s31_axi_prep_slave_sg(&ch->vc.chan,&good,1,DMA_MEM_TO_DEV,0,NULL);
        tx->cookie=error+1;ch->vc.pending=&desc_from_tx(tx)->vd;
        barrier=false;s31_axi_start(ch);
        if(!barrier || readl(s31_axi_reg(ch,S31_AXI_LINK2))!=ch->active->nodes[0].dma ||
           readl(s31_axi_reg(ch,S31_AXI_INT_ENA))!=(BIT(3)|BIT(2)|BIT(4)|BIT(5)) ||
           readl(s31_axi_reg(ch,S31_AXI_PERI_SEL))!=1)
            fail("TX arm order / final EOF mask / peripheral selector");
        /* Stopping TX0 must leave RX0 untouched. */
        regs[S31_AXI_RX_BASE(0)/4+S31_AXI_CONF0/4]=0x12345670;
        writel(error?BIT(2):BIT(3),s31_axi_reg(ch,S31_AXI_INT_ST));
        if(s31_axi_irq(1,ch)!=IRQ_HANDLED || ch->active ||
           regs[S31_AXI_RX_BASE(0)/4+S31_AXI_CONF0/4]!=0x12345670)
            fail("independent TX/RX lifecycle");
        if(s31_axi_tx_status(&ch->vc.chan,tx->cookie,NULL)!=(error?DMA_ERROR:DMA_COMPLETE))
            fail("DMA error reported as success");
        s31_axi_free_desc(&desc_from_tx(tx)->vd);
    }
    regs[S31_AXI_TX_RESET_AVAIL(0)/4]=0;
    int before=callback_count;
    if(s31_axi_stop(ch)!=-ETIMEDOUT || before!=callback_count ||
       !(readl(s31_axi_reg(ch,S31_AXI_CONF0))&S31_AXI_TX_ABORT))
        fail("abort deadline must retain DMA-owned descriptors");
    init_dma();
    struct of_dma of={.of_dma_data=&dma};
    struct of_phandle_args a={.args_count=2,.args={1,1}};
    if(!s31_axi_xlate(&a,&of))fail("TX0 SPI2 claim");
    a.args[0]=3;if(s31_axi_xlate(&a,&of))fail("duplicate SPI2 TX connection");
    a.args[0]=0;if(!s31_axi_xlate(&a,&of))fail("SPI2 RX connection");
    a.args[0]=4;a.args[1]=2;channel_get_fail=true;
    if(s31_axi_xlate(&a,&of)||dma.channels[4].periph!=U32_MAX)fail("failed claim retained");
    channel_get_fail=false;if(!s31_axi_xlate(&a,&of))fail("released claim cannot be reused");
    a.args[0]=6;if(s31_axi_xlate(&a,&of))fail("invalid channel");
    if(alloc_count||held)fail("resource/lock leak");
    puts("PASS: actual GDMA SG LLIs; 512K/unaligned/tails/allocation failures;"
         " IRQ/error/abort lifecycle; independent channels; exclusive peripheral claims");
    return 0;
}
"""
spi_defines = "\n".join(x for x in spi.splitlines() if x.startswith("#define S31_SPI_"))
spi_header = r"""
struct sg_table {struct scatterlist *sgl;unsigned nents;};
struct spi_controller {void *data;};
struct spi_device {struct spi_controller *controller;};
struct spi_transfer {
    const void *tx_buf;void *rx_buf;unsigned len;
    struct sg_table tx_sg;
};
static bool dma_enable=true;
static void *spi_controller_get_devdata(struct spi_controller *c){return c->data;}
static unsigned spi_controller_xfer_timeout(struct spi_controller *c,struct spi_transfer *t){
    (void)c;(void)t;return 200;
}
static u32 spi_regs[64];
static int injection, submitted, terminated, fifo_writes;
static struct s31_axi_desc *submitted_desc;
static void atomic64_inc(atomic64_t *v){(*v)++;}
static void atomic64_add(unsigned n,atomic64_t *v){*v+=n;}
static void reinit_completion(struct completion *c){c->signalled=false;}
static void complete(struct completion *c){c->signalled=true;}
static unsigned long msecs_to_jiffies(unsigned ms){return ms;}
static unsigned wait_for_completion_timeout(struct completion *c,unsigned long ms){
    (void)ms;return c->signalled;
}
static void synchronize_irq(int irq){(void)irq;}
static int dmaengine_slave_config(struct dma_chan *ch,struct dma_slave_config *c){
    return injection==1?-EINVAL:s31_axi_config(ch,c);
}
static struct dma_async_tx_descriptor *dmaengine_prep_slave_sg(struct dma_chan *ch,
    struct scatterlist *sg,unsigned n,enum dma_transfer_direction dir,unsigned flags){
    if(injection==2)return NULL;
    struct dma_async_tx_descriptor *tx=s31_axi_prep_slave_sg(ch,sg,n,dir,flags,NULL);
    if(tx)submitted_desc=desc_from_tx(tx);
    return tx;
}
static dma_cookie_t dmaengine_submit(struct dma_async_tx_descriptor *tx){
    static unsigned cookie;submitted++;tx->cookie=++cookie;
    return injection==3?-EINVAL:tx->cookie;
}
static int dma_submit_error(dma_cookie_t cookie){return cookie<0?cookie:0;}
static void dma_async_issue_pending(struct dma_chan *chan){
    struct s31_axi_chan *ch=s31_axi_chan(chan);
    if(!(spi_regs[S31_SPI_DMA_CONF/4]&S31_SPI_DMA_TX_EN))fail("SPI AFIFO not enabled before GDMA start");
    ch->vc.pending=&submitted_desc->vd;s31_axi_start(ch);
}
static int dmaengine_terminate_sync(struct dma_chan *chan){
    struct s31_axi_chan *ch=s31_axi_chan(chan);terminated++;
    if(s31_axi_stop(ch))fail("DMA abort recovery");
    ch->active=NULL;
    if(submitted_desc){s31_axi_free_desc(&submitted_desc->vd);submitted_desc=NULL;}
    return 0;
}
static enum dma_status dma_async_is_tx_complete(struct dma_chan *ch,dma_cookie_t cookie,void *a,void *b){
    (void)a;(void)b;return s31_axi_tx_status(ch,cookie,NULL);
}
"""
spi_helpers = r"""
static void esp32s31_spi_dma_complete(void *p){complete(p);}
static void esp32s31_spi_target_reset(struct esp32s31_spi *s){
    writel(0,s->base+S31_SPI_CMD);
}
static int esp32s31_spi_wait_transaction(struct esp32s31_spi *s,unsigned timeout){
    (void)timeout;
    if(injection==4)return -ETIMEDOUT;
    struct s31_axi_chan *ch=s31_axi_chan(s->tx_dma);
    if(spi_regs[S31_SPI_MS_DLEN/4]!=submitted_desc->len*8-1 ||
       submitted_desc->len>32768 || !(spi_regs[S31_SPI_USER/4]&S31_SPI_FWRITE_QUAD))
        fail("SPI data bit length / quad width");
    if(injection!=5){
        writel(injection==6?BIT(2):BIT(3),s31_axi_reg(ch,S31_AXI_INT_ST));
        s31_axi_irq(1,ch);
    }
    if(injection==7)spi_regs[S31_SPI_INT_RAW/4]=S31_SPI_MST_TX_AFIFO_REMPTY_ERR;
    return 0;
}
"""
spi_main = r"""
int main(void){
    init_dma();struct s31_axi_chan *ch=&dma.channels[1];ch->periph=1;
    struct esp32s31_spi s={.base=spi_regs,.tx_dma=&ch->vc.chan,
                          .rx_dma=&dma.channels[0].vc.chan,.dev=&device};
    struct spi_controller ctlr={.data=&s};struct spi_device dev={.controller=&ctlr};
    struct scatterlist pages[8];struct spi_transfer t={.tx_buf=(void *)0x1000};
    unsigned left=411640,total=0,transfers=0;
    while(left){
        unsigned len=left>32768?32768:left,nents=(len+4095)/4096;
        for(unsigned i=0;i<nents;i++)
            pages[i]=(struct scatterlist){0x50001000+i*8192,i+1<nents?4096:len-i*4096};
        t.len=len;t.tx_sg=(struct sg_table){pages,nents};
        memset(spi_regs,0,sizeof(spi_regs));spi_regs[S31_SPI_USER/4]=S31_SPI_FWRITE_QUAD;
        if(!esp32s31_spi_can_dma(&ctlr,&dev,&t))fail("aligned TX-SG was rejected");
        if(esp32s31_spi_dma_tx_sg(&s,&ctlr,&t,0))fail("TX-SG returned error");
        inspect(submitted_desc,pages,nents,true);
        s31_axi_free_desc(&submitted_desc->vd);submitted_desc=NULL;
        total+=len;left-=len;transfers++;
        if(alloc_count || spi_regs[S31_SPI_DMA_CONF/4]&S31_SPI_DMA_TX_EN)
            fail("DMA resource or AFIFO enable leaked after success");
    }
    if(total!=411640 || transfers!=13 || s.dma_tx_transfers!=13 ||
       s.dma_tx_bytes!=411640 || s.dma_errors)
        fail("full-frame segmentation/counters");
    for(injection=1;injection<=7;injection++){
        t.len=256;pages[0]=(struct scatterlist){0x50001000,256};
        t.tx_sg=(struct sg_table){pages,1};memset(spi_regs,0,sizeof(spi_regs));
        spi_regs[S31_SPI_USER/4]=S31_SPI_FWRITE_QUAD;
        int ret=esp32s31_spi_dma_tx_sg(&s,&ctlr,&t,0);
        if(ret>=0 || alloc_count || submitted_desc || s.dma_errors!=injection)
            fail("DMA failures must propagate and release descriptors");
    }
    injection=0;dma_enable=false;
    if(esp32s31_spi_can_dma(&ctlr,&dev,&t))fail("PIO fallback parameter ignored");
    dma_enable=true;t.rx_buf=(void *)1;
    if(esp32s31_spi_can_dma(&ctlr,&dev,&t))fail("RX incorrectly mapped as TX-SG");
    t.rx_buf=NULL;t.tx_buf=(void *)0x1001;
    if(esp32s31_spi_can_dma(&ctlr,&dev,&t))fail("unaligned TX fallback");
    if(esp32s31_spi_max_transfer_size(&dev)!=32768)fail("MS_DLEN cap");
    s.target=true;
    if(esp32s31_spi_max_transfer_size(&dev)!=4096)fail("target bounce buffer cap");
    puts("PASS: actual SPI TX-SG + GDMA chain; 13 transfers/411640 bytes;"
         " quad/MS_DLEN; DMA error propagation/cleanup; counters; PIO fallback");
    return 0;
}
"""
programs = [
    "\n".join([common, defines, structs, functions, setup, provider_main]),
    common + "\n" + defines + "\n" + structs + "\n" + functions + "\n" + setup + "\n" + spi_defines + "\n" + spi_header + "\n" +
    extract(spi, "struct esp32s31_spi {") + spi_helpers +
    extract(spi, "static size_t esp32s31_spi_max_transfer_size(") +
    extract(spi, "static bool esp32s31_spi_can_dma(") +
    extract(spi, "static int esp32s31_spi_dma_tx_sg(") + spi_main,
]
with tempfile.TemporaryDirectory(prefix="s31-dma-test-") as temp:
    for i, program in enumerate(programs):
        path = Path(temp) / f"test{i}.c"
        exe = Path(temp) / f"test{i}"
        path.write_text(program)
        subprocess.run([os.environ.get("CC", "cc"), "-std=gnu11", "-O1", "-g",
                        "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                        str(path), "-o", str(exe)], check=True)
        subprocess.run([str(exe)], check=True,
                       env=dict(os.environ, ASAN_OPTIONS="detect_leaks=0"))
