"""ChromeOS recovery image support.

Google ships ChromeOS as a recovery image: a zip holding ``chromeos_image.bin``,
a raw disk image with a fixed partition layout. Google's own instructions are
to copy that image onto the drive byte for byte, which is exactly what
Lubus' dd mode does - the only thing missing was recognising the image.

The partition table is the reliable marker:

    EFI-SYSTEM   the kernel and the bootloader
    ROOT-A       the root filesystem
    KERN-A/B/C   the kernel slots
    OEM          vendor space
    STATE        the user data partition

The names differ between the generations of the images, so the *type* of the
partitions is what decides: ChromeOS uses its own GUIDs for the kernel, the
root filesystem, the firmware and the reserved areas. Reading the table is
much safer than looking for strings in the image bytes, the same way the
ISO9660 tree is used for ISO media.
"""
from typing import Optional

from .gpt import GptInfo, read_gpt

# Partitions every ChromeOS recovery image carries.
REQUIRED_PARTITIONS = ("EFI-SYSTEM",)
OPTIONAL_PARTITIONS = ("CHROMEOS", "ROOT-A", "ROOT-B", "ROOT-C",
                       "KERN-A", "KERN-B", "KERN-C", "OEM", "STATE",
                       "RESERVED", "RWFW")

# The partition type GUIDs ChromeOS uses.
ESP_GUID = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
CHROMEOS_KERNEL_GUID = "fe3a2a5d-4f32-41a7-b725-accc3285a309"
CHROMEOS_ROOTFS_GUID = "3cb8e202-3b7e-47dd-8a3c-7ff2a13cfcec"
CHROMEOS_FIRMWARE_GUID = "cab6e88e-abf3-4102-a07a-d4bb9be3c1d3"
CHROMEOS_RESERVED_GUID = "2e0a753d-9e48-43b0-8337-b15192cb1b5e"

CHROMEOS_TYPE_GUIDS = (
    CHROMEOS_KERNEL_GUID,
    CHROMEOS_ROOTFS_GUID,
    CHROMEOS_FIRMWARE_GUID,
    CHROMEOS_RESERVED_GUID,
)

# The names of the ChromeOS partitions, in the spellings the images use.
CHROMEOS_PARTITION_NAMES = (
    "CHROMEOS", "ROOT-A", "ROOT-B", "ROOT-C",
    "KERN-A", "KERN-B", "KERN-C", "RWFW", "RESERVED",
)


def chromeos_gpt(path: str) -> Optional[GptInfo]:
    """The partition table of the image, when it has one."""
    return read_gpt(path)


def is_chromeos_image(path: str) -> bool:
    """Whether the file is a ChromeOS recovery image.

    Every ChromeOS image carries an EFI partition and at least one kernel
    partition, and the kernel partition has its own type GUID. That is
    what decides: a plain Linux disk image with a GPT does not have it,
    so nothing else is needed to tell the two apart.
    """
    gpt = read_gpt(path)
    if gpt is None:
        return False

    has_efi = False
    has_kernel = False
    for part in gpt.partitions:
        name = part.name.upper()
        if part.type_guid == ESP_GUID or name == "EFI-SYSTEM":
            has_efi = True
        if (part.type_guid == CHROMEOS_KERNEL_GUID
                or name in ("KERN-A", "KERN-B", "KERN-C", "CHROMEOS")):
            has_kernel = True

    return has_efi and has_kernel


def chromeos_partitions(path: str) -> list:
    """The ChromeOS partitions of the image, as (name, size) pairs."""
    gpt = read_gpt(path)
    if gpt is None:
        return []
    return [(p.name, p.size_bytes) for p in gpt.partitions if p.name]
