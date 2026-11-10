"""macOS installer media support.

Apple does not ship ISO images: the installer is a DMG (Apple Disk Image,
UDIF format) holding an HFS+ volume with the installer payload. Writing a
bootable macOS USB from Linux therefore means:

1. converting the DMG into a raw disk image (``dmg2img``),
2. putting the drive into the layout Apple's own ``createinstallmedia``
   uses: a small EFI system partition with the bootloader plus an
   Apple_HFS partition with the installer volume,
3. copying the bootloader into the EFI partition and the HFS+ volume into
   the second partition.

The HFS+ volume is written as raw blocks - it is never mounted, so no
special filesystem support is needed on the Linux side.
"""
import os
import shutil
import struct
import subprocess
from dataclasses import dataclass
from typing import List, Optional

from ..utils.logging import get_logger

log = get_logger(__name__)

# Apple's disk images end with a 512 byte "koly" trailer.
DMG_TRAILER_SIZE = 512
DMG_MAGIC = b"koly"

# HFS+ volume header lives 1024 bytes into the volume.
HFSPLUS_HEADER_OFFSET = 1024
HFSPLUS_SIGNATURES = (b"H+", b"HX")

# The EFI partition of a macOS installer USB is 200 MB, like Apple's own
# createinstallmedia.
EFI_PARTITION_SIZE = 200 * 1024 * 1024

# GPT partition type of an Apple HFS+ volume.
APPLE_HFS_GUID = "7c3457ef-0000-11aa-aa11-00306543ecac"

# How far into the raw image the HFS+ volume is looked for.
HFSPLUS_SEARCH_LIMIT = 8 * 1024 * 1024


class MacosError(Exception):
    """A macOS image could not be prepared."""


@dataclass
class DmgInfo:
    """The contents of the DMG "koly" trailer."""
    version: int
    data_length: int
    xml_offset: int
    xml_length: int
    segment_count: int


@dataclass
class HfsPlusVolume:
    """An HFS+ volume found inside a raw image."""
    offset: int          # where the volume starts in the raw image
    size: int            # size of the volume in bytes
    block_size: int
    total_blocks: int


def is_dmg(path: str) -> bool:
    """Whether the file is an Apple disk image."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return False
    if size < DMG_TRAILER_SIZE:
        return False
    try:
        with open(path, "rb") as handle:
            handle.seek(-DMG_TRAILER_SIZE, os.SEEK_END)
            return handle.read(4) == DMG_MAGIC
    except OSError:
        return False


def read_dmg_info(path: str) -> Optional[DmgInfo]:
    """Parse the "koly" trailer of a DMG."""
    try:
        size = os.path.getsize(path)
        if size < DMG_TRAILER_SIZE:
            return None
        with open(path, "rb") as handle:
            handle.seek(-DMG_TRAILER_SIZE, os.SEEK_END)
            trailer = handle.read(DMG_TRAILER_SIZE)
    except OSError:
        return None

    if trailer[0:4] != DMG_MAGIC:
        return None

    # The "koly" trailer is big endian, like every other Apple structure.
    values = struct.unpack_from(">16I", trailer, 0)
    return DmgInfo(
        version=values[1],
        data_length=values[5],
        xml_offset=values[12],
        xml_length=values[13],
        segment_count=values[9],
    )


def find_hfsplus_volume(raw_path: str) -> Optional[HfsPlusVolume]:
    """Locate the HFS+ volume inside a raw disk image.

    The volume header sits 1024 bytes into the volume and starts with the
    "H+" (or "HX") signature, which makes it easy to find even when the
    image carries a partition table in front of it.
    """
    try:
        with open(raw_path, "rb") as handle:
            limit = min(HFSPLUS_SEARCH_LIMIT, os.path.getsize(raw_path))
            handle.seek(0)
            blob = handle.read(limit)
    except OSError:
        return None

    for offset in range(0, max(0, len(blob) - HFSPLUS_HEADER_OFFSET), 512):
        marker = blob[offset + HFSPLUS_HEADER_OFFSET:offset + HFSPLUS_HEADER_OFFSET + 2]
        if marker not in HFSPLUS_SIGNATURES:
            continue
        header = blob[offset + HFSPLUS_HEADER_OFFSET:offset + HFSPLUS_HEADER_OFFSET + 64]
        if len(header) < 48:
            continue
        # The HFS+ volume header is big endian.
        block_size = struct.unpack_from(">I", header, 40)[0]
        total_blocks = struct.unpack_from(">I", header, 44)[0]
        if block_size == 0 or total_blocks == 0:
            continue
        if block_size > 1024 * 1024:
            continue
        size = block_size * total_blocks
        # The volume must fit in the image.
        try:
            image_size = os.path.getsize(raw_path)
        except OSError:
            image_size = 0
        if image_size and offset + size > image_size:
            continue
        return HfsPlusVolume(
            offset=offset,
            size=size,
            block_size=block_size,
            total_blocks=total_blocks,
        )
    return None


def _dmg2img_binary() -> Optional[str]:
    return shutil.which("dmg2img")


def extract_dmg(dmg_path: str, out_path: str, timeout: int = 3600) -> str:
    """Convert a DMG into a raw disk image.

    Returns the path of the raw image. ``dmg2img`` is used because it
    understands every compression Apple uses (LZFSE, ADC, zlib, bzip2).
    """
    binary = _dmg2img_binary()
    if not binary:
        raise MacosError(
            "dmg2img is required to write macOS images but was not found. "
            "Install it with: sudo dnf install dmg2img"
        )

    result = subprocess.run(
        [binary, "-i", dmg_path, "-o", out_path],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        message = detail[-1] if detail else f"exit code {result.returncode}"
        raise MacosError(f"Could not convert the macOS image: {message}")
    if not os.path.exists(out_path):
        raise MacosError("Could not convert the macOS image: no output file")
    return out_path


def extract_dmg_tree(dmg_path: str, out_dir: str, timeout: int = 3600) -> Optional[str]:
    """Extract the files of a DMG with 7-Zip.

    Only used to look for the EFI bootloader; when it fails the macOS
    volume is still written, the USB just is not directly bootable.
    """
    binary = shutil.which("7z") or shutil.which("7za") or shutil.which("7zr")
    if not binary:
        return None
    try:
        result = subprocess.run(
            [binary, "x", "-y", f"-o{out_dir}", dmg_path],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("Could not extract %s with 7-Zip: %s", dmg_path, exc)
        return None
    if result.returncode != 0:
        log.debug("7-Zip could not extract %s: %s", dmg_path, result.stderr[-400:])
        return None
    return out_dir


def find_bootloader(root: str) -> Optional[str]:
    """Find the EFI bootloader inside an extracted installer tree.

    Apple ships the bootloader as ``boot.efi`` inside the ``Preboot``
    directory of the OS payload; some images use ``BOOTX64.EFI`` directly.
    """
    if not root or not os.path.isdir(root):
        return None

    best: Optional[str] = None
    best_score = -1
    for dirpath, _dirnames, filenames in os.walk(root):
        for filename in filenames:
            lowered = filename.lower()
            if lowered not in ("boot.efi", "bootx64.efi", "bootx32.efi"):
                continue
            path = os.path.join(dirpath, filename)
            rel = os.path.relpath(path, root).lower()
            score = 0
            if "preboot" in rel:
                score += 10
            if "efi/boot" in rel.replace("\\", "/"):
                score += 5
            if lowered == "bootx64.efi":
                score += 2
            if score > best_score:
                best = path
                best_score = score
    return best


def bootloader_files(root: str, bootloader: str) -> List[tuple]:
    """The files that belong next to the bootloader in the EFI partition.

    Apple's EFI partition keeps the bootloader next to its helpers, so the
    whole directory the bootloader lives in is copied.
    """
    files = []
    directory = os.path.dirname(bootloader)
    if not directory or not os.path.isdir(directory):
        return [(bootloader, os.path.basename(bootloader))]
    for name in sorted(os.listdir(directory)):
        source = os.path.join(directory, name)
        if os.path.isfile(source):
            files.append((source, name))
    if not files:
        files.append((bootloader, os.path.basename(bootloader)))
    return files
