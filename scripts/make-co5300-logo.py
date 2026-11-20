#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Convert an 8-bit binary PPM (P6) to LE RGB565, or create the sample logo."""
import argparse
from pathlib import Path
import re
import struct


def ppm(data):
    pos = 0
    tokens = []
    for _ in range(4):
        while pos < len(data):
            if data[pos:pos + 1].isspace():
                pos += 1
            elif data[pos:pos + 1] == b"#":
                end = data.find(b"\n", pos)
                if end < 0:
                    raise ValueError("Unterminated PPM comment")
                pos = end + 1
            else:
                break
        match = re.match(rb"[^\s#]+", data[pos:])
        if not match:
            raise ValueError("Incomplete PPM header")
        tokens.append(match[0])
        pos += len(match[0])
    if tokens[0] != b"P6" or tokens[3] != b"255":
        raise ValueError("Expected binary PPM P6 with maxval 255")
    width, height = int(tokens[1]), int(tokens[2])
    if not data[pos:pos + 1].isspace():
        raise ValueError("Missing PPM raster separator")
    pos += 2 if data[pos:pos + 2] == b"\r\n" else 1
    pixels = data[pos:]
    if len(pixels) != width * height * 3:
        raise ValueError("PPM raster size does not match its dimensions")
    return width, height, pixels


def sample():
    width, height = 240, 120
    pixels = bytearray(width * height * 3)

    def rect(x, y, w, h, color):
        for row in range(y, y + h):
            for col in range(x, x + w):
                off = (row * width + col) * 3
                pixels[off:off + 3] = bytes(color)

    cyan, white = (32, 208, 224), (240, 248, 255)
    rect(16, 34, 52, 52, cyan)
    rect(20, 38, 44, 44, (8, 24, 32))
    for pin in range(4):
        rect(24 + pin * 10, 27, 4, 7, cyan)
        rect(24 + pin * 10, 86, 4, 7, cyan)
        rect(9, 42 + pin * 10, 7, 4, cyan)
        rect(68, 42 + pin * 10, 7, 4, cyan)
    rect(29, 47, 26, 26, white)
    glyphs = {
        "E": [31, 16, 16, 30, 16, 16, 31],
        "S": [15, 16, 16, 14, 1, 1, 30],
        "P": [30, 17, 17, 30, 16, 16, 16],
        "3": [30, 1, 1, 14, 1, 1, 30],
        "2": [14, 17, 1, 2, 4, 8, 31],
        "L": [16, 16, 16, 16, 16, 16, 31],
        "I": [31, 4, 4, 4, 4, 4, 31],
        "N": [17, 25, 25, 21, 19, 19, 17],
        "U": [17, 17, 17, 17, 17, 17, 14],
        "X": [17, 17, 10, 4, 10, 17, 17],
    }
    for text, top, color in [("ESP32", 34, white), ("LINUX", 65, cyan)]:
        for i, letter in enumerate(text):
            for y, bits in enumerate(glyphs[letter]):
                for x in range(5):
                    if bits & (1 << (4 - x)):
                        rect(88 + i * 27 + x * 4, top + y * 3, 4, 3, color)
    return width, height, pixels


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--sample", action="store_true")
    args = parser.parse_args()
    if len(args.paths) != (1 if args.sample else 2):
        parser.error("Use --sample OUTPUT, or INPUT.ppm OUTPUT.rgb565")
    width, height, pixels = sample() if args.sample else ppm(args.paths[0].read_bytes())
    if not (0 < width <= 422 and 0 < height <= 330 or
            0 < width <= 330 and 0 < height <= 422):
        raise ValueError("Logo must fit within 422x330 landscape or 330x422 portrait")
    out = bytearray()
    for r, g, b in zip(pixels[0::3], pixels[1::3], pixels[2::3]):
        out += struct.pack("<H", ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3))
    args.paths[-1].write_bytes(out)
    print(f"{args.paths[-1]}: {width}x{height}, {len(out)} bytes, RGB565 little-endian")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as error:
        raise SystemExit(str(error))
