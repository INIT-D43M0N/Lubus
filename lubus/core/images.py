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
from .bsd import UnixImage, detect_unix_image, detect_unix_iso
from .chromeos import is_chromeos_image
from .macos import is_dmg

log = get_logger(__name__)


class ImageType(Enum):
    """Type of bootable image."""
    WINDOWS_INSTALLER = "windows_installer"    # Windows setup media
    WINDOWS_TO_GO = "windows_to_go"            # Windows To Go (not an image type per se, but a mode)
    LINUX_ISHYBRID = "linux_ishybrid"          # Linux ISO with isohybrid (dd mode)
    LINUX_INSTALLER = "linux_installer"        # Linux installer (file copy mode)
    CHROMEOS = "chromeos"                      # ChromeOS recovery image (raw dd mode)
    MACOS = "macos"                            # macOS installer disk image (.dmg)
    BSD_UNIX = "bsd_unix"                      # FreeBSD, OpenBSD, NetBSD, illumos, ...
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
    os_name: str = ""       # concrete system name ("FreeBSD", "OpenBSD", ...)
    boot_warning: str = ""  # shown when the image needs another file


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

# Markers used for the raw byte-scan fallback (non-ISO9660 images).
# Keep these specific: bootloader binaries (GRUB, syslinux) embed generic
# strings such as "windows", "bootmgr" and "bcd", so bare words would
# misclassify Linux ISOs as Windows media.
WINDOWS_MARKERS = (
    "install.wim", "install.esd", "boot.wim", "bootmgr.efi",
    "efi/microsoft", "sources/setup.exe",
)
LINUX_MARKERS = (
    "isolinux", "casper", "vmlinuz", "initrd.img", "initrd",
    "grub.cfg", "grub2", "syslinux", "boot/grub", "boot/syslinux",
    "live/", "boot/vmlinuz", "boot/initrd", "squashfs.img",
)

# Exact ISO9660 paths that identify the media type.
WINDOWS_ISO_FILES = frozenset({
    "sources/install.wim",
    "sources/install.esd",
    "sources/boot.wim",
    "bootmgr",
    "bootmgr.efi",
})
WINDOWS_ISO_PREFIXES = ("efi/microsoft/boot/",)

LINUX_ISO_FILES = frozenset({
    "isolinux.bin",
    "isolinux/isolinux.bin",
    "boot/isolinux/isolinux.bin",
    "casper/filesystem.squashfs",
    "casper/vmlinuz",
    "casper/initrd",
    "live/filesystem.squashfs",
    "live/vmlinuz",
    "live/initrd.img",
    "liveos/squashfs.img",
    "images/pxeboot/vmlinuz",
    "images/pxeboot/initrd.img",
    "boot/vmlinuz",
    "boot/initrd.img",
    "boot/grub2/grub.cfg",
    "boot/grub/grub.cfg",
    "efi/boot/grub.cfg",
})
LINUX_ISO_PREFIXES = (
    "boot/grub/i386-pc/",
    "boot/grub2/i386_pc/",
    "images/pxeboot/",
)

EFI_BOOT_PATHS = frozenset({
    "efi/boot/bootx64.efi",
    "efi/boot/bootia32.efi",
    "efi/boot/bootaa64.efi",
    "efi/boot/bootarm.efi",
    "efi/boot/grubx64.efi",
    "efi/boot/grubia32.efi",
    "efi/boot/grubaa64.efi",
    "efi/boot/grub.cfg",
    # 8.3-truncated names used by the primary tree (e.g. Fedora)
    "efi/boot/bootx640.efi",
    "efi/boot/bootia320.efi",
})
BIOS_BOOT_PATHS = frozenset({
    "isolinux.bin",
    "isolinux/isolinux.bin",
    "boot/isolinux/isolinux.bin",
    "boot/grub/grub.cfg",
    "boot/grub2/grub.cfg",
    "syslinux/syslinux.cfg",
    "syslinux.cfg",
    "bootmgr",
    "boot/bcd",
})
BIOS_BOOT_PREFIXES = ("boot/grub/i386-pc/", "boot/grub2/i386_pc/")

ISO_SCAN_LIMIT = 64 * 1024 * 1024  # 64 MiB
ISO_CHUNK = 4 * 1024 * 1024  # 4 MiB

ISO_BLOCK_SIZE = 2048
ISO_MAX_DIR_BYTES = 16 * 1024 * 1024
ISO_MAX_ENTRIES = 200_000
ISO_MAX_DEPTH = 8
ISO_PVD_SECTOR = 16
ISO_MAX_VOLUME_DESCRIPTORS = 64

# ISO9660 structure constants
ISO_VD_PRIMARY = 1
ISO_VD_SUPPLEMENTARY = 2
ISO_VD_TERMINATOR = 255
ISO_VD_MIN = 7
ISO_RECORD_MIN = 33
ISO_ROOT_RECORD_MIN = 14
ISO_NAME_LEN_OFFSET = 32
ISO_NAME_OFFSET = 33
ISO_SINGLE_NAME_LEN = 1


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


def _iso_root_record(rec: bytes) -> Optional[Tuple[int, int]]:
    """Parse a root directory record -> (extent, size).

    The root record carries the same single 0x00 "name" as the "." entry, so
    it has to be decoded separately from regular directory records.
    """
    if len(rec) < ISO_ROOT_RECORD_MIN:
        return None
    extent = int.from_bytes(rec[2:6], "little")
    size = int.from_bytes(rec[10:14], "little")
    return extent, size


def _iso_dir_record(rec: bytes, joliet: bool) -> Optional[Tuple[str, int, int, bool]]:
    """Parse one ISO9660 directory record.

    Returns (name, extent, size, is_directory) or None for "." / "..".
    """
    if len(rec) < ISO_RECORD_MIN:
        return None
    extent = int.from_bytes(rec[2:6], "little")
    size = int.from_bytes(rec[10:14], "little")
    flags = rec[25]
    name_len = rec[ISO_NAME_LEN_OFFSET]
    raw = rec[ISO_NAME_OFFSET:ISO_NAME_OFFSET + name_len]
    if name_len == ISO_SINGLE_NAME_LEN and raw in (b"\x00", b"\x01"):
        return None
    if joliet:
        name = raw.decode("utf-16-be", "replace")
    else:
        name = raw.decode("latin-1", "replace")
    name = name.split(";", 1)[0].rstrip(".").strip()
    if not name:
        return None
    return name.lower(), extent, size, bool(flags & 0x02)


def _iso_walk(
    f,
    extent: int,
    size: int,
    prefix: str,
    out: set,
    seen: set,
    depth: int,
    joliet: bool,
) -> None:
    """Recursively collect lowercase paths from an ISO9660 directory tree."""
    if depth > ISO_MAX_DEPTH or len(out) > ISO_MAX_ENTRIES:
        return
    key = (extent, size)
    if key in seen or not size or size > ISO_MAX_DIR_BYTES:
        return
    seen.add(key)

    try:
        f.seek(extent * ISO_BLOCK_SIZE)
        data = f.read(size)
    except OSError:
        return

    pos = 0
    while pos < len(data):
        length = data[pos]
        if length == 0:
            # Records never straddle a sector boundary; skip the padding.
            pos = (pos // ISO_BLOCK_SIZE + 1) * ISO_BLOCK_SIZE
            continue

        record = data[pos:pos + length]
        pos += length
        parsed = _iso_dir_record(record, joliet)
        if parsed is None:
            continue

        name, child_extent, child_size, is_dir = parsed
        full = f"{prefix}/{name}" if prefix else name
        out.add(full)
        if is_dir:
            _iso_walk(f, child_extent, child_size, full, out, seen, depth + 1, joliet)


def _iso_paths(path: str) -> Optional[set]:
    """Return lowercase paths from an ISO9660 image, or None if not ISO9660.

    Prefers the Joliet tree (which preserves long names such as
    ``casper/filesystem.squashfs``) and falls back to the primary tree.
    """
    try:
        file_size = os.path.getsize(path)
        f = open(path, "rb")
    except OSError:
        return None

    try:
        primary_root = None
        joliet_root = None
        sector = ISO_PVD_SECTOR
        while sector < ISO_PVD_SECTOR + ISO_MAX_VOLUME_DESCRIPTORS:
            f.seek(sector * ISO_BLOCK_SIZE)
            vd = f.read(ISO_BLOCK_SIZE)
            if len(vd) < ISO_VD_MIN or vd[1:6] != b"CD001":
                break
            vd_type = vd[0]
            if vd_type == ISO_VD_PRIMARY and primary_root is None:
                primary_root = vd[156:190]
            elif vd_type == ISO_VD_SUPPLEMENTARY and vd[88:91] in (
                    b"%/@", b"%/C", b"%/E"):
                joliet_root = vd[156:190]
            if vd_type == ISO_VD_TERMINATOR:
                break
            sector += 1

        joliet = joliet_root is not None
        root = joliet_root if joliet else primary_root
        if not root:
            return None
        root_info = _iso_root_record(root)
        if root_info is None:
            return None

        extent, size = root_info
        if not size or extent * ISO_BLOCK_SIZE >= file_size:
            return None

        paths = set()
        _iso_walk(f, extent, size, "", paths, set(), 0, joliet)
        return paths or None
    except OSError:
        return None
    finally:
        f.close()


def _matches_paths(paths: set, files, prefixes) -> bool:
    """True when any exact file or path prefix is present."""
    return any(p in paths for p in files) or any(
        p.startswith(prefix) for p in paths for prefix in prefixes
    )


def scan_iso_for_markers(path: str) -> Tuple[bool, bool]:
    """Scan raw image bytes for Windows/Linux markers.

    This is only a fallback for files that cannot be parsed as ISO9660.
    Returns (has_windows_markers, has_linux_markers).
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


def _detect_image_type(path: str, paths: Optional[set]) -> ImageType:
    # Apple's disk images come first: they are not ISO9660 and their
    # compressed payload may contain anything.
    if is_dmg(path):
        return ImageType.MACOS

    unix_image: Optional[UnixImage] = None
    has_windows = False
    has_linux = False

    if paths is not None:
        has_windows = _matches_paths(paths, WINDOWS_ISO_FILES, WINDOWS_ISO_PREFIXES)
        has_linux = _matches_paths(paths, LINUX_ISO_FILES, LINUX_ISO_PREFIXES)
        if not has_windows and not has_linux:
            # The tree is not conclusive. The BSD and Unix media are
            # recognised before the raw scan, because their bootloaders
            # contain strings that look like Linux ones.
            unix_image = detect_unix_iso(path, paths)
            if unix_image is None and not is_chromeos_image(path):
                has_windows, has_linux = scan_iso_for_markers(path)
    else:
        # Not an ISO9660 file: a raw disk image. Its partition table says
        # what it is, the byte scan is only the last resort.
        if is_chromeos_image(path):
            return ImageType.CHROMEOS
        unix_image = detect_unix_image(path)
        if unix_image is None:
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

    if unix_image is not None:
        return ImageType.BSD_UNIX

    # Not an ISO. A raw disk image with the ChromeOS partition layout is a
    # ChromeOS recovery image, which is written like an isohybrid one.
    if is_chromeos_image(path):
        return ImageType.CHROMEOS

    return ImageType.UNKNOWN


def detect_image_type(path: str) -> ImageType:
    """Detect the type of bootable image.

    ISO9660 images are inspected through their directory tree so that strings
    embedded in bootloader binaries cannot be mistaken for media markers.
    Raw byte scanning is kept as a fallback for other image formats.
    """
    return _detect_image_type(path, _iso_paths(path))


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
    except Exception as exc:  # noqa: BLE001 - editions are optional metadata
        # A mount/DBus failure must never abort image analysis; the caller
        # can still flash the image without the edition list.
        log.debug("Could not list Windows editions from %s: %s", iso_path, exc)
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
    paths = _iso_paths(path)
    image_type = _detect_image_type(path, paths)
    editions = []

    # The BSD and Unix images carry the name of the system they belong to.
    unix_image = None
    if image_type == ImageType.BSD_UNIX:
        unix_image = (detect_unix_iso(path, paths) if paths is not None
                      else detect_unix_image(path))

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
    elif image_type == ImageType.CHROMEOS:
        # ChromeOS recovery images are written as a whole, like isohybrid
        # media: the drive keeps the partition layout of the image.
        suggested_scheme = PartitionScheme.GPT
        suggested_target = TargetSystem.UEFI
        suggested_fs = FileSystem.FAT32
    elif image_type == ImageType.MACOS:
        # The macOS layout is fixed: an EFI partition plus an Apple_HFS one.
        suggested_scheme = PartitionScheme.GPT
        suggested_target = TargetSystem.UEFI
        suggested_fs = FileSystem.FAT32
    elif image_type == ImageType.BSD_UNIX:
        # The BSD images carry their own partition table, so they are
        # written as a whole, just like an isohybrid ISO.
        suggested_scheme = PartitionScheme.GPT
        suggested_target = (
            TargetSystem.UEFI
            if unix_image is None or unix_image.efi
            else TargetSystem.BIOS
        )
        suggested_fs = FileSystem.FAT32
    else:
        suggested_scheme = PartitionScheme.GPT
        suggested_target = TargetSystem.UEFI
        suggested_fs = FileSystem.FAT32

    # Check EFI/BIOS bootability. For ISO9660 images the directory tree is
    # enough; mounting is only needed for other formats (e.g. .img/.raw).
    is_efi = False
    is_bios = False
    if image_type == ImageType.CHROMEOS:
        # A raw disk image: it is copied as a whole and never mounted.
        is_efi = False
        is_bios = False
    elif image_type == ImageType.MACOS:
        # The macOS layout carries an EFI partition with Apple's bootloader.
        is_efi = True
        is_bios = False
    elif image_type == ImageType.BSD_UNIX and unix_image is not None:
        # The partition table of the image says how it boots.
        is_efi = unix_image.efi
        is_bios = unix_image.bios
    elif paths is not None:
        is_efi = _matches_paths(paths, EFI_BOOT_PATHS, ())
        is_bios = _matches_paths(paths, BIOS_BOOT_PATHS, BIOS_BOOT_PREFIXES)
    else:
        try:
            from ..udisks import mount_iso_readonly
            with mount_iso_readonly(path) as root:
                is_efi = os.path.exists(os.path.join(root, "EFI", "BOOT", "BOOTX64.EFI")) or \
                         os.path.exists(os.path.join(root, "EFI", "BOOT", "BOOTX64.efi"))
                is_bios = os.path.exists(os.path.join(root, "isolinux.bin")) or \
                          os.path.exists(os.path.join(root, "boot", "grub", "i386-pc"))
        except Exception:
            pass

    if image_type == ImageType.WINDOWS_INSTALLER:
        distro_name = None
    elif image_type == ImageType.CHROMEOS:
        distro_name = "ChromeOS"
    elif image_type == ImageType.MACOS:
        distro_name = "macOS"
    elif image_type == ImageType.BSD_UNIX and unix_image is not None:
        distro_name = unix_image.name
    else:
        distro_name = identify_distro(label, filename)

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
        os_name=distro_name or "",
        boot_warning=(unix_image.advice if unix_image is not None else ""),
    )


def get_supported_filesystems() -> List[FileSystem]:
    """Get list of filesystems supported on this system."""
    from ..udisks import can_format_fstype
    supported = []
    for fs in FileSystem:
        if can_format_fstype(fs.value):
            supported.append(fs)
    return supported