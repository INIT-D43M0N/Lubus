"""ISO/Image analysis and detection for Lubus."""
import os
import re
import subprocess
import shutil
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Tuple
from pathlib import Path

from ..utils.logging import get_logger

log = get_logger(__name__)


class ImageType(Enum):
    """Type of bootable image."""
    WINDOWS_INSTALLER = "windows_installer"    # Windows setup media
    WINDOWS_TO_GO = "windows_to_go"            # Windows To Go (not an image type per se, but a mode)
    LINUX_ISHYBRID = "linux_ishybrid"          # Linux ISO with isohybrid (dd mode)
    LINUX_INSTALLER = "linux_installer"        # Linux installer (file copy mode)
    UNKNOWN = "unknown"


class PartitionScheme(Enum):
    """Partition table scheme."""
    GPT = "gpt"
    MBR = "mbr"  # also called "dos" or "msdos"


class TargetSystem(Enum):
    """Target boot system."""
    UEFI = "uefi"
    BIOS = "bios"  # CSM/Legacy


class FileSystem(Enum):
    """Supported filesystems."""
    FAT32 = "vfat"
    NTFS = "ntfs"
    EXFAT = "exfat"
    EXT4 = "ext4"
    BTRFS = "btrfs"

    @property
    def mkfs_cmd(self) -> str:
        return f"mkfs.{self.value}"

    @property
    def max_file_size(self) -> int:
        limits = {
            FileSystem.FAT32: 4 * 1024**3 - 1,      # 4 GiB - 1
            FileSystem.NTFS: 16 * 1024**4 - 1,      # 16 TiB - 1
            FileSystem.EXFAT: 16 * 1024**4 - 1,     # 16 EiB - 1
            FileSystem.EXT4: 16 * 1024**3 - 1,      # 16 TiB - 1 (with 4k blocks)
            FileSystem.BTRFS: 16 * 1024**4 - 1,     # 16 EiB - 1
        }
        return limits.get(self, 0)


@dataclass
class ImageInfo:
    """Information about a bootable image."""
    path: str
    size: int
    image_type: ImageType
    label: str
    suggested_scheme: PartitionScheme
    suggested_target: TargetSystem
    suggested_fs: FileSystem
    windows_editions: List[Tuple[int, str, int]]  # (index, name, size_bytes)
    is_efi_bootable: bool
    is_bios_bootable: bool


# ISO9660 Primary Volume Descriptor offset
PVD_OFFSET = 16 * 2048

# Known Linux distribution patterns (first match wins)
KNOWN_DISTROS = [
    (r"kubuntu", "Kubuntu"),
    (r"xubuntu", "Xubuntu"),
    (r"lubuntu", "Lubuntu"),
    (r"ubuntu", "Ubuntu"),
    (r"cachyos", "CachyOS"),
    (r"endeavouros", "EndeavourOS"),
    (r"manjaro", "Manjaro"),
    (r"garuda", "Garuda Linux"),
    (r"(^|[^a-z])arch", "Arch Linux"),
    (r"fedora", "Fedora"),
    (r"debian", "Debian"),
    (r"mint", "Linux Mint"),
    (r"pop[-_ ]?os", "Pop!_OS"),
    (r"opensuse|suse", "openSUSE"),
    (r"alpine", "Alpine Linux"),
    (r"nixos", "NixOS"),
    (r"kali", "Kali Linux"),
    (r"elementary", "elementary OS"),
    (r"zorin", "Zorin OS"),
    (r"gentoo", "Gentoo"),
    (r"void", "Void Linux"),
    (r"rocky", "Rocky Linux"),
    (r"alma", "AlmaLinux"),
    (r"centos", "CentOS"),
    (r"tails", "Tails"),
    (r"proxmox", "Proxmox"),
    (r"systemrescue", "SystemRescue"),
    (r"clonezilla", "Clonezilla"),
    (r"gparted", "GParted Live"),
]

# Markers in ISO for OS detection
WINDOWS_MARKERS = (
    "install.wim", "install.esd", "boot.wim", "bootmgr.efi",
    "windows", "bootmgr", "bcd", "efi/microsoft"
)
LINUX_MARKERS = (
    "isolinux", "casper", "vmlinuz", "initrd.img", "initrd",
    "grub.cfg", "grub2", "syslinux", "boot/grub", "boot/syslinux",
    "live/", "boot/vmlinuz", "boot/initrd"
)

ISO_SCAN_LIMIT = 64 * 1024 * 1024  # 64 MiB
ISO_CHUNK = 4 * 1024 * 1024  # 4 MiB


def read_volume_label(path: str) -> str:
    """Read ISO9660 volume label from image."""
    try:
        with open(path, "rb") as f:
            f.seek(PVD_OFFSET)
            pvd = f.read(72)
        if pvd[1:6] != b"CD001":
            return ""
        # ECMA-119 Primary Volume Descriptor: bytes 39-70 (0-based) hold the
        # volume identifier (byte 0 = type, 1-5 = "CD001", 6 = version,
        # 7-38 = system identifier).
        return pvd[39:71].decode("ascii", "replace").strip()
    except OSError:
        return ""


def identify_distro(label: str, filename: str) -> str:
    """Identify Linux distribution from label and filename."""
    text = f"{label} {filename}".lower()
    for pattern, name in KNOWN_DISTROS:
        if re.search(pattern, text):
            version = re.search(r"\d+(?:\.\d+)+", text)
            return f"{name} {version.group()}" if version else name
    return label or os.path.splitext(filename)[0]


def windows_name_from_editions(editions: List[Tuple[int, str, int]]) -> str:
    """Extract Windows version from edition names."""
    for _, name, _ in editions:
        match = re.search(r"Windows \d+", name)
        if match:
            return match.group()
    return "Windows"


def scan_iso_for_markers(path: str) -> Tuple[bool, bool]:
    """Scan ISO for Windows/Linux markers.

    Returns (has_windows_markers, has_linux_markers)
    """
    has_windows = False
    has_linux = False

    try:
        with open(path, "rb") as f:
            carry = b""
            read = 0
            while read < ISO_SCAN_LIMIT:
                buf = f.read(ISO_CHUNK)
                if not buf:
                    break
                read += len(buf)
                window = carry + buf

                # Check in multiple encodings
                texts = [
                    window.decode("latin-1").lower(),
                ]
                # UTF-16BE at both alignments
                for start in (0, 1):
                    body = window[start:]
                    texts.append(body[:len(body) & ~1].decode("utf-16-be", "ignore").lower())

                for text in texts:
                    if any(m in text for m in WINDOWS_MARKERS):
                        has_windows = True
                    if any(m in text for m in LINUX_MARKERS):
                        has_linux = True

                # A name can straddle two chunks
                carry = window[-64:]

                if has_windows:
                    break  # Windows wins
    except OSError:
        pass

    return has_windows, has_linux


def detect_image_type(path: str) -> ImageType:
    """Detect the type of bootable image."""
    has_windows, has_linux = scan_iso_for_markers(path)

    if has_windows:
        return ImageType.WINDOWS_INSTALLER
    if has_linux:
        # Check if it's a proper isohybrid (has MBR boot sector)
        try:
            with open(path, "rb") as f:
                mbr = f.read(512)
                if mbr[510:512] == b"\x55\xaa":
                    return ImageType.LINUX_ISHYBRID
        except OSError:
            pass
        return ImageType.LINUX_INSTALLER

    return ImageType.UNKNOWN


def list_windows_editions(iso_path: str) -> List[Tuple[int, str, int]]:
    """List Windows editions (images) in a Windows ISO.

    Returns list of (index, name, size_bytes).
    """
    try:
        # Mount ISO to find install.wim/esd
        from ..udisks import mount_iso_readonly
        with mount_iso_readonly(iso_path) as root:
            wim_path = None
            for name in ("install.wim", "install.esd"):
                candidate = os.path.join(root, "sources", name)
                if os.path.exists(candidate):
                    wim_path = candidate
                    break

            if not wim_path:
                return []

            # Use wimlib-imagex to get info
            wimlib = shutil.which("wimlib-imagex") or "wimlib-imagex"
            result = subprocess.run(
                [wimlib, "info", wim_path],
                capture_output=True, text=True, timeout=120, check=False
            )
            return parse_wim_info(result.stdout)
    except (OSError, subprocess.SubprocessError, FileNotFoundError):
        return []


def parse_wim_info(info: str) -> List[Tuple[int, str, int]]:
    """Parse wimlib-imagex info output."""
    editions = []
    index = None

    for line in info.splitlines():
        if line.startswith("Index:"):
            index = line.split(":", 1)[1].strip()
        elif line.startswith("Name:") and index is not None:
            editions.append([int(index), line.split(":", 1)[1].strip(), 0])
            index = None
        elif line.startswith("Total Bytes:") and editions:
            editions[-1][2] += int(line.split(":", 1)[1].strip())
        elif line.startswith("Hard Link Bytes:") and editions:
            editions[-1][2] -= int(line.split(":", 1)[1].strip())

    return [tuple(e) for e in editions]


def analyze_image(path: str) -> ImageInfo:
    """Analyze a bootable image and return detailed info."""
    size = os.path.getsize(path)
    label = read_volume_label(path)
    filename = os.path.basename(path)
    image_type = detect_image_type(path)
    editions = []

    # Default suggestions
    if image_type == ImageType.WINDOWS_INSTALLER:
        suggested_scheme = PartitionScheme.GPT
        suggested_target = TargetSystem.UEFI
        suggested_fs = FileSystem.FAT32
        editions = list_windows_editions(path)
    elif image_type in (ImageType.LINUX_ISHYBRID, ImageType.LINUX_INSTALLER):
        suggested_scheme = PartitionScheme.GPT
        suggested_target = TargetSystem.UEFI
        suggested_fs = FileSystem.FAT32
    else:
        suggested_scheme = PartitionScheme.GPT
        suggested_target = TargetSystem.UEFI
        suggested_fs = FileSystem.FAT32

    # Check EFI/BIOS bootability
    is_efi = False
    is_bios = False
    try:
        from ..udisks import mount_iso_readonly
        with mount_iso_readonly(path) as root:
            is_efi = os.path.exists(os.path.join(root, "EFI", "BOOT", "BOOTX64.EFI")) or \
                     os.path.exists(os.path.join(root, "EFI", "BOOT", "BOOTX64.efi"))
            is_bios = os.path.exists(os.path.join(root, "isolinux.bin")) or \
                      os.path.exists(os.path.join(root, "boot", "grub", "i386-pc"))
    except Exception:
        pass

    distro_name = identify_distro(label, filename) if image_type != ImageType.WINDOWS_INSTALLER else None

    return ImageInfo(
        path=path,
        size=size,
        image_type=image_type,
        label=label,
        suggested_scheme=suggested_scheme,
        suggested_target=suggested_target,
        suggested_fs=suggested_fs,
        windows_editions=editions,
        is_efi_bootable=is_efi,
        is_bios_bootable=is_bios,
    )


def get_supported_filesystems() -> List[FileSystem]:
    """Get list of filesystems supported on this system."""
    from ..udisks import can_format_fstype
    supported = []
    for fs in FileSystem:
        if can_format_fstype(fs.value):
            supported.append(fs)
    return supported