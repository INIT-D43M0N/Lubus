"""Detection of BSD and other Unix boot media.

The BSDs ship their installation media in two very different shapes:

* Hybrid ISOs, which carry a protective MBR in the system area and can
  therefore be copied to a USB drive byte for byte (FreeBSD's ``disc1``,
  the OmniOS installer, the Haiku "anyboot" image).
* Raw disk images (``installXX.img``, ``*-install.img``, ``memstick.img``,
  ``*.usb-dd``), which are disk images with an MBR or a GPT of their own.

And some ISOs are *not* bootable from a USB drive at all: the OpenBSD,
NetBSD and DragonFly installation CDs are plain ISO9660 files meant for
an optical drive. Those are recognised as well, so the application can
tell the user to fetch the ``.img`` file instead.

The markers were taken from the images themselves: the paths below are
the ones the ISO9660 parser of :mod:`yaku.core.images` produces, and the
partition types are the ones the BSDs put in their images.
"""
import os
import re
from dataclasses import dataclass
from typing import List, Optional

from .gpt import read_gpt

# ------------------------------------------------------- ISO tree markers

# FreeBSD keeps its distribution tree in "usr/freebsd_dist" in the primary
# ISO9660 tree (the Rock Ridge name would be "usr/freebsd-dist"). The boot
# files "boot/isoboot" and "boot/pmbr" are not used as markers: the illumos
# installer carries the very same ones.
FREEBSD_ISO_FILES = frozenset({
    "usr/freebsd_dist/base.txz",
    "usr/freebsd_dist/kernel.txz",
    "etc/freebsd_update.conf",
})
FREEBSD_ISO_PREFIXES = ("usr/freebsd_dist/",)

# OpenBSD puts everything below a version directory ("8.0/amd64/..."), so
# the paths have to be matched with a pattern.
OPENBSD_ISO_PATTERNS = (
    re.compile(r"^\d+\.\d+/amd64/bsd\.rd$"),
    re.compile(r"^\d+\.\d+/amd64/cdboot$"),
    re.compile(r"^\d+\.\d+/amd64/eficdboot$"),
    re.compile(r"^\d+\.\d+/amd64/cdbr$"),
    re.compile(r"^\d+\.\d+/amd64/buildinfo$"),
    re.compile(r"^\d+\.\d+/amd64/install\.\w+$"),
)

# NetBSD keeps the installation tools under the machine architecture.
NETBSD_ISO_FILES = frozenset({
    "etc/release",
    "boot.cfg",
    "amd64/installation/miniroot/miniroot.kmod",
    "amd64/installation/ramdisk/ramdisk.fs",
    "amd64/binary/kernel/netbsd_install.gz",
})
NETBSD_ISO_PREFIXES = ("amd64/installation/", "amd64/binary/sets/")

# DragonFly uses "etc.hdd" for the installation "etc" directory.
DRAGONFLY_ISO_FILES = frozenset({
    "boot/efiboot.img",
    "readme.usb",
    "dflybsd.ico",
})
DRAGONFLY_ISO_PREFIXES = ("etc.hdd/",)

# illumos (OmniOS, OpenIndiana) uses the Kayak installer.
ILLUMOS_ISO_FILES = frozenset({
    ".volsetid",
    ".initialboot",
    "boot/gptzfsboot",
    "kayak/installer/install-image",
})
ILLUMOS_ISO_PREFIXES = ("kayak/", "image/")

# Haiku keeps the system itself in a BFS partition, the ISO only carries
# the boot floppy and the EFI image.
HAIKU_ISO_FILES = frozenset({
    "esp.image",
    "haiku-boot-floppy.image",
})

# GNU/Hurd (Debian's port) ships the GNU Mach kernel in the ISO.
HURD_ISO_FILES = frozenset({
    "boot/kernel/gnumach.gz",
    "boot/kernel/exec.static",
})
HURD_ISO_PREFIXES = ("pool-hurd-i386/", "dists/sid/main/binary-hurd-i386/")

# ------------------------------------------------------ partition markers

# MBR partition types the BSDs use for their own partitions.
MBR_FREEBSD = 0xA5
MBR_OPENBSD = 0xA6
MBR_NETBSD = 0xA9
MBR_DRAGONFLY = 0x6C
MBR_HAIKU = 0xEB
MBR_SOLARIS = 0xBE
MBR_EFI_SYSTEM = 0xEF
MBR_ISO_HYBRID = 0xCD

MBR_TYPES = {
    MBR_FREEBSD: "FreeBSD",
    MBR_OPENBSD: "OpenBSD",
    MBR_NETBSD: "NetBSD",
    MBR_DRAGONFLY: "DragonFly BSD",
    MBR_HAIKU: "Haiku",
    MBR_SOLARIS: "illumos",
}

# GPT partition type GUIDs.
GPT_FREEBSD_BOOT = "83bd6b9d-7f41-11dc-be0b-001560b84f0f"
GPT_NETBSD_FFS = "49f48d5a-b10e-11dc-b99b-0019d1879648"
GPT_HAMMER = "61dc63ac-6e38-11dc-8513-01301bb8a9f5"
GPT_ILLUMOS_BOOT = "6a82cb45-1dd2-11b2-99a6-080020736631"
GPT_ILLUMOS_ROOT = "6a85cf4d-1dd2-11b2-99a6-080020736631"
GPT_ILLUMOS_RESERVED = "6a945a3b-1dd2-11b2-99a6-080020736631"
GPT_EFI = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"

GPT_TYPES = {
    GPT_FREEBSD_BOOT: "FreeBSD",
    GPT_NETBSD_FFS: "NetBSD",
    GPT_HAMMER: "DragonFly BSD",
    GPT_ILLUMOS_BOOT: "illumos",
    GPT_ILLUMOS_ROOT: "illumos",
    GPT_ILLUMOS_RESERVED: "illumos",
}


@dataclass
class UnixImage:
    """A BSD or Unix image that Yaku recognised."""

    name: str                       # "FreeBSD", "OpenBSD", ...
    bootable: bool                  # can be copied to a USB drive with dd
    is_iso: bool = True
    efi: bool = False
    bios: bool = False
    advice: str = ""                # what to do when it is not bootable


def _matches_files(paths: set, files, prefixes=()) -> bool:
    """True when any exact file or path prefix is present."""
    if any(path in paths for path in files):
        return True
    return any(path.startswith(prefix) for path in paths for prefix in prefixes)


def _matches_patterns(paths: set, patterns) -> bool:
    """True when any path matches one of the regular expressions."""
    return any(pattern.search(path) for pattern in patterns for path in paths)


def has_mbr_signature(path: str) -> bool:
    """Whether the first sector carries the 0x55AA boot signature."""
    try:
        with open(path, "rb") as handle:
            sector = handle.read(512)
    except OSError:
        return False
    return len(sector) >= 512 and sector[510:512] == b"\x55\xaa"


def read_mbr_partitions(path: str) -> List[dict]:
    """The partition entries of the protective MBR of an image."""
    try:
        with open(path, "rb") as handle:
            sector = handle.read(512)
    except OSError:
        return []
    if len(sector) < 512 or sector[510:512] != b"\x55\xaa":
        return []

    partitions = []
    for index in range(4):
        start = 446 + index * 16
        entry = sector[start:start + 16]
        if len(entry) < 16:
            break
        part_type = entry[4]
        if part_type == 0:
            continue
        partitions.append({
            "type": part_type,
            "lba": int.from_bytes(entry[8:12], "little"),
            "size": int.from_bytes(entry[12:16], "little"),
            "bootable": entry[0] == 0x80,
        })
    return partitions


def detect_unix_iso(path: str, paths: set) -> Optional[UnixImage]:
    """Recognise a BSD or Unix image from its ISO9660 tree."""
    hybrid = has_mbr_signature(path)

    if _matches_files(paths, FREEBSD_ISO_FILES, FREEBSD_ISO_PREFIXES):
        return UnixImage("FreeBSD", bootable=hybrid, is_iso=True,
                         efi=hybrid, bios=hybrid)
    if _matches_patterns(paths, OPENBSD_ISO_PATTERNS):
        return UnixImage(
            "OpenBSD", bootable=False, is_iso=True,
            advice=(
                "The OpenBSD installation CD is meant for an optical drive. "
                "Download installXX.img instead - it boots from a USB drive."
            ),
        )
    if _matches_files(paths, NETBSD_ISO_FILES, NETBSD_ISO_PREFIXES):
        return UnixImage(
            "NetBSD", bootable=False, is_iso=True,
            advice=(
                "The NetBSD installation CD is meant for an optical drive. "
                "Download the amd64-install.img image instead - it boots "
                "from a USB drive."
            ),
        )
    if _matches_files(paths, DRAGONFLY_ISO_FILES, DRAGONFLY_ISO_PREFIXES):
        return UnixImage(
            "DragonFly BSD", bootable=False, is_iso=True,
            advice=(
                "The DragonFly BSD CD is meant for an optical drive. "
                "Download the .img image instead - it boots from a USB drive."
            ),
        )
    if _matches_files(paths, ILLUMOS_ISO_FILES, ILLUMOS_ISO_PREFIXES):
        return UnixImage("illumos", bootable=hybrid, is_iso=True,
                         efi=hybrid, bios=hybrid)
    if _matches_files(paths, HAIKU_ISO_FILES):
        return UnixImage("Haiku", bootable=hybrid, is_iso=True,
                         efi=hybrid, bios=hybrid)
    if _matches_files(paths, HURD_ISO_FILES, HURD_ISO_PREFIXES):
        return UnixImage("GNU/Hurd", bootable=hybrid, is_iso=True,
                         efi=hybrid, bios=hybrid)
    return None


def detect_unix_image(path: str) -> Optional[UnixImage]:
    """Recognise a BSD or Unix image from its partition table.

    The raw images of the BSDs (``installXX.img``, ``memstick.img``,
    ``*-install.img``, ``*.usb-dd``) are disk images with an MBR or a GPT
    of their own, and that is what identifies them.
    """
    name = None
    efi = False
    bios = False

    for part in read_mbr_partitions(path):
        part_type = part["type"]
        if part_type == MBR_EFI_SYSTEM:
            efi = True
            continue
        if part_type == MBR_ISO_HYBRID:
            # A hybrid ISO: the system itself is still inside the ISO.
            continue
        if part_type in MBR_TYPES:
            name = MBR_TYPES[part_type]
            bios = True

    gpt = read_gpt(path)
    if gpt is not None:
        for part in gpt.partitions:
            if part.type_guid == GPT_EFI:
                efi = True
            elif part.type_guid in GPT_TYPES:
                if name is None:
                    name = GPT_TYPES[part.type_guid]
                bios = True

    if name is None:
        return None
    return UnixImage(name, bootable=True, is_iso=False, efi=efi, bios=bios)
