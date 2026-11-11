"""Brunch support: ChromeOS on a generic PC.

Brunch (https://github.com/sebanc/brunch) turns a ChromeOS recovery image
into something that boots on an ordinary computer. It does that by laying
out a ChromeOS specific partition table and putting the Brunch kernel and
bootloader in two of the partitions.

Upstream this is done by ``chromeos-install.sh``, which needs root for the
whole run. Yaku does not need that: the table is built in user space and
written to the drive with the same privileged device access the other write
modes already use, so the only privileged step is the write itself.

The layout below is a faithful copy of the ``write_base_table`` function of
the upstream script (12 partitions, with the same gaps and alignments),
including the ChromeOS kernel attributes and the boot GUID of the EFI
partition in the protective MBR.
"""
import os
import shutil
import tarfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ..utils.i18n import _
from .gpt import GptEntry, GptInfo, GptPartition, read_gpt

# --------------------------------------------------------------- GUID types

TYPE_EFI = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
TYPE_KERNEL = "fe3a2a5d-4f32-41a7-b725-accc3285a309"
TYPE_ROOTFS = "3cb8e202-3b7e-47dd-8a3c-7ff2a13cfcec"
TYPE_DATA = "0fc63daf-8483-4772-8e79-3d69d8477de4"
TYPE_RESERVED = "2e0a753d-9e48-43b0-8337-b15192cb1b5e"
TYPE_FIRMWARE = "cab6e88e-abf3-4102-a07a-d4bb9be3c1d3"

# --------------------------------------------------------------- geometry

SECTOR_SIZE = 512

# The upstream script refuses anything smaller than this (29360128 sectors).
MIN_DRIVE_SECTORS = 29360128
MIN_DRIVE_SIZE = MIN_DRIVE_SECTORS * SECTOR_SIZE

# The first partition starts 16 MiB into the disk.
BASE_OFFSET = 32768

# Gap between the reserved partitions and KERN-A, in bytes.
KERN_GAP = 2062336

# Gap between OEM and EFI-SYSTEM, in bytes.
EFI_GAP = 67108864

# Bytes kept free at the end of the disk for the backup partition table.
TAIL_RESERVED = 24576

# Alignment the upstream script applies to some partitions.
ALIGNMENT = 4096

# The ChromeOS recovery image starts with this magic.
CHROMEOS_MAGIC = b"\x33\xc0\xfa\x8e"


def kernel_attributes(priority: int, tries: int = 15, successful: int = 0) -> int:
    """Build the GPT attribute bits ChromeOS uses for kernel partitions.

    The bits live in the upper half of the 64 bit attribute field: priority
    in bits 48-51, tries in bits 52-55 and the successful flag in bit 56.
    """
    return (
        ((priority & 0xF) << 48)
        | ((tries & 0xF) << 52)
        | ((successful & 0x1) << 56)
    )


# Attributes the upstream script sets on the kernel partitions.
ATTR_KERN_A = kernel_attributes(15, 15, 0)
ATTR_KERN_B = kernel_attributes(0, 15, 0)
ATTR_KERN_C = kernel_attributes(0, 15, 0)


@dataclass
class BrunchPartition:
    """One partition of the Brunch layout."""

    index: int          # 1-based GPT entry number
    type_guid: str
    name: str
    offset: int         # byte offset on the disk
    size: int           # size in bytes
    attributes: int = 0

    @property
    def first_lba(self) -> int:
        return self.offset // SECTOR_SIZE

    @property
    def last_lba(self) -> int:
        return (self.offset + self.size) // SECTOR_SIZE - 1

    def as_gpt_entry(self) -> GptEntry:
        return GptEntry(
            index=self.index,
            type_guid=self.type_guid,
            name=self.name,
            first_lba=self.first_lba,
            last_lba=self.last_lba,
            attributes=self.attributes,
        )


def _align(offset: int) -> int:
    """Round an offset up to the next 4 KiB boundary."""
    remainder = offset % ALIGNMENT
    return offset if remainder == 0 else offset + ALIGNMENT - remainder


def brunch_layout(disk_size: int) -> List[BrunchPartition]:
    """Build the Brunch partition layout for a drive of ``disk_size`` bytes.

    The order and the gaps are exactly the ones of the upstream
    ``chromeos-install.sh``, so the result is byte-compatible with a drive
    installed by Brunch itself.
    """
    total_sectors = disk_size // SECTOR_SIZE
    if total_sectors < MIN_DRIVE_SECTORS:
        raise ValueError(
            "The drive is too small for Brunch "
            f"({total_sectors} < {MIN_DRIVE_SECTORS} sectors)"
        )

    parts: List[BrunchPartition] = []
    curr = BASE_OFFSET

    def add(index: int, type_guid: str, name: str, size: int,
            attributes: int = 0):
        nonlocal curr
        parts.append(BrunchPartition(
            index=index,
            type_guid=type_guid,
            name=name,
            offset=curr,
            size=size,
            attributes=attributes,
        ))
        curr += size

    # 11 - RWFW: the read write firmware
    add(11, TYPE_FIRMWARE, "RWFW", 8 * 1024 * 1024)
    # 6 - KERN-C: a one sector placeholder for the kernel slot
    add(6, TYPE_KERNEL, "KERN-C", SECTOR_SIZE, ATTR_KERN_C)
    curr = _align(curr)
    # 7 - ROOT-C: the Brunch root filesystem (1 GiB)
    add(7, TYPE_ROOTFS, "ROOT-C", 1024 * 1024 * 1024)
    # 9 and 10 - two reserved one sector partitions
    add(9, TYPE_RESERVED, "reserved", SECTOR_SIZE)
    add(10, TYPE_RESERVED, "reserved", SECTOR_SIZE)
    curr += KERN_GAP
    # 2 and 4 - the two kernel slots
    add(2, TYPE_KERNEL, "KERN-A", 64 * 1024 * 1024, ATTR_KERN_A)
    add(4, TYPE_KERNEL, "KERN-B", 64 * 1024 * 1024, ATTR_KERN_B)
    curr = _align(curr)
    # 8 - OEM
    add(8, TYPE_DATA, "OEM", 16 * 1024 * 1024)
    curr += EFI_GAP
    # 12 - EFI-SYSTEM: holds the Brunch bootloader
    add(12, TYPE_EFI, "EFI-SYSTEM", 64 * 1024 * 1024)
    # 5 - ROOT-B
    add(5, TYPE_ROOTFS, "ROOT-B", 4 * 1024 * 1024 * 1024)
    curr = _align(curr)
    # 3 - ROOT-A
    add(3, TYPE_ROOTFS, "ROOT-A", 4 * 1024 * 1024 * 1024)
    curr = _align(curr)
    # 1 - STATE: the user data partition, it takes whatever is left
    state_blocks = total_sectors - (curr + TAIL_RESERVED) // SECTOR_SIZE
    add(1, TYPE_DATA, "STATE", state_blocks * SECTOR_SIZE)

    return parts


def brunch_gpt_entries(disk_size: int) -> List[GptEntry]:
    """The Brunch layout as GPT entries, ready to be written."""
    return [part.as_gpt_entry() for part in brunch_layout(disk_size)]


# --------------------------------------------------------------- the source


def has_chromeos_magic(path: str) -> bool:
    """Whether the file starts with the ChromeOS boot code magic.

    The upstream installer uses this to tell a raw recovery image from the
    zip file it was packed in.
    """
    try:
        with open(path, "rb") as handle:
            return handle.read(4) == CHROMEOS_MAGIC
    except OSError:
        return False


def is_chromeos_recovery_image(path: str) -> bool:
    """Whether the file is a ChromeOS recovery image Brunch can use.

    The upstream installer accepts an image when the EFI partition is there
    and the third partition is the root filesystem ("ROOT-A"). The same
    two things are checked here, and the root filesystem is also accepted
    when it carries the ChromeOS type GUID instead of the name.
    """
    gpt = read_gpt(path)
    if gpt is None:
        return False

    has_efi = gpt.find("EFI-SYSTEM") is not None
    has_root = gpt.find("ROOT-A") is not None or any(
        part.type_guid == TYPE_ROOTFS for part in gpt.partitions
    )
    return has_efi and has_root


def source_partitions(path: str) -> Dict[int, GptPartition]:
    """The partitions of a ChromeOS recovery image, keyed by GPT number."""
    gpt = read_gpt(path)
    if gpt is None:
        return {}
    return {part.index + 1: part for part in gpt.partitions}


def _find_source(
    parts: Dict[int, GptPartition],
    index: int,
    names: tuple = (),
) -> Optional[GptPartition]:
    """Find a source partition by GPT number, falling back to its name."""
    part = parts.get(index)
    if part is not None:
        return part
    wanted = {name.upper() for name in names}
    for candidate in parts.values():
        if candidate.name.upper() in wanted:
            return candidate
    return None


@dataclass
class CopyJob:
    """One partition to copy from the source image onto the drive."""

    dest: BrunchPartition
    source_path: str
    source_offset: int
    length: int

    @property
    def dest_offset(self) -> int:
        return self.dest.offset


def build_copy_plan(
    source_image: str,
    layout: List[BrunchPartition],
    rootc_path: str,
    efi_path: str,
) -> List[CopyJob]:
    """Plan the copy of every partition of the Brunch layout.

    The mapping follows the upstream script: the kernel of the recovery
    image is copied to both kernel slots, the root filesystem to both root
    slots, ROOT-C gets the Brunch root filesystem and EFI-SYSTEM gets the
    Brunch bootloader. The reserved partitions stay empty.
    """
    parts = source_partitions(source_image)
    if not parts:
        raise ValueError("The ChromeOS image has no partition table")

    kernel = _find_source(parts, 4, ("KERN-A", "KERN-B"))
    rootfs = _find_source(parts, 3, ("ROOT-A", "ROOT-B"))
    state = _find_source(parts, 1, ("STATE",))
    oem = _find_source(parts, 8, ("OEM",))
    rwfw = _find_source(parts, 11, ("RWFW",))

    if kernel is None or rootfs is None:
        raise ValueError(
            "The ChromeOS image does not have the kernel and root "
            "filesystem partitions Brunch needs"
        )

    jobs: List[CopyJob] = []
    for part in layout:
        if part.index in (6, 9, 10):
            continue                      # reserved / kernel placeholder
        if part.index == 7:
            jobs.append(CopyJob(part, rootc_path, 0,
                                os.path.getsize(rootc_path)))
            continue
        if part.index == 12:
            jobs.append(CopyJob(part, efi_path, 0,
                                os.path.getsize(efi_path)))
            continue

        if part.index in (2, 4):
            source = kernel
        elif part.index in (3, 5):
            source = rootfs
        elif part.index == 1:
            source = state
        elif part.index == 8:
            source = oem
        elif part.index == 11:
            source = rwfw
        else:
            continue

        if source is None:
            continue
        jobs.append(CopyJob(
            part,
            source_image,
            source.first_lba * SECTOR_SIZE,
            # Never write past the end of the destination partition: that
            # would run into the next one and break the layout.
            min(source.size_sectors * SECTOR_SIZE, part.size),
        ))

    return jobs


def total_copy_bytes(jobs: List[CopyJob]) -> int:
    """How many bytes the copy plan writes."""
    return sum(job.length for job in jobs)


# ------------------------------------------------------------------ assets


@dataclass
class BrunchRelease:
    """A Brunch release and the files Yaku needs from it."""

    tag: str
    url: str
    files: tuple = ("rootc.img", "efi_secure.img", "efi_legacy.img")


# The stable release Yaku downloads when the user asks for Brunch.
DEFAULT_RELEASE = BrunchRelease(
    tag="r154-stable-20261010",
    url=(
        "https://github.com/sebanc/brunch/releases/download/"
        "r154-stable-20261010/brunch_r154_stable_20261010.tar.gz"
    ),
)

BRUNCH_URL = "https://github.com/sebanc/brunch"


class BrunchAssets:
    """The Brunch files (root filesystem and bootloaders) on disk."""

    def __init__(self, directory: str, release: BrunchRelease = DEFAULT_RELEASE):
        self.directory = Path(directory)
        self.release = release

    # ---------------------------------------------------------------- paths
    @property
    def rootc_path(self) -> Path:
        return self.directory / "rootc.img"

    @property
    def efi_secure_path(self) -> Path:
        return self.directory / "efi_secure.img"

    @property
    def efi_legacy_path(self) -> Path:
        return self.directory / "efi_legacy.img"

    def efi_path(self, legacy: bool = False) -> Path:
        """The bootloader matching the requested boot mode."""
        return self.efi_legacy_path if legacy else self.efi_secure_path

    def is_available(self, legacy: Optional[bool] = None) -> bool:
        """Whether the files Brunch needs are already downloaded.

        With ``legacy`` set, only the bootloader for that boot mode has to
        be there; without it the whole release is required.
        """
        return not self.missing_files(legacy)

    def missing_files(self, legacy: Optional[bool] = None) -> List[str]:
        """The files that still have to be downloaded."""
        needed = [self.rootc_path]
        if legacy is None:
            needed += [self.efi_secure_path, self.efi_legacy_path]
        else:
            needed.append(self.efi_path(legacy))
        return [
            path.name for path in needed
            if not path.is_file() or path.stat().st_size == 0
        ]

    # ------------------------------------------------------------- download
    def download(
        self,
        progress: Optional[Callable[[int, int], None]] = None,
        stopped: Optional[Callable[[], bool]] = None,
    ) -> None:
        """Download and unpack the Brunch release.

        Raises:
            BrunchError: when the download or the extraction failed.
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        archive = self.directory / f"brunch_{self.release.tag}.tar.gz"

        if not archive.is_file():
            self._download_archive(archive, progress, stopped)

        self._extract(archive)

    def _download_archive(
        self,
        archive: Path,
        progress: Optional[Callable[[int, int], None]],
        stopped: Optional[Callable[[], bool]],
    ) -> None:
        partial = archive.with_suffix(archive.suffix + ".part")
        try:
            with urllib.request.urlopen(self.release.url, timeout=60) as response:
                total = int(response.headers.get("Content-Length") or 0)
                done = 0
                with open(partial, "wb") as handle:
                    while True:
                        if stopped and stopped():
                            raise BrunchError(_("The download was cancelled."))
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        handle.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total)
        except urllib.error.URLError as exc:
            partial.unlink(missing_ok=True)
            raise BrunchError(
                _("Could not download the Brunch files: {error}").format(
                    error=exc.reason
                )
            ) from exc
        except OSError as exc:
            partial.unlink(missing_ok=True)
            raise BrunchError(
                _("Could not save the Brunch files: {error}").format(error=exc)
            ) from exc

        partial.replace(archive)

    def _extract(self, archive: Path) -> None:
        """Unpack the files Yaku needs out of the release archive."""
        wanted = set(self.release.files)
        try:
            with tarfile.open(archive, "r:gz") as tar:
                members = [
                    member for member in tar.getmembers()
                    if os.path.basename(member.name) in wanted
                    and member.isfile()
                ]
                found = {os.path.basename(m.name) for m in members}
                missing = wanted - found
                if missing:
                    raise BrunchError(
                        _("The Brunch archive does not contain: {files}").format(
                            files=", ".join(sorted(missing))
                        )
                    )
                for member in members:
                    name = os.path.basename(member.name)
                    source = tar.extractfile(member)
                    if source is None:
                        continue
                    target = self.directory / name
                    with open(target, "wb") as handle:
                        shutil.copyfileobj(source, handle)
        except tarfile.TarError as exc:
            raise BrunchError(
                _("Could not unpack the Brunch files: {error}").format(error=exc)
            ) from exc
        except OSError as exc:
            raise BrunchError(
                _("Could not unpack the Brunch files: {error}").format(error=exc)
            ) from exc


class BrunchError(Exception):
    """Something went wrong while preparing the Brunch files."""


def default_assets_dir() -> str:
    """Where the Brunch files are kept between runs."""
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share"
    )
    return os.path.join(data_home, "yaku", "brunch", DEFAULT_RELEASE.tag)
