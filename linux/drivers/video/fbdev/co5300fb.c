// SPDX-License-Identifier: GPL-2.0-only
/* CO5300 QSPI RGB565 framebuffer for the ESP32-S31 boot console.
 * Commands: 02 00 <DCS register> 00, all on IO0.
 * Pixels:   32 00 2c 00 on IO0, followed by data on IO0..IO3.
 * Panel defaults follow Espressif esp_lcd_co5300; vendor sequences may differ.
 */
#include <linux/delay.h>
#include <linux/fb.h>
#include <linux/gpio/consumer.h>
#include <linux/math64.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/property.h>
#include <linux/sched.h>
#include <linux/spi/spi.h>
#include <linux/timekeeping.h>
#include <linux/vmalloc.h>

/* Full-frame DMA is board-tested. Opt in to partial GRAM windows only when
 * comparing the login black-screen failure on the actual panel. */
static bool partial_updates;
module_param(partial_updates, bool, 0644);
MODULE_PARM_DESC(partial_updates, "Enable partial GRAM windows (default: full-frame refresh)");

/* Opt in: hashing a full frame adds CPU work to every diagnostic flush. */
static bool render_diagnostics;
module_param(render_diagnostics, bool, 0644);
MODULE_PARM_DESC(render_diagnostics, "Measure rendered pixels before SPI submission");

struct co5300fb_render_stats {
	u64 samples;
	u32 first, last, bytes, nonzero_bytes, hash;
	u32 rotation;
	u32 sample_offset;
	u16 fb_sample, tx_sample;
	bool sample_valid;
};

struct co5300fb_stats {
	u64 flushes, errors, full_flushes;
	u64 render_us, spi_us, total_us;
	u64 full_render_us, full_spi_us, full_total_us, max_full_us;
	u32 first, last, bytes;
};

struct co5300fb {
	struct spi_device *spi;
	struct gpio_desc *reset;
	struct fb_deferred_io defio;
	spinlock_t damage_lock;
	struct mutex io_lock; /* Serialize complete windows and runtime settings. */
	bool dirty, stopped;
	u32 first, last, width, height, xoff, yoff;
	u32 rotation;
	struct co5300fb_stats stats; /* Protected by damage_lock. */
	struct co5300fb_render_stats render_stats; /* Protected by io_lock. */
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

/*
 * Rotation 0 keeps the proven clockwise software mapping:
 * logical (x, y) -> GRAM (yoff + height - 1 - y, xoff + x).
 * xoff/yoff describe the unrotated logical axes for the existing DT.
 * Read 16x16 tiles to avoid a full-height strided source scan. Output is
 * still row-major GRAM order, RGB565 MSB first, in one even-sized window.
 * Rotation is relative to this landscape view; portrait is selected at
 * probe time so fbcon and mmap never see changing framebuffer dimensions.
 */
static void co5300fb_render(struct fb_info *info, u32 first, u32 last)
{
	struct co5300fb *p = info->par;
	const u16 *fb = (const u16 *)info->screen_buffer;
	u32 rows = last - first + 1;
	u32 xb, yb, x, y;

	for (xb = 0; xb < p->width; xb += 16) {
		u32 xe = min(xb + 16, p->width);

		for (yb = first; yb <= last; yb += 16) {
			u32 ye = min(yb + 16, last + 1);

			for (y = yb; y < ye; y++) {
				const u16 *src = fb + y * p->width + xb;
				u32 offset, step;

				switch (p->rotation) {
				case 180:
					offset = 2 * ((p->width - 1 - xb) * rows + y - first);
					step = rows * 2;
					break;
				case 90:
					offset = 2 * ((y - first) * p->width + xb);
					step = 2;
					break;
				case 270:
					offset = 2 * ((last - y) * p->width + p->width - 1 - xb);
					step = 2;
					break;
				default:
					offset = 2 * (xb * rows + last - y);
					step = rows * 2;
					break;
				}

				for (x = xb; x < xe; x++) {
					u16 pixel = READ_ONCE(*src++);
					u8 *dst = p->pixels + offset;

					dst[0] = pixel >> 8;
					dst[1] = pixel;
					if (p->rotation == 180 || p->rotation == 270)
						offset -= step;
					else
						offset += step;
				}
			}
		}
		cond_resched();
	}
}

/* Diagnostics inspect exactly the rendered bytes passed to SPI. The sample
 * is logical (265,240), the white belly of the positioned 230x240 Tux.
 * Counts/hash describe the pre-SPI CPU buffer, not on-wire measurements. */
static void co5300fb_measure_render(struct fb_info *info, u32 first, u32 last)
{
	struct co5300fb *p = info->par;
	struct co5300fb_render_stats *s = &p->render_stats;
	u32 rows = last - first + 1, offset = 0, i;
	const u32 x = 265, y = 240;

	s->samples++;
	s->first = first;
	s->last = last;
	s->bytes = p->width * rows * 2;
	s->rotation = p->rotation;
	s->nonzero_bytes = 0;
	s->hash = 2166136261U;
	for (i = 0; i < s->bytes; i++) {
		u8 byte = p->pixels[i];

		s->nonzero_bytes += !!byte;
		s->hash = (s->hash ^ byte) * 16777619U;
	}
	s->sample_valid = x < p->width && y < p->height && y >= first && y <= last;
	s->sample_offset = 0;
	s->fb_sample = 0;
	s->tx_sample = 0;
	if (!s->sample_valid)
		return;
	switch (p->rotation) {
	case 90:
		offset = 2 * ((y - first) * p->width + x);
		break;
	case 180:
		offset = 2 * ((p->width - 1 - x) * rows + y - first);
		break;
	case 270:
		offset = 2 * ((last - y) * p->width + p->width - 1 - x);
		break;
	default:
		offset = 2 * (x * rows + last - y);
		break;
	}
	s->sample_offset = offset;
	s->fb_sample = READ_ONCE(((u16 *)info->screen_buffer)[y * p->width + x]);
	s->tx_sample = ((u16)p->pixels[offset] << 8) | p->pixels[offset + 1];
}

static int co5300fb_flush(struct fb_info *info, u32 first, u32 last)
{
	struct co5300fb *p = info->par;
	u32 cfirst, clast, rfirst, rlast;
	u8 address[4];
	struct spi_transfer t[2] = {
		{ .tx_buf = p->command, .len = 4, .tx_nbits = SPI_NBITS_SINGLE },
		{ .tx_buf = p->pixels, .len = p->width * (last - first + 1) * 2,
		  .tx_nbits = SPI_NBITS_QUAD },
	};
	u64 begin, rendered, finish;
	unsigned long flags;
	int ret;

	mutex_lock(&p->io_lock);
	begin = ktime_get_ns();
	if (p->rotation == 90 || p->rotation == 270) {
		cfirst = p->yoff;
		clast = p->yoff + p->width - 1;
		rfirst = p->xoff + (p->rotation == 90 ? first : p->height - 1 - last);
		rlast = p->xoff + (p->rotation == 90 ? last : p->height - 1 - first);
	} else {
		cfirst = p->yoff + (p->rotation == 180 ? first : p->height - 1 - last);
		clast = p->yoff + (p->rotation == 180 ? last : p->height - 1 - first);
		rfirst = p->xoff;
		rlast = p->xoff + p->width - 1;
	}
	co5300fb_render(info, first, last);
	if (READ_ONCE(render_diagnostics))
		co5300fb_measure_render(info, first, last);
	rendered = ktime_get_ns();
	address[0] = cfirst >> 8;
	address[1] = cfirst;
	address[2] = clast >> 8;
	address[3] = clast;
	ret = co5300fb_command(p, 0x2a, address, sizeof(address));
	if (ret)
		goto done;
	address[0] = rfirst >> 8;
	address[1] = rfirst;
	address[2] = rlast >> 8;
	address[3] = rlast;
	ret = co5300fb_command(p, 0x2b, address, sizeof(address));
	if (ret)
		goto done;
	p->command[0] = 0x32;
	p->command[1] = 0;
	p->command[2] = 0x2c;
	p->command[3] = 0;
	/* CS stays asserted across the single-lane header and quad payload. */
	ret = spi_sync_transfer(p->spi, t, ARRAY_SIZE(t));
done:
	finish = ktime_get_ns();
	spin_lock_irqsave(&p->damage_lock, flags);
	p->stats.flushes++;
	p->stats.errors += !!ret;
	p->stats.first = first;
	p->stats.last = last;
	p->stats.bytes = t[1].len;
	p->stats.render_us = div_u64(rendered - begin, 1000);
	p->stats.spi_us = div_u64(finish - rendered, 1000);
	p->stats.total_us = div_u64(finish - begin, 1000);
	if (!ret && !first && last == p->height - 1) {
		p->stats.full_flushes++;
		p->stats.full_render_us = p->stats.render_us;
		p->stats.full_spi_us = p->stats.spi_us;
		p->stats.full_total_us = p->stats.total_us;
		p->stats.max_full_us = max(p->stats.max_full_us, p->stats.total_us);
	}
	spin_unlock_irqrestore(&p->damage_lock, flags);
	mutex_unlock(&p->io_lock);
	return ret;
}

/* Reading stats must not printk into fbcon and trigger more display work. */
static ssize_t flush_stats_show(struct device *dev,
				struct device_attribute *attr, char *buf)
{
	struct fb_info *info = spi_get_drvdata(to_spi_device(dev));
	struct co5300fb *p = info->par;
	struct co5300fb_stats stats;
	unsigned long flags;

	spin_lock_irqsave(&p->damage_lock, flags);
	stats = p->stats;
	spin_unlock_irqrestore(&p->damage_lock, flags);
	return sysfs_emit(buf,
		"flushes=%llu errors=%llu rows=%u..%u bytes=%u\n"
		"render_us=%llu spi_us=%llu total_us=%llu\n"
		"full_flushes=%llu full_render_us=%llu full_spi_us=%llu\n"
		"full_total_us=%llu max_full_us=%llu\n",
		stats.flushes, stats.errors, stats.first, stats.last, stats.bytes,
		stats.render_us, stats.spi_us, stats.total_us,
		stats.full_flushes, stats.full_render_us, stats.full_spi_us,
		stats.full_total_us, stats.max_full_us);
}
static DEVICE_ATTR_RO(flush_stats);

static ssize_t render_stats_show(struct device *dev,
				 struct device_attribute *attr, char *buf)
{
	struct fb_info *info = spi_get_drvdata(to_spi_device(dev));
	struct co5300fb *p = info->par;
	struct co5300fb_render_stats s;
	int reset_raw;

	mutex_lock(&p->io_lock);
	s = p->render_stats;
	reset_raw = gpiod_get_raw_value_cansleep(p->reset);
	mutex_unlock(&p->io_lock);
	return sysfs_emit(buf,
		"enabled=%c samples=%llu rows=%u..%u bytes=%u nonzero_bytes=%u hash=%08x\n"
		"sample_x=265 sample_y=240 sample_valid=%c fb_rgb565=%04x tx_rgb565=%04x tx_offset=%u\n"
		"width=%u height=%u stride=%u rotation=%u reset_raw=%d\n",
		READ_ONCE(render_diagnostics) ? 'Y' : 'N', s.samples,
		s.first, s.last, s.bytes, s.nonzero_bytes, s.hash,
		s.sample_valid ? 'Y' : 'N', s.fb_sample, s.tx_sample, s.sample_offset,
		p->width, p->height, info->fix.line_length, s.rotation, reset_raw);
}
static DEVICE_ATTR_RO(render_stats);

static void co5300fb_damage(struct fb_info *info, u32 first, u32 last,
			    unsigned long delay);

/* Explicit recovery experiment only: a black RAMWR does not itself send
 * sleep/display-off commands. Do not silently reinitialize a healthy panel. */
static ssize_t panel_resume_store(struct device *dev,
				  struct device_attribute *attr,
				  const char *buf, size_t count)
{
	struct fb_info *info = spi_get_drvdata(to_spi_device(dev));
	struct co5300fb *p = info->par;
	u8 value;
	u32 request;
	int ret;

	if (kstrtou32(buf, 10, &request) || request != 1)
		return -EINVAL;
	mutex_lock(&p->io_lock);
	ret = gpiod_get_raw_value_cansleep(p->reset);
	if (ret <= 0) {
		ret = ret < 0 ? ret : -EHOSTDOWN;
		goto out;
	}
	ret = co5300fb_command(p, 0x11, NULL, 0);
	if (ret)
		goto out;
	/* Datasheet SLPOUT settling time; keep probe's supplier init unchanged. */
	msleep(120);
	value = 0x20;
	ret = co5300fb_command(p, 0x53, &value, 1);
	if (ret)
		goto out;
	value = 0xff;
	ret = co5300fb_command(p, 0x51, &value, 1);
	if (ret)
		goto out;
	ret = co5300fb_command(p, 0x63, &value, 1);
	if (ret)
		goto out;
	ret = co5300fb_command(p, 0x29, NULL, 0);
	if (!ret)
		co5300fb_damage(info, 0, p->height - 1, p->defio.delay);
out:
	mutex_unlock(&p->io_lock);
	return ret ? ret : count;
}
static DEVICE_ATTR_WO(panel_resume);

static ssize_t spi_freq_hz_show(struct device *dev,
			       struct device_attribute *attr, char *buf)
{
	return sysfs_emit(buf, "%u\n", READ_ONCE(to_spi_device(dev)->max_speed_hz));
}

static ssize_t spi_freq_hz_store(struct device *dev,
				struct device_attribute *attr,
				const char *buf, size_t count)
{
	struct spi_device *spi = to_spi_device(dev);
	struct fb_info *info = spi_get_drvdata(spi);
	struct co5300fb *p = info->par;
	u32 hz;

	if (kstrtou32(buf, 10, &hz) || !hz ||
	    hz < spi->controller->min_speed_hz || hz > spi->controller->max_speed_hz)
		return -EINVAL;
	/* All transfers use the SPI device default speed. Do not change it
	 * between CASET, RASET and RAMWR of a deferred-I/O window. */
	mutex_lock(&p->io_lock);
	WRITE_ONCE(spi->max_speed_hz, hz);
	mutex_unlock(&p->io_lock);
	return count;
}
static DEVICE_ATTR_RW(spi_freq_hz);

static ssize_t rotation_show(struct device *dev,
			     struct device_attribute *attr, char *buf)
{
	struct fb_info *info = spi_get_drvdata(to_spi_device(dev));
	struct co5300fb *p = info->par;

	return sysfs_emit(buf, "%u\n", READ_ONCE(p->rotation));
}

static ssize_t rotation_store(struct device *dev,
			      struct device_attribute *attr,
			      const char *buf, size_t count)
{
	struct fb_info *info = spi_get_drvdata(to_spi_device(dev));
	struct co5300fb *p = info->par;
	u32 rotation;

	if (kstrtou32(buf, 10, &rotation) || rotation > 270 || rotation % 90)
		return -EINVAL;
	mutex_lock(&p->io_lock);
	/* Keep dimensions, stride, fbcon and existing mmap mappings stable. */
	if (rotation % 180 != p->rotation % 180) {
		mutex_unlock(&p->io_lock);
		return -EINVAL;
	}
	if (rotation != p->rotation) {
		WRITE_ONCE(p->rotation, rotation);
		co5300fb_damage(info, 0, p->height - 1, p->defio.delay);
	}
	mutex_unlock(&p->io_lock);
	return count;
}
static DEVICE_ATTR_RW(rotation);

static struct attribute *co5300fb_attrs[] = {
	&dev_attr_flush_stats.attr,
	&dev_attr_render_stats.attr,
	&dev_attr_panel_resume.attr,
	&dev_attr_spi_freq_hz.attr,
	&dev_attr_rotation.attr,
	NULL,
};
ATTRIBUTE_GROUPS(co5300fb);

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
	struct fb_deferred_io_pageref *ref;
	unsigned long flags;
	u32 first, last;
	int ret;

	spin_lock_irqsave(&p->damage_lock, flags);
	if (p->stopped || (!p->dirty && list_empty(pages))) {
		spin_unlock_irqrestore(&p->damage_lock, flags);
		return;
	}
	first = p->dirty ? p->first : p->height;
	last = p->dirty ? p->last : 0;
	p->dirty = false;
	spin_unlock_irqrestore(&p->damage_lock, flags);
	/* mmap damage covers touched pages, not an unconditional full frame. */
	list_for_each_entry(ref, pages, list) {
		u32 off = ref->offset, end;

		if (off >= info->fix.smem_len)
			continue;
		end = min_t(u32, off + PAGE_SIZE, info->fix.smem_len);
		first = min(first, off / info->fix.line_length);
		last = max(last, (end - 1) / info->fix.line_length);
	}
	if (first >= p->height)
		return;
	/* The failing login trace has successful full clears followed only by
	 * partial cursor windows. Refresh the complete, shared screen_buffer by
	 * default so text, raw writes and mmap damage all reach the proven DMA
	 * window. Keep partial windows available for on-board A/B diagnosis. */
	if (!READ_ONCE(partial_updates)) {
		first = 0;
		last = p->height - 1;
	} else {
		/* Include the unchanged neighbour to keep start/extent even. */
		first &= ~1U;
		last = min(last | 1U, p->height - 1);
	}
	ret = co5300fb_flush(info, first, last);
	if (ret) {
		dev_err_ratelimited(&p->spi->dev,
				    "rows %u..%u transfer failed: %d\n", first, last, ret);
		co5300fb_damage(info, first, last, HZ);
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
	vfree(p->pixels);
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
		/* Keep the proven supplier sequence; rotation is software only.
		 * Display On (0x29) follows this table.
		 */
		0xfe, 1, 0, 0, 0x00,
		0xc4, 1, 0, 0, 0x80,
		0x3a, 1, 0, 0, 0x55,
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
	mutex_init(&p->io_lock);
	/* Defaults are the C05300AF-51 module on this shield, landscape
	 * (502x410) via software rotation; DT properties
	 * (chipone,width/height/x-offset/y-offset) override them. */
	p->width = 502;
	p->height = 410;
	p->xoff = 0;
	p->yoff = 22;
	device_property_read_u32(dev, "chipone,width", &p->width);
	device_property_read_u32(dev, "chipone,height", &p->height);
	device_property_read_u32(dev, "chipone,x-offset", &p->xoff);
	device_property_read_u32(dev, "chipone,y-offset", &p->yoff);
	device_property_read_u32(dev, "rotation", &p->rotation);
	if (!p->width || !p->height || p->width > 512 || p->height > 512 ||
	    p->xoff > 65535 - p->width || p->yoff > 65535 - p->height ||
	    ((p->width | p->height | p->xoff | p->yoff) & 1) ||
	    p->rotation > 270 || p->rotation % 90) {
		ret = -EINVAL;
		goto fail;
	}
	if (p->rotation == 90 || p->rotation == 270)
		swap(p->width, p->height);
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
	/* GPSPI PIO reads this buffer directly; avoid a high-order allocation. */
	p->pixels = vzalloc(p->width * p->height * 2);
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
	dev_info(dev, "fb%d: %ux%u RGB565 QSPI console at %u Hz (tiled single-window)\n",
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
		.dev_groups = co5300fb_groups,
	},
	.probe = co5300fb_probe,
	.remove = co5300fb_remove,
	.id_table = co5300fb_ids,
};
module_spi_driver(co5300fb_driver);
MODULE_DESCRIPTION("CO5300 QSPI AMOLED framebuffer console");
MODULE_LICENSE("GPL");
