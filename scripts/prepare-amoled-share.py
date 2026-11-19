#!/usr/bin/env python3
"""Validate and prepare the user-tested PIO pair for ESP Forge + separate USB download.
Run on WSL/build host. Does not build, flash, upload, or modify an input image.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile

PIO_COMMIT = "ab6cd0be1ff52c1baac86ac93629af7b88e7a33b"
REPO = Path(__file__).resolve().parent.parent


def git_blob(path):
    return subprocess.check_output(["git", "show", f"{PIO_COMMIT}:{path}"], cwd=REPO)


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for data in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            h.update(data)
    return h.hexdigest()


def executable_lines(data):
    return [line.strip() for line in data.decode().splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def debugfs(image, path):
    result = subprocess.run(["debugfs", "-R", f"cat {path}", str(image)],
                            capture_output=True, check=True)
    if not result.stdout:
        raise ValueError(f"rootfs is missing {path}")
    return result.stdout


def check_rootfs(sdcard):
    with sdcard.open("rb") as stream:
        mbr = stream.read(512)
        if len(mbr) != 512 or mbr[510:] != b"\x55\xaa":
            raise ValueError("sdcard.img has no valid MBR")
        if struct.unpack_from("<I", mbr, 440)[0] != 0x45533331:
            raise ValueError("USB PARTUUID does not match the kernel")
        entry = mbr[462:478]  # Second partition entry: Linux rootfs.
        start, sectors = struct.unpack_from("<II", entry, 8)
        if entry[4] != 0x83 or not sectors or (start + sectors) * 512 > sdcard.stat().st_size:
            raise ValueError("sdcard.img has no complete Linux root partition")
        stream.seek(start * 512)
        with tempfile.NamedTemporaryFile(prefix="co5300-share-root-", suffix=".img") as root:
            left = sectors * 512
            while left:
                data = stream.read(min(left, 4 * 1024 * 1024))
                if not data:
                    raise ValueError("truncated root partition")
                # Preserve zero ranges as holes; no mount/root privileges needed.
                if data.count(0) == len(data):
                    root.seek(len(data), 1)
                else:
                    root.write(data)
                left -= len(data)
            root.truncate(sectors * 512)
            root.flush()
            wrapper = debugfs(Path(root.name), "/sbin/amoled-getty")
            if executable_lines(wrapper) != executable_lines(
                    git_blob("br2-external/board/esp32s31/amoled-getty")):
                raise ValueError("USB getty wrapper differs from the tested PIO wrapper")
            inittab = debugfs(Path(root.name), "/etc/inittab").decode()
            if [x for x in inittab.splitlines() if x.startswith("tty0::respawn:")] != [
                    "tty0::respawn:/sbin/amoled-getty"]:
                raise ValueError("USB rootfs must have exactly one AMOLED wrapper getty")
            mode = subprocess.check_output(
                ["debugfs", "-R", "stat /sbin/amoled-getty", root.name],
                stderr=subprocess.DEVNULL).decode()
            if "Mode:  0755" not in mode and "Mode: 0755" not in mode:
                raise ValueError("USB getty wrapper must be executable (0755)")
            return debugfs(Path(root.name), "/usr/lib/os-release").decode()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--flash", type=Path, default=REPO / "build/coreboard1/flash.bin")
    parser.add_argument("--sdcard", type=Path, default=REPO / "build/coreboard1/buildroot/sdcard.img")
    parser.add_argument("--kernel-build", type=Path,
                        default=REPO / "build/br-output-coreboard1-amoled/build/linux-7.3.4")
    parser.add_argument("--output", type=Path, default=REPO / "build/share-co5300-pio")
    parser.add_argument("--usb-url", default="", help="Public URL of this exact sdcard.img")
    args = parser.parse_args()
    for path in [args.flash, args.sdcard]:
        if not path.is_file() or not path.stat().st_size:
            raise ValueError(f"missing artifact: {path}")
    for path in ["drivers/spi/spi-esp32s31.c", "drivers/video/fbdev/co5300fb.c",
                 "drivers/clk/clk-esp32s31.c",
                 "arch/riscv/boot/dts/espressif/esp32s31_coreboard1_amoled.dts",
                 "arch/riscv/boot/dts/espressif/esp32s31.dtsi"]:
        if (args.kernel_build / path).read_bytes() != git_blob("linux/" + path):
            raise ValueError(f"kernel source differs from the tested PIO revision: {path}")
    config = (args.kernel_build / ".config").read_text()
    for setting in ['CONFIG_FB_CO5300=y', 'CONFIG_FRAMEBUFFER_CONSOLE=y',
                    'CONFIG_BUILTIN_DTB_NAME="espressif/esp32s31_coreboard1_amoled"']:
        if setting not in config.splitlines():
            raise ValueError(f"wrong kernel configuration: {setting}")
    if "CONFIG_ESP32S31_AXI_GDMA=y" in config.splitlines():
        raise ValueError("DMA image cannot be labelled as the user-tested PIO release")
    image = (args.kernel_build / "arch/riscv/boot/Image").read_bytes()
    flash = args.flash.read_bytes()
    # Linux 7.3.4 arch/riscv/include/asm/image.h: 64-byte boot header.
    if len(image) < 64 or image[48:56] != b"RISCV\x00\x00\x00" or image[56:60] != b"RSC\x05":
        raise ValueError("kernel Image has no valid RISC-V boot header")
    if not image or image not in flash or b"tiled single-window" not in image:
        raise ValueError("flash.bin does not contain this PIO kernel Image")
    if b"AXI GDMA TX SG" in image:
        raise ValueError("DMA kernel cannot be labelled as PIO")
    os_release = check_rootfs(args.sdcard)
    main_head = subprocess.check_output(["git", "ls-remote", "origin", "refs/heads/main"],
                                        cwd=REPO, text=True).split()[0]
    subprocess.run(["git", "merge-base", "--is-ancestor", PIO_COMMIT, main_head],
                   cwd=REPO, check=True)
    hashes = {p.name: sha256(p) for p in [args.flash, args.sdcard]}
    args.output.mkdir(parents=True, exist_ok=True)
    for src, name in [(args.flash, "flash.bin"), (args.sdcard, "sdcard.img")]:
        dst = args.output / name
        if dst.exists() and sha256(dst) != hashes[src.name]:
            raise ValueError(f"refusing to overwrite a different release artifact: {dst}")
        if not dst.exists():
            shutil.copyfile(src, dst)
        if sha256(dst) != hashes[src.name]:
            raise ValueError(f"copy checksum failed: {dst}")
    manifest = {
        "kind": "user-tested PIO, not DMA",
        "source_commit": PIO_COMMIT, "main_contains_source": True,
        "kernel_build": str(args.kernel_build.resolve()),
        "rootfs_os_release": os_release,
        "hardware_test": "User 2026-11-19: median 87667 us, 3/5 samples below 100 ms, errors=0",
        "checks": ["kernel source matches PIO revision", "Image present in flash.bin",
                   "USB MBR/PARTUUID", "wrapper content/executable mode", "single tty0 getty",
                   "copied artifact SHA256"],
        "files": {name: {"sha256": hashes[src.name], "bytes": src.stat().st_size}
                  for src, name in [(args.flash, "flash.bin"), (args.sdcard, "sdcard.img")]},
        "usb_url": args.usb_url or None,
        "upload_status": "prepared locally; no upload performed",
    }
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (args.output / "SHA256SUMS").write_text(
        "".join(f"{manifest['files'][name]['sha256']}  {name}\n" for name in ["flash.bin", "sdcard.img"]))
    description = (
        "ESP32-S31 Linux on CO5300 AMOLED (410x502 native, 502x410 landscape, 40 MHz QSPI).\n"
        "1. Flash flash.bin at offset 0x0 using the ESP Forge Web Flasher.\n"
        f"2. Download sdcard.img: {args.usb_url or '[LINK_SDCARD_IMG]'}\n"
        "Write sdcard.img to a USB stick using balenaEtcher; plug it into USB-A and power on.\n"
        "Linux kernel boot log and a clean login prompt appear on the panel.\n"
        "User-tested PIO polling: median ~87 ms/frame; a light scan line remains. DMA is in development.\n"
        f"Source: https://github.com/pangcrd/esp32-s31-linux-amoled/tree/{PIO_COMMIT}\n"
        "SSH uses keys. Add your own public key via the serial console before using SSH.\n"
    )
    (args.output / "forge-description.txt").write_text(description)
    print(f"Prepared {args.output}: flash.bin, sdcard.img, SHA256SUMS, manifest.json, forge-description.txt")
    if not args.usb_url:
        print("Add the sdcard.img download URL before posting on ESP Forge.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, FileNotFoundError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"Share pack not ready: {error}")
