#!/bin/sh
# Run from serial/SSH, not from the framebuffer VT.
# Snapshot Linux framebuffer RAM; this never reads the SPI panel.
set -eu
test "$(cat /sys/class/graphics/fb0/virtual_size)" = "502,410"
stats=
for file in /sys/bus/spi/devices/*/flush_stats; do
	if [ -r "$file" ]; then
		stats=$file
		break
	fi
done
test -n "$stats" || { echo "flush_stats missing: flash the new kernel" >&2; exit 1; }
frame=/tmp/amoled-speed-$$.rgb565
trap 'rm -f "$frame"' EXIT
trap 'exit 1' HUP INT TERM
cat "$stats"
dd if=/dev/fb0 of="$frame" bs=411640 count=1
test "$(wc -c < "$frame")" -eq 411640
# Re-send the same image so the existing login prompt remains visible.
for sample in 1 2 3 4 5; do
	printf 'sample %s\n' "$sample"
	dd if="$frame" of=/dev/fb0 bs=411640 count=1
	sleep 1
	cat "$stats"
	for dma_stats in /sys/class/spi_master/spi*/device/dma_stats; do
		if [ -r "$dma_stats" ]; then
			cat "$dma_stats"
		fi
	done
done
