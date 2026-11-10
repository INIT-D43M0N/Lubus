"""ChromeOS recovery image support.

Google ships ChromeOS as a recovery image: a zip holding ``chromeos_image.bin``,
a raw disk image with a fixed partition layout. Google's own instructions are
to copy that image onto the drive byte for byte, which is exactly what
Lubus' dd mode does - the only thing missing was recognising the image.

The partition table is the reliable marker:

    EFI-SYSTEM   the kernel and the bootloader
    CHROMEOS     the root filesystem
    OEM          vendor space
    STATE        the user data partition

Reading the table is much safer than looking for strings in the image
bytes, the same way the ISO9660 tree is used for ISO media.
"""
from typing import Optional

from .gpt import GptInfo, read_gpt

# Partitions every ChromeOS recovery image carries.
REQUIRED_PARTITIONS = ("EFI-SYSTEM", "CHROMEOS")

# Partitions that show up depending on the image and the board.
OPTIONAL_PARTITIONS = ("OEM", "STATE", "ROOT-A", "RESERVED", "KERN-A", "KERN-B")


def chromeos_gpt(path: str) -> Optional[GptInfo]:
    """The partition table of the image, when it has one."""
    return read_gpt(path)


def is_chromeos_image(path: str) -> bool:
    """Whether the file is a ChromeOS recovery image.

    The GPT has to carry the ChromeOS partitions. A plain Linux disk image
    with a GPT does not, so nothing else is needed to tell them apart.
    """
    gpt = read_gpt(path)
    if gpt is None:
        return False
    return gpt.has_layout(REQUIRED_PARTITIONS, OPTIONAL_PARTITIONS)


def chromeos_partitions(path: str) -> list:
    """The ChromeOS partitions of the image, as (name, size) pairs."""
    gpt = read_gpt(path)
    if gpt is None:
        return []
    return [(p.name, p.size_bytes) for p in gpt.partitions if p.name]
