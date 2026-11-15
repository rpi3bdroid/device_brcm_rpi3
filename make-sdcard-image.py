#!/usr/bin/env python3
"""Package the README's partitions into an MBR SD-card image without root."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[3]
PRODUCT = ROOT / "out/target/product/rpi3"
MIB = 1024 * 1024
BLOCK = 4 * MIB


def run(*args, **kwargs):
    subprocess.run([str(a) for a in args], check=True, **kwargs)


def tool(name):
    candidates = [ROOT / "out/host-compat/usr/bin" / name]
    if name == "mke2fs":
        candidates.insert(0, ROOT / "out/host/linux-x86/bin/mke2fs")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    found = shutil.which(name)
    if not found:
        raise SystemExit("Missing host tool: " + name)
    return found


def digest(path, offset=0, length=None):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        stream.seek(offset)
        while length is None or length > 0:
            chunk = stream.read(BLOCK if length is None else min(BLOCK, length))
            if not chunk:
                if length:
                    raise RuntimeError("Unexpected EOF: " + str(path))
                break
            result.update(chunk)
            if length is not None:
                length -= len(chunk)
    return result.hexdigest()


def copy_partition(source, image, offset):
    # Leave all-zero blocks as holes in the newly created image.
    with Path(source).open("rb") as stream:
        image.seek(offset)
        while True:
            chunk = stream.read(BLOCK)
            if not chunk:
                break
            if chunk == bytes(len(chunk)):
                image.seek(len(chunk), os.SEEK_CUR)
            else:
                image.write(chunk)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size-mib", type=int, default=4096,
                        help="total image size; default 4096 MiB")
    parser.add_argument("--output", type=Path,
                        default=PRODUCT / "rpi3-android10-sdcard.img")
    args = parser.parse_args()
    if args.size_mib < 2048:
        parser.error("image must be at least 2048 MiB")
    output = args.output.resolve()
    partial = output.with_suffix(output.suffix + ".partial")
    if output.exists() or partial.exists():
        parser.error("output or .partial already exists; choose a new --output")

    boot_source = ROOT / "device/brcm/rpi3/boot"
    kernel = PRODUCT / "KERNEL_OBJ/arch/arm/boot"
    boot_extra = {
        "zImage": kernel / "zImage",
        "bcm2710-rpi-3-b.dtb": kernel / "dts/bcm2710-rpi-3-b.dtb",
        "overlays/vc4-kms-v3d.dtbo": kernel / "dts/overlays/vc4-kms-v3d.dtbo",
        "ramdisk.img": PRODUCT / "ramdisk.img",
    }
    system, vendor = PRODUCT / "system.img", PRODUCT / "vendor.img"
    for path in [system, vendor, *boot_extra.values(),
                 boot_source / "bootcode.bin", boot_source / "start.elf",
                 boot_source / "fixup.dat", boot_source / "config.txt",
                 boot_source / "cmdline.txt"]:
        if not path.is_file():
            raise SystemExit("Missing build artifact: " + str(path))
    for path, limit in [(system, 640 * MIB), (vendor, 128 * MIB)]:
        with path.open("rb") as stream:
            if stream.read(4) == struct.pack("<I", 0xED26FF3A):
                raise SystemExit("Convert Android sparse image with simg2img: " + str(path))
        if path.stat().st_size > limit:
            raise SystemExit("Image exceeds README partition size: " + str(path))

    tools = {name: tool(name) for name in
             ["sfdisk", "mkfs.vfat", "mcopy", "mke2fs", "e2fsck", "fsck.fat"]}
    env = os.environ.copy()
    env["MKE2FS_CONFIG"] = str(ROOT / "system/extras/ext4_utils/mke2fs.conf")
    env["MTOOLSRC"] = "/dev/null"
    env["LC_ALL"] = "C"
    output.parent.mkdir(parents=True, exist_ok=True)
    layout = [
        {"name": "BOOT", "start_mib": 1, "size_mib": 256, "type": "c"},
        {"name": "system", "start_mib": 257, "size_mib": 640, "type": "83"},
        {"name": "vendor", "start_mib": 897, "size_mib": 128, "type": "83"},
        {"name": "userdata", "start_mib": 1025,
         "size_mib": args.size_mib - 1025, "type": "83"},
    ]
    with tempfile.TemporaryDirectory(prefix="sdcard-", dir=output.parent) as tmp:
        work = Path(tmp)
        boot, data = work / "boot.img", work / "userdata.img"
        for path, size in [(boot, 256 * MIB),
                           (data, layout[3]["size_mib"] * MIB)]:
            with path.open("xb") as stream:
                stream.truncate(size)
        run(tools["mkfs.vfat"], "-F", "32", "-n", "BOOT", boot, env=env)
        staging = work / "boot-files"
        shutil.copytree(boot_source, staging)
        for name, source in boot_extra.items():
            dest = staging / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, dest)
        run(tools["mcopy"], "-s", "-i", boot,
            *sorted(staging.iterdir()), "::/", env=env)
        run(tools["mke2fs"], "-F", "-t", "ext4", "-b", "4096",
            "-I", "256", "-L", "userdata", "-m", "0", data, env=env)

        run(tools["fsck.fat"], "-n", boot, env=env)
        for path in [system, vendor, data]:
            run(tools["e2fsck"], "-f", "-n", path, env=env)
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                extracted = work / "verify-boot-file"
                run(tools["mcopy"], "-o", "-i", boot,
                    "::/" + path.relative_to(staging).as_posix(), extracted, env=env)
                if digest(path) != digest(extracted):
                    raise RuntimeError("BOOT verification failed: " + str(path))

        with partial.open("xb") as image:
            image.truncate(args.size_mib * MIB)
        table = "label: dos\nunit: sectors\n\n" + "\n".join(
            "start={start}, size={size}, type={type}{bootable}".format(
                start=p["start_mib"] * 2048, size=p["size_mib"] * 2048,
                type=p["type"], bootable=", bootable" if i == 0 else "")
            for i, p in enumerate(layout)) + "\n"
        run(tools["sfdisk"], partial, input=table, text=True, env=env)
        sources = [boot, system, vendor, data]
        with partial.open("r+b") as image:
            for partition, source in zip(layout, sources):
                print("Writing " + partition["name"], flush=True)
                copy_partition(source, image, partition["start_mib"] * MIB)
            image.flush()
            os.fsync(image.fileno())
        for partition, source in zip(layout, sources):
            size = source.stat().st_size
            expected = digest(source)
            if digest(partial, partition["start_mib"] * MIB, size) != expected:
                raise RuntimeError("Partition verification failed: " + partition["name"])
            partition["sha256"] = expected
        run(tools["sfdisk"], "--verify", partial, env=env)
        checksum = digest(partial)
        partial.rename(output)
        output.with_suffix(".img.sha256").write_text(checksum + "  " + output.name + "\n")
        output.with_suffix(".img.layout.json").write_text(json.dumps({
            "image": output.name, "size_bytes": args.size_mib * MIB,
            "partition_table": "MBR", "sector_size": 512,
            "sha256": checksum, "partitions": layout,
        }, indent=2) + "\n")
    print("Ready: " + str(output), flush=True)
    print("SHA256: " + checksum, flush=True)


if __name__ == "__main__":
    main()
