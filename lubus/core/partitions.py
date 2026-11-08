"""Partition scheme logic for Lubus."""
from dataclasses import dataclass
from typing import List, Optional
from enum import Enum

from .images import PartitionScheme, TargetSystem, FileSystem, ImageInfo


class PartitionType(Enum):
    """Partition type for creation."""
    EFI_SYSTEM = "efi"
    MICROSOFT_BASIC_DATA = "msftdata"
    LINUX_FILESYSTEM = "linux"
    BIOS_BOOT = "bios_boot"
    MICROSOFT_RESERVED = "msftres"


@dataclass
class PartitionSpec:
    """Specification for a partition."""
    type: PartitionType
    size: int              # Size in bytes (0 = remaining space)
    fs: FileSystem         # Filesystem to format with
    label: str             # Filesystem label
    name: str              # Partition name (GPT)
    flags: List[str]       # Partition flags (e.g., ["boot", "esp"])
    offset: int = 0        # Start offset (for alignment)


@dataclass
class PartitionLayout:
    """Complete partition layout for a drive."""
    scheme: PartitionScheme
    target: TargetSystem
    partitions: List[PartitionSpec]
    alignment: int = 1024 * 1024  # 1 MiB alignment


# Standard partition layouts

def get_windows_installer_layout(
    scheme: PartitionScheme,
    target: TargetSystem,
    fs: FileSystem,
    label: str,
    drive_size: int,
    esp_size: int = 512 * 1024 * 1024,
) -> PartitionLayout:
    """Get partition layout for Windows installer media.

    Args:
        esp_size: Size of the FAT32 ESP (GPT only). The ESP holds the whole
            ISO tree except install.wim, so this must be measured from the
            actual ISO contents (a Windows 11 ISO needs ~2.5 GiB, not the
            default 512 MiB) or the copy dies with ENOSPC.
    """
    partitions = []

    if scheme == PartitionScheme.GPT:
        # GPT + UEFI: ESP (FAT32) + Windows (NTFS)
        partitions.append(PartitionSpec(
            type=PartitionType.EFI_SYSTEM,
            size=esp_size,
            fs=FileSystem.FAT32,
            label="ESP",
            name="EFI System Partition",
            flags=["boot", "esp"],
        ))
        partitions.append(PartitionSpec(
            type=PartitionType.MICROSOFT_BASIC_DATA,
            size=0,  # Rest of disk
            fs=FileSystem.NTFS,
            label=label,
            name="Windows",
            flags=["msftdata"],
        ))
    else:
        # MBR + BIOS: Single NTFS partition with active flag
        partitions.append(PartitionSpec(
            type=PartitionType.MICROSOFT_BASIC_DATA,
            size=0,  # Rest of disk (starts at 1 MiB)
            fs=FileSystem.NTFS,
            label=label,
            name="Windows",
            flags=["boot"],  # Active flag
        ))

    return PartitionLayout(
        scheme=scheme,
        target=target,
        partitions=partitions,
    )


def get_windows_togo_layout(
    fs: FileSystem,
    label: str,
    drive_size: int,
) -> PartitionLayout:
    """Get partition layout for Windows To Go (always GPT/UEFI)."""
    partitions = [
        PartitionSpec(
            type=PartitionType.EFI_SYSTEM,
            size=512 * 1024 * 1024,  # 512 MiB ESP
            fs=FileSystem.FAT32,
            label="ESP",
            name="EFI System Partition",
            flags=["boot", "esp"],
        ),
        PartitionSpec(
            type=PartitionType.MICROSOFT_BASIC_DATA,
            size=0,  # Rest of disk
            fs=FileSystem.NTFS,
            label=label,
            name="Windows",
            flags=["msftdata"],
        ),
    ]

    return PartitionLayout(
        scheme=PartitionScheme.GPT,
        target=TargetSystem.UEFI,
        partitions=partitions,
    )


def get_linux_ishybrid_layout(
    fs: FileSystem,
    label: str,
    drive_size: int,
) -> PartitionLayout:
    """Get partition layout for Linux isohybrid (dd mode).

    Note: In dd mode, we don't create partitions - we write the image directly.
    This layout is used when extracting files instead.
    """
    partitions = [
        PartitionSpec(
            type=PartitionType.EFI_SYSTEM,
            size=512 * 1024 * 1024,
            fs=FileSystem.FAT32,
            label="ESP",
            name="EFI System Partition",
            flags=["boot", "esp"],
        ),
        PartitionSpec(
            type=PartitionType.LINUX_FILESYSTEM,
            size=0,
            fs=fs,
            label=label,
            name="Linux",
            flags=[],
        ),
    ]

    return PartitionLayout(
        scheme=PartitionScheme.GPT,
        target=TargetSystem.UEFI,
        partitions=partitions,
    )


def get_generic_layout(
    scheme: PartitionScheme,
    target: TargetSystem,
    fs: FileSystem,
    label: str,
    drive_size: int,
) -> PartitionLayout:
    """Get a generic partition layout."""
    partitions = []

    if scheme == PartitionScheme.GPT:
        if target == TargetSystem.UEFI:
            partitions.append(PartitionSpec(
                type=PartitionType.EFI_SYSTEM,
                size=512 * 1024 * 1024,
                fs=FileSystem.FAT32,
                label="ESP",
                name="EFI System Partition",
                flags=["boot", "esp"],
            ))
        partitions.append(PartitionSpec(
            type=PartitionType.LINUX_FILESYSTEM,
            size=0,
            fs=fs,
            label=label,
            name="Data",
            flags=[],
        ))
    else:
        # MBR
        partitions.append(PartitionSpec(
            type=PartitionType.LINUX_FILESYSTEM,
            size=0,
            fs=fs,
            label=label,
            name="Data",
            flags=["boot"] if target == TargetSystem.BIOS else [],
        ))

    return PartitionLayout(
        scheme=scheme,
        target=target,
        partitions=partitions,
    )


def calculate_partition_offsets(layout: PartitionLayout, drive_size: int) -> List[PartitionSpec]:
    """Calculate actual offsets for each partition based on alignment."""
    alignment = layout.alignment
    current_offset = alignment  # Start at 1 MiB

    result = []
    for i, part in enumerate(layout.partitions):
        # Align offset
        if current_offset % alignment != 0:
            current_offset = ((current_offset // alignment) + 1) * alignment

        # Calculate size
        if part.size == 0:
            # Last partition gets remaining space
            if i == len(layout.partitions) - 1:
                size = drive_size - current_offset - alignment  # Leave 1 MiB at end
            else:
                size = 0
        else:
            size = part.size

        # Ensure minimum size
        if size < alignment:
            size = alignment

        new_part = PartitionSpec(
            type=part.type,
            size=size,
            fs=part.fs,
            label=part.label,
            name=part.name,
            flags=part.flags,
            offset=current_offset,
        )
        result.append(new_part)
        current_offset += size

    return result


def get_partition_type_guid(part_type: PartitionType, scheme: PartitionScheme) -> str:
    """Get the GPT partition type GUID or MBR type code."""
    if scheme == PartitionScheme.GPT:
        guids = {
            PartitionType.EFI_SYSTEM: "c12a7328-f81f-11d2-ba4b-00a0c93ec93b",
            PartitionType.MICROSOFT_BASIC_DATA: "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7",
            PartitionType.LINUX_FILESYSTEM: "0fc63daf-8483-4772-8e79-3d69d8477de4",
            PartitionType.BIOS_BOOT: "21686148-6449-6e6f-744e-656564454649",
            PartitionType.MICROSOFT_RESERVED: "e3c9e316-0b5c-4db8-817d-f92df00215ae",
        }
        return guids.get(part_type, "0fc63daf-8483-4772-8e79-3d69d8477de4")
    else:
        # MBR codes
        codes = {
            PartitionType.EFI_SYSTEM: "0xef",      # EFI System
            PartitionType.MICROSOFT_BASIC_DATA: "0x07",  # NTFS/exFAT
            PartitionType.LINUX_FILESYSTEM: "0x83",      # Linux
            PartitionType.BIOS_BOOT: "0xee",      # GPT protective (not used in MBR)
            PartitionType.MICROSOFT_RESERVED: "0x00",  # MSR is GPT-only
        }
        return codes.get(part_type, "0x83")


def validate_layout(layout: PartitionLayout, drive_size: int, image_size: int) -> tuple[bool, str]:
    """Validate that a layout fits on the drive.

    Returns (ok, message).
    """
    # Calculate minimum required space
    min_space = 0
    for part in layout.partitions:
        if part.size > 0:
            min_space += part.size
        else:
            # For variable-size partitions, estimate minimum
            if part.fs == FileSystem.FAT32:
                min_space += 32 * 1024 * 1024  # 32 MiB minimum for FAT32
            else:
                min_space += 100 * 1024 * 1024  # 100 MiB minimum

    # Add alignment overhead
    min_space += len(layout.partitions) * layout.alignment

    if min_space > drive_size:
        return False, f"Drive too small: need {format_size(min_space)}, have {format_size(drive_size)}"

    # For dd mode, image must fit
    if image_size > drive_size:
        return False, f"Image too large: {format_size(image_size)} > {format_size(drive_size)}"

    return True, "OK"


def _trim(value: float) -> str:
    """Format with one decimal, dropping a trailing '.0' (1.0 -> '1')."""
    return f"{value:.1f}".rstrip("0").rstrip(".")


def format_size(size: int) -> str:
    """Format bytes as human-readable string."""
    if size < 1024:
        return f"{size} B"
    if size >= 1024**4:
        return f"{_trim(size / 1024**4)} TiB"
    if size >= 1024**3:
        return f"{_trim(size / 1024**3)} GiB"
    if size >= 1024**2:
        return f"{size / 1024**2:.0f} MiB"
    return f"{size / 1024:.0f} KiB"