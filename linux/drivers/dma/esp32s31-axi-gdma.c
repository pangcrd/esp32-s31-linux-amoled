// SPDX-License-Identifier: GPL-2.0-only
/*
 * ESP32-S31 AXI GDMA slave-SG support.
 *
 * Register layout and slave-SG model adapted from GrieferPig/linux-esp32-s31
 * 2bf05bc5f379066cf5f64d750afbb0b4ba0bf364. Each RX/TX half is an independent
 * Linux channel, as specified by ESP32-S31 TRM Pre-release v0.2, chapter 3.
 * Hardware descriptors live in Linux's coherent SRAM pool; payload addresses
 * come from the client's DMA mapping and may refer to non-coherent PSRAM.
 */
#include <linux/bitfield.h>
#include <linux/clk.h>
#include <linux/dma-mapping.h>
#include <linux/dmapool.h>
#include <linux/dmaengine.h>
#include <linux/interrupt.h>
#include <linux/io.h>
#include <linux/iopoll.h>
#include <linux/module.h>
#include <linux/of_dma.h>
#include <linux/platform_device.h>
#include <linux/reset.h>
#include <linux/scatterlist.h>
#include <linux/slab.h>
#include <linux/sizes.h>

#include "virt-dma.h"

#define S31_AXI_CHANNELS	6
#define S31_AXI_DESC_MAX		4032U /* <= 4095, multiple of PSRAM cache line */
#define S31_AXI_RX_BASE(n)	((n) * 0x68)
#define S31_AXI_TX_BASE(n)	(0x138 + (n) * 0x68)
#define S31_AXI_INT_ST		0x04
#define S31_AXI_INT_ENA		0x08
#define S31_AXI_INT_CLR		0x0c
#define S31_AXI_CONF0		0x10
#define S31_AXI_CONF1		0x14
#define S31_AXI_LINK1		0x20
#define S31_AXI_LINK2		0x24
#define S31_AXI_EOF_DESC		0x2c
#define S31_AXI_PERI_SEL		0x44
#define S31_AXI_RST		BIT(0)
#define S31_AXI_OWNER_CHECK	BIT(12)
#define S31_AXI_TX_AUTO_WRBACK	BIT(2)
#define S31_AXI_TX_EOF_POPPED	BIT(3)
#define S31_AXI_TX_BURST		GENMASK(7, 5)
#define S31_AXI_TX_ABORT		BIT(8)
#define S31_AXI_TX_DESC_BURST	BIT(10)
#define S31_AXI_RX_BURST		GENMASK(6, 4)
#define S31_AXI_RX_ABORT		BIT(7)
#define S31_AXI_RX_DESC_BURST	BIT(9)
#define S31_AXI_TX_STOP		BIT(0)
#define S31_AXI_TX_START		BIT(1)
#define S31_AXI_RX_STOP		BIT(1)
#define S31_AXI_RX_START		BIT(2)
#define S31_AXI_TX_EOF		BIT(1)
#define S31_AXI_TX_TOTAL_EOF	BIT(3)
#define S31_AXI_TX_ERRORS	(BIT(2) | BIT(4) | BIT(5))
#define S31_AXI_RX_EOF		BIT(1)
#define S31_AXI_RX_ERRORS	GENMASK(6, 2)
#define S31_AXI_RX_RESET_AVAIL(n) (0x28c + (n) * 4)
#define S31_AXI_TX_RESET_AVAIL(n) (0x298 + (n) * 4)
#define S31_AXI_INTR_START	0x27c
#define S31_AXI_INTR_END		0x280
#define S31_AXI_EXTR_START	0x284
#define S31_AXI_EXTR_END		0x288
#define S31_AXI_MISC		0x2a8
#define S31_AXI_MISC_CLK		BIT(4)
#define S31_AXI_DESC_SIZE	GENMASK(11, 0)
#define S31_AXI_DESC_LENGTH	GENMASK(23, 12)
#define S31_AXI_DESC_EOF		BIT(30)
#define S31_AXI_DESC_OWNER	BIT(31)

struct s31_axi_hw_desc {
	__le32 control, buffer, next, padding;
} __aligned(8);

struct s31_axi_node {
	struct s31_axi_hw_desc *hw;
	dma_addr_t dma;
};

struct s31_axi_desc {
	struct virt_dma_desc vd;
	size_t len;
	unsigned int count;
	struct s31_axi_node nodes[];
};

struct s31_axi_dma;
struct s31_axi_chan {
	struct virt_dma_chan vc;
	struct s31_axi_dma *dma;
	struct s31_axi_desc *active;
	struct dma_slave_config config;
	dma_cookie_t error_cookie;
	u32 periph;
	unsigned int id;
	bool tx;
	int irq;
};

struct s31_axi_dma {
	struct dma_device device;
	void __iomem *base;
	struct dma_pool *pool;
	/* Serialize directional channel/peripheral reservations. */
	spinlock_t request_lock;
	struct s31_axi_chan channels[S31_AXI_CHANNELS];
};

static struct s31_axi_chan *s31_axi_chan(struct dma_chan *chan)
{
	return container_of(chan, struct s31_axi_chan, vc.chan);
}

static struct s31_axi_desc *s31_axi_desc(struct virt_dma_desc *vd)
{
	return container_of(vd, struct s31_axi_desc, vd);
}

static void __iomem *s31_axi_reg(struct s31_axi_chan *ch, u32 reg)
{
	u32 base = ch->tx ? S31_AXI_TX_BASE(ch->id) : S31_AXI_RX_BASE(ch->id);

	return ch->dma->base + base + reg;
}

static void s31_axi_free_desc(struct virt_dma_desc *vd)
{
	struct s31_axi_desc *desc = s31_axi_desc(vd);
	struct s31_axi_chan *ch = s31_axi_chan(vd->tx.chan);
	unsigned int i;

	for (i = 0; i < desc->count; i++)
		dma_pool_free(ch->dma->pool, desc->nodes[i].hw, desc->nodes[i].dma);
	kfree(desc);
}

/* Called with vc.lock held. Abort bus requests before resetting/freeing LLIs. */
static int s31_axi_stop(struct s31_axi_chan *ch)
{
	u32 val, conf = readl(s31_axi_reg(ch, S31_AXI_CONF0));
	u32 avail = ch->tx ? S31_AXI_TX_RESET_AVAIL(ch->id) :
			    S31_AXI_RX_RESET_AVAIL(ch->id);

	writel(0, s31_axi_reg(ch, S31_AXI_INT_ENA));
	writel(conf | (ch->tx ? S31_AXI_TX_ABORT : S31_AXI_RX_ABORT),
	       s31_axi_reg(ch, S31_AXI_CONF0));
	writel(ch->tx ? S31_AXI_TX_STOP : S31_AXI_RX_STOP,
	       s31_axi_reg(ch, S31_AXI_LINK1));
	if (readl_poll_timeout(ch->dma->base + avail, val, val & BIT(0), 0, 1000)) {
		dev_err_ratelimited(ch->dma->device.dev, "%s%u reset unavailable\n",
				    ch->tx ? "tx" : "rx", ch->id);
		return -ETIMEDOUT;
	}
	writel(conf | S31_AXI_RST, s31_axi_reg(ch, S31_AXI_CONF0));
	writel(conf & ~S31_AXI_RST, s31_axi_reg(ch, S31_AXI_CONF0));
	writel(~0U, s31_axi_reg(ch, S31_AXI_INT_CLR));
	return 0;
}

static void s31_axi_start(struct s31_axi_chan *ch)
{
	struct virt_dma_desc *vd;
	u32 conf, burst, mask;

	lockdep_assert_held(&ch->vc.lock);
	if (ch->active)
		return;
	vd = vchan_next_desc(&ch->vc);
	if (!vd)
		return;
	list_del(&vd->node);
	ch->active = s31_axi_desc(vd);
	/* 32-byte memory bursts (4 AXI beats), below the PSRAM 128-byte limit. */
	burst = 2;
	conf = ch->tx ? S31_AXI_TX_AUTO_WRBACK | S31_AXI_TX_EOF_POPPED |
		       S31_AXI_TX_DESC_BURST | FIELD_PREP(S31_AXI_TX_BURST, burst) :
		       S31_AXI_RX_DESC_BURST | FIELD_PREP(S31_AXI_RX_BURST, burst);
	writel(conf | S31_AXI_RST, s31_axi_reg(ch, S31_AXI_CONF0));
	writel(conf, s31_axi_reg(ch, S31_AXI_CONF0));
	writel(S31_AXI_OWNER_CHECK, s31_axi_reg(ch, S31_AXI_CONF1));
	writel(ch->periph, s31_axi_reg(ch, S31_AXI_PERI_SEL));
	writel(~0U, s31_axi_reg(ch, S31_AXI_INT_CLR));
	mask = ch->tx ? S31_AXI_TX_TOTAL_EOF | S31_AXI_TX_ERRORS :
		       S31_AXI_RX_EOF | S31_AXI_RX_ERRORS;
	dma_wmb();
	writel(lower_32_bits(ch->active->nodes[0].dma),
	       s31_axi_reg(ch, S31_AXI_LINK2));
	writel(mask, s31_axi_reg(ch, S31_AXI_INT_ENA));
	writel(ch->tx ? S31_AXI_TX_START : S31_AXI_RX_START,
	       s31_axi_reg(ch, S31_AXI_LINK1));
}

static bool s31_axi_valid_address(dma_addr_t addr, size_t len)
{
	if (!len || upper_32_bits(addr) || len - 1 > U32_MAX - addr)
		return false;
	/* Default address windows in S31 AXI GDMA LL; no peripheral pointers. */
	return (addr >= 0x2f000000 && addr + len - 1 <= 0x2f07ffff) ||
	       (addr >= 0x40000000 && addr + len - 1 <= 0x53ffffff);
}

static struct dma_async_tx_descriptor *
s31_axi_prep_slave_sg(struct dma_chan *chan, struct scatterlist *sgl,
		      unsigned int nents, enum dma_transfer_direction dir,
		      unsigned long flags, void *context)
{
	struct s31_axi_chan *ch = s31_axi_chan(chan);
	struct s31_axi_desc *desc;
	struct scatterlist *sg;
	unsigned int capacity = 0, i, n = 0;
	size_t total = 0;

	if (!nents || ch->periph > 5 ||
	    dir != (ch->tx ? DMA_MEM_TO_DEV : DMA_DEV_TO_MEM))
		return NULL;
	for_each_sg(sgl, sg, nents, i) {
		size_t len = sg_dma_len(sg);

		if (!s31_axi_valid_address(sg_dma_address(sg), len) ||
		    len > SZ_512K || total > SZ_512K - len)
			return NULL;
		total += len;
		capacity += DIV_ROUND_UP(len, S31_AXI_DESC_MAX);
	}
	desc = kzalloc(struct_size(desc, nodes, capacity), GFP_NOWAIT);
	if (!desc)
		return NULL;
	desc->vd.tx.chan = chan; /* Free the partially allocated list on errors. */
	desc->len = total;
	for_each_sg(sgl, sg, nents, i) {
		dma_addr_t addr = sg_dma_address(sg);
		size_t len = sg_dma_len(sg);

		while (len) {
			struct s31_axi_node *node = &desc->nodes[n];
			u32 chunk = min_t(size_t, len, S31_AXI_DESC_MAX);
			u32 ctrl = FIELD_PREP(S31_AXI_DESC_SIZE, chunk) | S31_AXI_DESC_OWNER;

			node->hw = dma_pool_zalloc(ch->dma->pool, GFP_NOWAIT, &node->dma);
			if (!node->hw)
				goto fail;
			desc->count = ++n;
			if (ch->tx)
				ctrl |= FIELD_PREP(S31_AXI_DESC_LENGTH, chunk);
			node->hw->control = cpu_to_le32(ctrl);
			node->hw->buffer = cpu_to_le32(lower_32_bits(addr));
			if (n > 1)
				desc->nodes[n - 2].hw->next = cpu_to_le32(lower_32_bits(node->dma));
			addr += chunk;
			len -= chunk;
		}
	}
	if (ch->tx)
		desc->nodes[n - 1].hw->control |= cpu_to_le32(S31_AXI_DESC_EOF);
	return vchan_tx_prep(&ch->vc, &desc->vd, flags);
fail:
	s31_axi_free_desc(&desc->vd);
	return NULL;
}

static int s31_axi_config(struct dma_chan *chan, struct dma_slave_config *config)
{
	struct s31_axi_chan *ch = s31_axi_chan(chan);

	if (config->direction != (ch->tx ? DMA_MEM_TO_DEV : DMA_DEV_TO_MEM) ||
	    config->src_addr_width != DMA_SLAVE_BUSWIDTH_4_BYTES ||
	    config->dst_addr_width != DMA_SLAVE_BUSWIDTH_4_BYTES ||
	    config->device_fc)
		return -EINVAL;
	ch->config = *config;
	return 0;
}

static void s31_axi_issue_pending(struct dma_chan *chan)
{
	struct s31_axi_chan *ch = s31_axi_chan(chan);
	unsigned long flags;

	spin_lock_irqsave(&ch->vc.lock, flags);
	if (vchan_issue_pending(&ch->vc))
		s31_axi_start(ch);
	spin_unlock_irqrestore(&ch->vc.lock, flags);
}

static irqreturn_t s31_axi_irq(int irq, void *arg)
{
	struct s31_axi_chan *ch = arg;
	struct s31_axi_desc *desc;
	unsigned long flags;
	u32 status, errors;
	int ret;

	spin_lock_irqsave(&ch->vc.lock, flags);
	status = readl(s31_axi_reg(ch, S31_AXI_INT_ST));
	if (!status) {
		spin_unlock_irqrestore(&ch->vc.lock, flags);
		return IRQ_NONE;
	}
	writel(status, s31_axi_reg(ch, S31_AXI_INT_CLR));
	desc = ch->active;
	if (!desc) {
		spin_unlock_irqrestore(&ch->vc.lock, flags);
		return IRQ_HANDLED;
	}
	errors = status & (ch->tx ? S31_AXI_TX_ERRORS : S31_AXI_RX_ERRORS);
	ret = s31_axi_stop(ch);
	if (ret) {
		spin_unlock_irqrestore(&ch->vc.lock, flags);
		return IRQ_HANDLED; /* Retain DMA-owned descriptors until termination. */
	}
	ch->active = NULL;
	if (errors) {
		ch->error_cookie = desc->vd.tx.cookie;
		desc->vd.tx_result.result = ch->tx ? DMA_TRANS_READ_FAILED : DMA_TRANS_WRITE_FAILED;
		desc->vd.tx_result.residue = desc->len;
		dev_err_ratelimited(ch->dma->device.dev, "%s%u DMA error status=%#x\n",
				    ch->tx ? "tx" : "rx", ch->id, status);
	}
	vchan_cookie_complete(&desc->vd);
	s31_axi_start(ch);
	spin_unlock_irqrestore(&ch->vc.lock, flags);
	return IRQ_HANDLED;
}

static enum dma_status s31_axi_tx_status(struct dma_chan *chan,
					 dma_cookie_t cookie, struct dma_tx_state *state)
{
	struct s31_axi_chan *ch = s31_axi_chan(chan);
	enum dma_status status = dma_cookie_status(chan, cookie, state);
	unsigned long flags;
	struct virt_dma_desc *vd;

	spin_lock_irqsave(&ch->vc.lock, flags);
	if (cookie == ch->error_cookie) {
		status = DMA_ERROR;
	} else if (status != DMA_COMPLETE && state) {
		vd = vchan_find_desc(&ch->vc, cookie);
		if (ch->active && ch->active->vd.tx.cookie == cookie)
			dma_set_residue(state, ch->active->len);
		else if (vd)
			dma_set_residue(state, s31_axi_desc(vd)->len);
	}
	spin_unlock_irqrestore(&ch->vc.lock, flags);
	return status;
}

static int s31_axi_terminate_all(struct dma_chan *chan)
{
	struct s31_axi_chan *ch = s31_axi_chan(chan);
	unsigned long flags;
	int ret;
	LIST_HEAD(head);

	spin_lock_irqsave(&ch->vc.lock, flags);
	ret = s31_axi_stop(ch);
	if (!ret) {
		if (ch->active) {
			vchan_terminate_vdesc(&ch->active->vd);
			ch->active = NULL;
		}
		vchan_get_all_descriptors(&ch->vc, &head);
	}
	spin_unlock_irqrestore(&ch->vc.lock, flags);
	vchan_dma_desc_free_list(&ch->vc, &head);
	return ret;
}

static void s31_axi_synchronize(struct dma_chan *chan)
{
	struct s31_axi_chan *ch = s31_axi_chan(chan);

	synchronize_irq(ch->irq);
	vchan_synchronize(&ch->vc);
}

static int s31_axi_alloc_chan(struct dma_chan *chan)
{
	dma_cookie_init(chan);
	s31_axi_chan(chan)->error_cookie = -EBUSY;
	return 0;
}

static void s31_axi_free_chan(struct dma_chan *chan)
{
	struct s31_axi_chan *ch = s31_axi_chan(chan);

	if (s31_axi_terminate_all(chan))
		return;
	s31_axi_synchronize(chan);
	vchan_free_chan_resources(&ch->vc);
	writel(0x3f, s31_axi_reg(ch, S31_AXI_PERI_SEL));
	spin_lock_irq(&ch->dma->request_lock);
	ch->periph = U32_MAX;
	spin_unlock_irq(&ch->dma->request_lock);
}

static struct dma_chan *s31_axi_xlate(struct of_phandle_args *args, struct of_dma *ofdma)
{
	struct s31_axi_dma *dma = ofdma->of_dma_data;
	struct s31_axi_chan *ch;
	struct dma_chan *chan;
	unsigned long flags;
	unsigned int i;

	if (args->args_count != 2 || args->args[0] >= S31_AXI_CHANNELS || args->args[1] > 5)
		return NULL;
	ch = &dma->channels[args->args[0]];
	spin_lock_irqsave(&dma->request_lock, flags);
	if (ch->periph != U32_MAX)
		goto busy;
	/* TRM: a peripheral can have only one channel per direction. */
	for (i = 0; i < S31_AXI_CHANNELS; i++)
		if (dma->channels[i].tx == ch->tx && dma->channels[i].periph == args->args[1])
			goto busy;
	ch->periph = args->args[1];
	spin_unlock_irqrestore(&dma->request_lock, flags);
	chan = dma_get_slave_channel(&ch->vc.chan);
	if (!chan) {
		spin_lock_irqsave(&dma->request_lock, flags);
		ch->periph = U32_MAX;
		spin_unlock_irqrestore(&dma->request_lock, flags);
	}
	return chan;
busy:
	spin_unlock_irqrestore(&dma->request_lock, flags);
	return NULL;
}

static int s31_axi_probe(struct platform_device *pdev)
{
	struct device *dev = &pdev->dev;
	struct s31_axi_dma *dma;
	struct reset_control *rst;
	struct clk *clk;
	unsigned int i;
	int ret;

	dma = devm_kzalloc(dev, sizeof(*dma), GFP_KERNEL);
	if (!dma)
		return -ENOMEM;
	dma->base = devm_platform_ioremap_resource(pdev, 0);
	if (IS_ERR(dma->base))
		return PTR_ERR(dma->base);
	clk = devm_clk_get_enabled(dev, NULL);
	if (IS_ERR(clk))
		return dev_err_probe(dev, PTR_ERR(clk), "bus clock unavailable\n");
	rst = devm_reset_control_get_exclusive(dev, NULL);
	if (IS_ERR(rst))
		return dev_err_probe(dev, PTR_ERR(rst), "reset unavailable\n");
	ret = reset_control_assert(rst);
	if (ret)
		return ret;
	ret = reset_control_deassert(rst);
	if (ret)
		return ret;
	ret = dma_set_mask_and_coherent(dev, DMA_BIT_MASK(32));
	if (ret)
		return ret;
	dma->pool = dma_pool_create("esp32s31-axi-desc", dev,
				    sizeof(struct s31_axi_hw_desc), 8, 0);
	if (!dma->pool)
		return -ENOMEM;
	spin_lock_init(&dma->request_lock);
	dma->device.dev = dev;
	INIT_LIST_HEAD(&dma->device.channels);
	dma_cap_set(DMA_SLAVE, dma->device.cap_mask);
	dma->device.directions = BIT(DMA_MEM_TO_DEV) | BIT(DMA_DEV_TO_MEM);
	dma->device.src_addr_widths = BIT(DMA_SLAVE_BUSWIDTH_4_BYTES);
	dma->device.dst_addr_widths = BIT(DMA_SLAVE_BUSWIDTH_4_BYTES);
	dma->device.residue_granularity = DMA_RESIDUE_GRANULARITY_DESCRIPTOR;
	dma->device.device_alloc_chan_resources = s31_axi_alloc_chan;
	dma->device.device_free_chan_resources = s31_axi_free_chan;
	dma->device.device_config = s31_axi_config;
	dma->device.device_prep_slave_sg = s31_axi_prep_slave_sg;
	dma->device.device_issue_pending = s31_axi_issue_pending;
	dma->device.device_tx_status = s31_axi_tx_status;
	dma->device.device_terminate_all = s31_axi_terminate_all;
	dma->device.device_synchronize = s31_axi_synchronize;

	writel(0x2f000000, dma->base + S31_AXI_INTR_START);
	writel(0x2f07ffff, dma->base + S31_AXI_INTR_END);
	writel(0x40000000, dma->base + S31_AXI_EXTR_START);
	writel(0x53ffffff, dma->base + S31_AXI_EXTR_END);
	writel(S31_AXI_MISC_CLK, dma->base + S31_AXI_MISC);
	for (i = 0; i < S31_AXI_CHANNELS; i++) {
		struct s31_axi_chan *ch = &dma->channels[i];
		char name[8];

		ch->dma = dma;
		ch->id = i / 2;
		ch->tx = i & 1;
		ch->periph = U32_MAX;
		ch->error_cookie = -EBUSY;
		ch->vc.desc_free = s31_axi_free_desc;
		vchan_init(&ch->vc, &dma->device);
		writel(0, s31_axi_reg(ch, S31_AXI_INT_ENA));
		writel(~0U, s31_axi_reg(ch, S31_AXI_INT_CLR));
		writel(0x3f, s31_axi_reg(ch, S31_AXI_PERI_SEL));
		snprintf(name, sizeof(name), "%s%u", ch->tx ? "tx" : "rx", ch->id);
		ch->irq = platform_get_irq_byname(pdev, name);
		if (ch->irq < 0) {
			ret = ch->irq;
			goto fail;
		}
		ret = devm_request_irq(dev, ch->irq, s31_axi_irq, 0, dev_name(dev), ch);
		if (ret)
			goto fail;
	}
	ret = dma_async_device_register(&dma->device);
	if (ret)
		goto fail;
	ret = of_dma_controller_register(dev->of_node, s31_axi_xlate, dma);
	if (ret) {
		dma_async_device_unregister(&dma->device);
		goto fail;
	}
	platform_set_drvdata(pdev, dma);
	dev_info(dev, "AXI GDMA ready: 3 independent TX/RX pairs, slave-SG, SRAM descriptors\n");
	return 0;
fail:
	/* Only the first i+1 vchans were initialized if IRQ setup failed. */
	for (i = 0; i < S31_AXI_CHANNELS; i++)
		if (dma->channels[i].vc.desc_free)
			tasklet_kill(&dma->channels[i].vc.task);
	dma_pool_destroy(dma->pool);
	return ret;
}

static void s31_axi_remove(struct platform_device *pdev)
{
	struct s31_axi_dma *dma = platform_get_drvdata(pdev);
	unsigned int i;

	of_dma_controller_free(pdev->dev.of_node);
	dma_async_device_unregister(&dma->device);
	for (i = 0; i < S31_AXI_CHANNELS; i++) {
		if (s31_axi_terminate_all(&dma->channels[i].vc.chan)) {
			dev_err(&pdev->dev, "DMA still owns descriptors; retaining pool\n");
			return;
		}
		s31_axi_synchronize(&dma->channels[i].vc.chan);
		vchan_free_chan_resources(&dma->channels[i].vc);
	}
	dma_pool_destroy(dma->pool);
}

static const struct of_device_id s31_axi_match[] = {
	{ .compatible = "espressif,esp32s31-axi-gdma" },
	{ }
};
MODULE_DEVICE_TABLE(of, s31_axi_match);

static struct platform_driver s31_axi_driver = {
	.probe = s31_axi_probe,
	.remove = s31_axi_remove,
	.driver = {
		.name = "esp32s31-axi-gdma",
		.of_match_table = s31_axi_match,
	},
};
module_platform_driver(s31_axi_driver);
MODULE_DESCRIPTION("ESP32-S31 AXI GDMA slave-SG controller");
MODULE_LICENSE("GPL");
