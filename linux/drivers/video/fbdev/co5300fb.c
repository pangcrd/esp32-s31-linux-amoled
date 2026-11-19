// SPDX-License-Identifier: GPL-2.0-only
/* CO5300 QSPI RGB565 framebuffer for the ESP32-S31 boot console.
 * Commands: 02 00 <DCS register> 00, all on IO0.
 * Pixels:   32 00 2c 00 on IO0, followed by data on IO0..IO3.
 * Panel defaults follow Espressif esp_lcd_co5300; vendor sequences may differ.
 */
#include <linux/delay.h>
#include <linux/fb.h>
#include <linux/gpio/consumer.h>
#include <linux/module.h>
#include <linux/property.h>
#include <linux/spi/spi.h>
#include <linux/vmalloc.h>

struct co5300fb {
	struct spi_device *spi;
	struct gpio_desc *reset;
	struct fb_deferred_io defio;
	spinlock_t damage_lock;
	bool dirty, stopped;
	u32 first, last, width, height, xoff, yoff;
	u32 palette[16];
	u8 *command, *pixels;
};

static int co5300fb_command(struct co5300fb *p, u8 cmd,
			    const u8 *data, size_t len)
{
	struct spi_transfer t = {
		.tx_buf = p->command, .len = 4 + len,
		.tx_nbits = SPI_NBITS_SINGLE,
	};

	if (len > 255)
		return -EINVAL;
	p->command[0] = 0x02;
	p->command[1] = 0;
	p->command[2] = cmd;
	p->command[3] = 0;
	if (len)
		memcpy(p->command + 4, data, len);
	{
		int ret = spi_sync_transfer(p->spi, &t, 1);

		/* Row commands must not feed printk back into fbcon redraws. */
		if (cmd != 0x2a && cmd != 0x2b)
			dev_info(&p->spi->dev,
				 "DIAG cmd 0x%02x len %zu tx=%*ph -> %d\n",
				 cmd, len, (int)(4 + len), p->command, ret);
		return ret;
	}
}

/* CO5300 CASET/RASET require an even start and an even-sized window.
 * Send two adjacent rows per message; a one-row RASET is invalid.
 * Never rely on RAMWRC or CS surviving between messages. Use kmalloc-backed
 * buffers because the SPI core may DMA-map them on other controllers.
 */
static int co5300fb_row(struct fb_info *info, u32 y)
{
	struct co5300fb *p = info->par;
	const u16 *src = (const u16 *)(info->screen_buffer +
				     y * info->fix.line_length);
	u32 xend = p->xoff + p->width - 1, row = y + p->yoff;
	u8 address[4];
	struct spi_transfer t[2] = {
		{ .tx_buf = p->command, .len = 4, .tx_nbits = SPI_NBITS_SINGLE },
		{ .tx_buf = p->pixels, .len = p->width * 4,
		  .tx_nbits = SPI_NBITS_QUAD },
	};
	u32 x;
	int ret;

	address[0] = p->xoff >> 8;
	address[1] = p->xoff;
	address[2] = xend >> 8;
	address[3] = xend;
	ret = co5300fb_command(p, 0x2a, address, sizeof(address));
	if (ret)
		return ret;
	address[0] = row >> 8;
	address[1] = row;
	address[2] = (row + 1) >> 8;
	address[3] = row + 1;
	ret = co5300fb_command(p, 0x2b, address, sizeof(address));
	if (ret)
		return ret;
	for (x = 0; x < p->width * 2; x++) {
		u16 pixel = READ_ONCE(src[x]);

		p->pixels[2 * x] = pixel >> 8;
		p->pixels[2 * x + 1] = pixel;
	}
	p->command[0] = 0x32;
	p->command[1] = 0;
	p->command[2] = 0x2c;
	p->command[3] = 0;
	/* cs_change remains zero: CS covers the single and quad phases. */
	{
		static int rows;
		int ret = spi_sync_transfer(p->spi, t, ARRAY_SIZE(t));

		rows++;
		if (rows <= 3 || ret)
			dev_info(&p->spi->dev,
				 "DIAG row y=%u xoff=%u RASET=%u..%u bytes=%u lanes=%u/%u -> %d\n",
				 y, p->xoff, (address[0] << 8) | address[1],
				 (address[2] << 8) | address[3], t[1].len,
				 t[0].tx_nbits, t[1].tx_nbits, ret);
		return ret;
	}
}

static void co5300fb_damage(struct fb_info *info, u32 first, u32 last,
			    unsigned long delay)
{
	struct co5300fb *p = info->par;
	unsigned long flags;

	if (first >= p->height)
		return;
	last = min(last, p->height - 1);
	spin_lock_irqsave(&p->damage_lock, flags);
	if (!p->stopped) {
		p->first = p->dirty ? min(p->first, first) : first;
		p->last = p->dirty ? max(p->last, last) : last;
		p->dirty = true;
		schedule_delayed_work(&info->deferred_work, delay);
	}
	spin_unlock_irqrestore(&p->damage_lock, flags);
}

static void co5300fb_damage_range(struct fb_info *info, off_t off, size_t len)
{
	if (len)
		co5300fb_damage(info, off / info->fix.line_length,
			       (off + len - 1) / info->fix.line_length,
			       info->fbdefio->delay);
}

static void co5300fb_damage_area(struct fb_info *info, u32 x, u32 y,
				 u32 width, u32 height)
{
	struct co5300fb *p = info->par;

	if (width && height && y < p->height)
		co5300fb_damage(info, y, y + min(height, p->height - y) - 1,
			       info->fbdefio->delay);
}

FB_GEN_DEFAULT_DEFERRED_SYSMEM_OPS(co5300fb,
				   co5300fb_damage_range,
				   co5300fb_damage_area)

static void co5300fb_deferred_io(struct fb_info *info, struct list_head *pages)
{
	struct co5300fb *p = info->par;
	unsigned long flags;
	u32 first, last, y;
	int ret;

	spin_lock_irqsave(&p->damage_lock, flags);
	if (p->stopped || (!p->dirty && list_empty(pages))) {
		spin_unlock_irqrestore(&p->damage_lock, flags);
		return;
	}
	first = list_empty(pages) ? p->first : 0;
	last = list_empty(pages) ? p->last : p->height - 1;
	p->dirty = false;
	spin_unlock_irqrestore(&p->damage_lock, flags);
	/* Include the unchanged neighbour when damage covers only one row. */
	first &= ~1U;
	last |= 1U;
	for (y = first; y <= last; y += 2) {
		ret = co5300fb_row(info, y);
		if (ret) {
			dev_err_ratelimited(&p->spi->dev,
				"row %u transfer failed: %d\n", y, ret);
			co5300fb_damage(info, y, last, HZ);
			return;
		}
		cond_resched();
	}
}

static int co5300fb_setcolreg(unsigned int reg, unsigned int red,
			     unsigned int green, unsigned int blue,
			     unsigned int transp, struct fb_info *info)
{
	struct co5300fb *p = info->par;

	if (reg >= ARRAY_SIZE(p->palette))
		return 1;
	p->palette[reg] = ((red >> 11) << 11) |
			 ((green >> 10) << 5) | (blue >> 11);
	return 0;
}

static int co5300fb_check_var(struct fb_var_screeninfo *var, struct fb_info *info)
{
	/* This is a fixed-size RGB565 surface; no panning or mode switching. */
	if (var->xres != info->var.xres || var->yres != info->var.yres ||
	    var->xres_virtual != var->xres || var->yres_virtual != var->yres ||
	    var->bits_per_pixel != 16 || var->xoffset || var->yoffset)
		return -EINVAL;
	var->red = info->var.red;
	var->green = info->var.green;
	var->blue = info->var.blue;
	var->transp = info->var.transp;
	return 0;
}

static void co5300fb_destroy(struct fb_info *info)
{
	struct co5300fb *p = info->par;

	vfree(info->screen_buffer);
	kfree(p->pixels);
	kfree(p->command);
	framebuffer_release(info);
}

static const struct fb_ops co5300fb_ops = {
	.owner = THIS_MODULE,
	FB_DEFAULT_DEFERRED_OPS(co5300fb),
	.fb_setcolreg = co5300fb_setcolreg,
	.fb_check_var = co5300fb_check_var,
	.fb_destroy = co5300fb_destroy,
};

/* Supplier override: records [command, length (0..255), delay-ms BE16, data].
 * Validate the entire sequence before sending any command.
 */
static int co5300fb_init_sequence(struct co5300fb *p)
{
	static const u8 defaults[] = {
		/* Supplier init for the C05300AF-51 410x502 module (proven
		 * under ESP-IDF), in the supplier's order: FE, C4, 3A, 36,
		 * 35, 53, 51, 63, 11 (600 ms). 0x35 carries its data byte
		 * (0x00) — sending it with no data desyncs the panel's
		 * command parser and the commands after it (53/51/63/11/
		 * 29) are misread, leaving the display off. 0x36 is MADCTL
		 * 0x60 (landscape: row/column exchange + column order);
		 * without it the panel shows portrait. 0x29 (Display On)
		 * is sent after this table. */
		0xfe, 1, 0, 0, 0x00,
		0xc4, 1, 0, 0, 0x80,
		0x3a, 1, 0, 0, 0x55,
		0x36, 1, 0, 0, 0x60,
		0x35, 1, 0, 0, 0x00,
		0x53, 1, 0, 0, 0x20,
		0x51, 1, 0, 0, 0xff,
		0x63, 1, 0, 0, 0xff,
		0x11, 0, 0x02, 0x58,
	};
	struct device *dev = &p->spi->dev;
	const u8 *seq = defaults;
	u8 *custom;
	int size = sizeof(defaults), i, len, ret;

	if (device_property_present(dev, "chipone,init-sequence")) {
		size = device_property_count_u8(dev, "chipone,init-sequence");
		if (size <= 0 || size > 4096)
			return -EINVAL;
		custom = devm_kmalloc(dev, size, GFP_KERNEL);
		if (!custom)
			return -ENOMEM;
		ret = device_property_read_u8_array(dev, "chipone,init-sequence",
						  custom, size);
		if (ret)
			return ret;
		seq = custom;
	}
	for (i = 0; i < size; i += 4 + seq[i + 1]) {
		if (size - i < 4 || size - i < 4 + seq[i + 1])
			return -EINVAL;
	}
	for (i = 0; i < size; i += 4 + len) {
		len = seq[i + 1];
		ret = co5300fb_command(p, seq[i], seq + i + 4, len);
		if (ret)
			return ret;
		if (seq[i + 2] || seq[i + 3])
			msleep((seq[i + 2] << 8) | seq[i + 3]);
	}
	return 0;
}

static int co5300fb_probe(struct spi_device *spi)
{
	struct device *dev = &spi->dev;
	struct fb_info *info;
	struct co5300fb *p;
	int ret;

	info = framebuffer_alloc(sizeof(*p), dev);
	if (!info)
		return -ENOMEM;
	p = info->par;
	p->spi = spi;
	spin_lock_init(&p->damage_lock);
	/* Defaults are the C05300AF-51 module on this shield, landscape
	 * (502x410) via MADCTL 0x60; DT properties
	 * (chipone,width/height/x-offset/y-offset) override them. */
	p->width = 502;
	p->height = 410;
	p->xoff = 0;
	p->yoff = 22;
	device_property_read_u32(dev, "chipone,width", &p->width);
	device_property_read_u32(dev, "chipone,height", &p->height);
	device_property_read_u32(dev, "chipone,x-offset", &p->xoff);
	device_property_read_u32(dev, "chipone,y-offset", &p->yoff);
	if (!p->width || !p->height || p->width > 512 || p->height > 512 ||
	    p->xoff > 65535 - p->width || p->yoff > 65535 - p->height ||
	    ((p->width | p->height | p->xoff | p->yoff) & 1)) {
		ret = -EINVAL;
		goto fail;
	}
	spi->mode &= ~(SPI_CPOL | SPI_CPHA | SPI_LSB_FIRST);
	spi->mode |= SPI_TX_QUAD;
	spi->bits_per_word = 8;
	ret = spi_setup(spi);
	if (ret)
		goto fail;
	p->reset = devm_gpiod_get(dev, "reset", GPIOD_ASIS);
	if (IS_ERR(p->reset)) {
		ret = dev_err_probe(dev, PTR_ERR(p->reset), "reset GPIO unavailable\n");
		goto fail;
	}
	p->command = kmalloc(259, GFP_KERNEL);
	p->pixels = kmalloc(p->width * 4, GFP_KERNEL);
	info->fix = (struct fb_fix_screeninfo) {
		.id = "CO5300 QSPI", .type = FB_TYPE_PACKED_PIXELS,
		.visual = FB_VISUAL_TRUECOLOR, .accel = FB_ACCEL_NONE,
		.line_length = p->width * 2, .smem_len = p->width * p->height * 2,
	};
	info->screen_buffer = vmalloc_user(PAGE_ALIGN(info->fix.smem_len));
	if (!p->command || !p->pixels || !info->screen_buffer) {
		ret = -ENOMEM;
		goto fail;
	}
	/* Sysmem framebuffer: sys_imageblit/sys_fillrect (used by fbcon)
	 * and fb_sys_write (used by /dev/fb0 writes) draw through
	 * screen_base, and the core only treats the memory as virtual
	 * RAM when FBINFO_VIRTFB is set. Without the flag the core
	 * warns "framebuffer is not in virtual address space" and the
	 * draw goes nowhere, so the screen stays black even though the
	 * deferred-I/O row transfers succeed. screen_base and
	 * screen_buffer point at the same vmalloc memory. */
	info->screen_base = info->screen_buffer;
	info->flags = FBINFO_VIRTFB;
	info->screen_size = info->fix.smem_len;
	info->var = (struct fb_var_screeninfo) {
		.xres = p->width, .xres_virtual = p->width,
		.yres = p->height, .yres_virtual = p->height,
		.bits_per_pixel = 16,
		.red = { .offset = 11, .length = 5 },
		.green = { .offset = 5, .length = 6 },
		.blue = { .offset = 0, .length = 5 },
	};
	info->fbops = &co5300fb_ops;
	info->pseudo_palette = p->palette;
	p->defio.delay = max_t(unsigned long, 1, HZ / 25);
	p->defio.deferred_io = co5300fb_deferred_io;
	info->fbdefio = &p->defio;
	ret = fb_deferred_io_init(info);
	if (ret)
		goto fail;

	/* Reset pulse with RAW (physical) levels: the logical
	 * gpiod_set_value() path left RST at 0 V on this board (panel
	 * stuck in reset, init ignored, screen black) even though the
	 * GPIO is flagged ACTIVE LOW. Raw 0 = physical 0 V = reset
	 * asserted; raw 1 = physical 3.3 V = reset released. The
	 * readback below proves the release in dmesg. */
	ret = gpiod_direction_output_raw(p->reset, 0);
	if (ret)
		goto cleanup;
	usleep_range(10000, 11000);
	gpiod_set_raw_value_cansleep(p->reset, 1);
	dev_info(dev, "reset released, RST raw value=%d (want 1)\n",
		 gpiod_get_raw_value(p->reset));
	msleep(150);
	/* Init sequence (supplier order, incl. 0x3a=0x55 and the 0x35
	 * data byte) then Display On. */
	ret = co5300fb_init_sequence(p);
	if (ret)
		goto cleanup;
	ret = co5300fb_command(p, 0x29, NULL, 0);
	if (ret)
		goto cleanup;
	spi_set_drvdata(spi, info);
	ret = register_framebuffer(info);
	if (ret)
		goto cleanup;
	co5300fb_damage(info, 0, p->height - 1, p->defio.delay);
	dev_info(dev, "fb%d: %ux%u RGB565 QSPI console at %u Hz\n",
		 info->node, p->width, p->height, spi->max_speed_hz);
	return 0;

cleanup:
	fb_deferred_io_cleanup(info);
fail:
	dev_err_probe(dev, ret, "CO5300 initialization failed\n");
	co5300fb_destroy(info);
	return ret;
}

static void co5300fb_remove(struct spi_device *spi)
{
	struct fb_info *info = spi_get_drvdata(spi);
	struct co5300fb *p = info->par;
	unsigned long flags;

	spin_lock_irqsave(&p->damage_lock, flags);
	p->stopped = true;
	spin_unlock_irqrestore(&p->damage_lock, flags);
	fb_deferred_io_cleanup(info);
	co5300fb_command(p, 0x28, NULL, 0);
	/*
	 * unregister_framebuffer drops the registration reference and may
	 * invoke fb_destroy immediately. Do not access info afterwards.
	 */
	unregister_framebuffer(info);
}

static const struct of_device_id co5300fb_of_match[] = {
	{ .compatible = "chipone,co5300" },
	{ }
};
MODULE_DEVICE_TABLE(of, co5300fb_of_match);

static const struct spi_device_id co5300fb_ids[] = {
	{ "co5300", 0 },
	{ }
};
MODULE_DEVICE_TABLE(spi, co5300fb_ids);

static struct spi_driver co5300fb_driver = {
	.driver = {
		.name = "co5300fb",
		.of_match_table = co5300fb_of_match,
	},
	.probe = co5300fb_probe,
	.remove = co5300fb_remove,
	.id_table = co5300fb_ids,
};
module_spi_driver(co5300fb_driver);
MODULE_DESCRIPTION("CO5300 QSPI AMOLED framebuffer console");
MODULE_LICENSE("GPL");
