"""GPT (GUID Partition Table) parsing and writing.

ChromeOS recovery images are raw disk images whose partition table tells
what they are: the layout is always the same (EFI-SYSTEM, CHROMEOS, OEM,
STATE). Reading the table is far more reliable than looking for strings in
the image bytes, and the same parser is used to inspect the macOS images
that are built on an Apple disk layout.

The writer produces the same byte layout as ChromeOS' own ``cgpt`` tool
(including the protective MBR with the ChromeOS boot GUID), so a table
built here is byte-compatible with what the firmware expects.
"""
import os
import struct
import uuid
import zlib
from dataclasses import dataclass, field
from typing import List, Optional

# GPT always uses 512-byte logical sectors for its own structures.
SECTOR_SIZE = 512

GPT_SIGNATURE = b"EFI PART"
GPT_MIN_HEADER_SIZE = 92
GPT_ENTRY_SIZE = 128
GPT_ENTRY_NAME_BYTES = 72
GPT_ENTRY_COUNT = 128
GPT_REVISION = 0x00010000

# Protective MBR (the "syslinux" layout ChromeOS uses): the boot GUID of the
# EFI partition lives in the MBR, next to the 0x1d9a magic.
PMBR_BOOTCODE_SIZE = 424
PMBR_BOOT_GUID_OFFSET = PMBR_BOOTCODE_SIZE
PMBR_DISK_ID_OFFSET = 440
PMBR_MAGIC_OFFSET = 444
PMBR_PARTITION_OFFSET = 446
PMBR_SIGNATURE_OFFSET = 510
PMBR_MAGIC = b"\x1d\x9a"
PMBR_TYPE_GPT = 0xEE

# Number of sectors kept free at the end of the disk for the backup table.
GPT_BACKUP_RESERVED_SECTORS = 33


@dataclass
class GptPartition:
    """One entry of the partition table."""
    index: int
    type_guid: str
    name: str
    first_lba: int
    last_lba: int
    attributes: int = 0
    unique_guid: str = ""

    @property
    def size_sectors(self) -> int:
        return max(0, self.last_lba - self.first_lba + 1)

    @property
    def size_bytes(self) -> int:
        return self.size_sectors * SECTOR_SIZE


@dataclass
class GptInfo:
    """The parsed partition table."""
    sector_size: int = SECTOR_SIZE
    disk_guid: str = ""
    first_usable_lba: int = 0
    last_usable_lba: int = 0
    partitions: List[GptPartition] = field(default_factory=list)

    @property
    def names(self) -> List[str]:
        """Partition names, upper-cased and without the empty ones."""
        return [p.name.upper() for p in self.partitions if p.name]

    def find(self, name: str) -> Optional[GptPartition]:
        """First partition with the given name (case-insensitive)."""
        wanted = name.upper()
        for part in self.partitions:
            if part.name.upper() == wanted:
                return part
        return None

    def has_layout(self, required, optional=()) -> bool:
        """Whether the table carries the given partition names.

        Every name in ``required`` has to be present. When ``optional`` is
        given, at least one of those names has to be present as well, so a
        stray partition with a familiar name is not enough.
        """
        names = set(self.names)
        if not {n.upper() for n in required}.issubset(names):
            return False
        if optional:
            return bool({n.upper() for n in optional} & names)
        return True


def guid_to_str(raw: bytes) -> str:
    """Decode a GUID stored in the GPT mixed-endian layout."""
    if len(raw) < 16:
        return ""
    d1 = int.from_bytes(raw[0:4], "little")
    d2 = int.from_bytes(raw[4:6], "little")
    d3 = int.from_bytes(raw[6:8], "little")
    d4 = int.from_bytes(raw[8:10], "big")
    return f"{d1:08x}-{d2:04x}-{d3:04x}-{d4:04x}-{raw[10:16].hex()}"


def _decode_name(raw: bytes) -> str:
    """Decode the UTF-16LE partition name."""
    name = raw[:GPT_ENTRY_NAME_BYTES].decode("utf-16-le", "ignore")
    return name.split("\x00", 1)[0].strip()


def read_gpt(path: str, sector_size: int = SECTOR_SIZE) -> Optional[GptInfo]:
    """Parse the GPT of an image file or block device.

    Returns ``None`` when there is no GPT (an MBR-only image, a plain ISO
    or a random file).
    """
    try:
        with open(path, "rb") as handle:
            header = handle.read(sector_size * 2)
            if len(header) < sector_size + GPT_MIN_HEADER_SIZE:
                return None
            if header[sector_size:sector_size + 8] != GPT_SIGNATURE:
                return None

            base = sector_size
            entry_lba = struct.unpack_from("<Q", header, base + 72)[0]
            entry_count = struct.unpack_from("<I", header, base + 80)[0]
            entry_size = struct.unpack_from("<I", header, base + 84)[0]
            if entry_size < GPT_ENTRY_SIZE or entry_count > 4096:
                return None

            disk_guid = guid_to_str(header[base + 56:base + 72])
            first_usable = struct.unpack_from("<Q", header, base + 40)[0]
            last_usable = struct.unpack_from("<Q", header, base + 48)[0]

            handle.seek(entry_lba * sector_size)
            table = handle.read(entry_count * entry_size)
    except OSError:
        return None

    partitions = []
    for index in range(entry_count):
        start = index * entry_size
        record = table[start:start + entry_size]
        if len(record) < GPT_ENTRY_SIZE:
            break
        type_guid = guid_to_str(record[0:16])
        if type_guid == "00000000-0000-0000-0000-000000000000":
            continue  # unused entry
        first_lba = struct.unpack_from("<Q", record, 32)[0]
        last_lba = struct.unpack_from("<Q", record, 40)[0]
        attributes = struct.unpack_from("<Q", record, 48)[0]
        partitions.append(GptPartition(
            index=index,
            type_guid=type_guid,
            name=_decode_name(record[56:56 + GPT_ENTRY_NAME_BYTES]),
            first_lba=first_lba,
            last_lba=last_lba,
            attributes=attributes,
            unique_guid=guid_to_str(record[16:32]),
        ))

    return GptInfo(
        sector_size=sector_size,
        disk_guid=disk_guid,
        first_usable_lba=first_usable,
        last_usable_lba=last_usable,
        partitions=partitions,
    )


def has_mbr_signature(path: str) -> bool:
    """Whether sector 0 carries the 0x55AA boot signature."""
    try:
        with open(path, "rb") as handle:
            sector = handle.read(SECTOR_SIZE)
    except OSError:
        return False
    return len(sector) >= 512 and sector[510:512] == b"\x55\xaa"


# =============================================================================
# Writing
# =============================================================================


@dataclass
class GptEntry:
    """One partition entry, ready to be written to a table."""

    index: int                      # 1-based GPT entry number
    type_guid: str
    name: str
    first_lba: int
    last_lba: int
    attributes: int = 0
    unique_guid: str = ""

    @property
    def size_sectors(self) -> int:
        return max(0, self.last_lba - self.first_lba + 1)

    def __post_init__(self):
        if not self.unique_guid:
            self.unique_guid = str(uuid.uuid4())


def str_to_guid_bytes(guid: str) -> bytes:
    """Encode a GUID string into the GPT mixed-endian layout."""
    parts = guid.split("-")
    if len(parts) != 5:
        raise ValueError(f"Invalid GUID: {guid!r}")
    d1 = int(parts[0], 16)
    d2 = int(parts[1], 16)
    d3 = int(parts[2], 16)
    tail = bytes.fromhex(parts[3] + parts[4])
    return (
        d1.to_bytes(4, "little")
        + d2.to_bytes(2, "little")
        + d3.to_bytes(2, "little")
        + tail
    )


def build_pmbr(total_sectors: int, boot_guid: str = "") -> bytes:
    """Build the protective MBR ChromeOS uses.

    The MBR carries the unique GUID of the EFI partition, so the firmware
    knows which partition to boot even before the GPT is parsed.
    """
    sector = bytearray(SECTOR_SIZE)
    if boot_guid:
        sector[PMBR_BOOT_GUID_OFFSET:PMBR_BOOT_GUID_OFFSET + 16] = \
            str_to_guid_bytes(boot_guid)
    sector[PMBR_MAGIC_OFFSET:PMBR_MAGIC_OFFSET + 2] = PMBR_MAGIC

    part = PMBR_PARTITION_OFFSET
    sector[part + 0] = 0x00              # not bootable
    sector[part + 1] = 0x00              # CHS: head
    sector[part + 2] = 0x02              # CHS: sector
    sector[part + 3] = 0x00              # CHS: cylinder
    sector[part + 4] = PMBR_TYPE_GPT     # protective type
    sector[part + 5] = 0xFF              # CHS: last head
    sector[part + 6] = 0xFF              # CHS: last sector
    sector[part + 7] = 0xFF              # CHS: last cylinder
    struct.pack_into("<I", sector, part + 8, 1)
    struct.pack_into("<I", sector, part + 12,
                     min(max(total_sectors - 1, 0), 0xFFFFFFFF))

    sector[PMBR_SIGNATURE_OFFSET] = 0x55
    sector[PMBR_SIGNATURE_OFFSET + 1] = 0xAA
    return bytes(sector)


def build_gpt_entries(entries: List[GptEntry]) -> bytes:
    """Build the partition entry array (128 entries of 128 bytes)."""
    table = bytearray(GPT_ENTRY_COUNT * GPT_ENTRY_SIZE)
    for entry in entries:
        if not 1 <= entry.index <= GPT_ENTRY_COUNT:
            raise ValueError(f"GPT entry index out of range: {entry.index}")
        record = bytearray(GPT_ENTRY_SIZE)
        record[0:16] = str_to_guid_bytes(entry.type_guid)
        record[16:32] = str_to_guid_bytes(entry.unique_guid)
        struct.pack_into("<Q", record, 32, entry.first_lba)
        struct.pack_into("<Q", record, 40, entry.last_lba)
        struct.pack_into("<Q", record, 48, entry.attributes)
        name = entry.name.encode("utf-16-le")[:GPT_ENTRY_NAME_BYTES]
        record[56:56 + len(name)] = name

        start = (entry.index - 1) * GPT_ENTRY_SIZE
        table[start:start + GPT_ENTRY_SIZE] = record
    return bytes(table)


def build_gpt_header(
    current_lba: int,
    backup_lba: int,
    first_usable: int,
    last_usable: int,
    disk_guid: str,
    entries_lba: int,
    entries_crc: int,
) -> bytes:
    """Build one GPT header (primary or backup)."""
    header = bytearray(SECTOR_SIZE)
    header[0:8] = GPT_SIGNATURE
    struct.pack_into("<I", header, 8, GPT_REVISION)
    struct.pack_into("<I", header, 12, GPT_MIN_HEADER_SIZE)
    struct.pack_into("<Q", header, 24, current_lba)
    struct.pack_into("<Q", header, 32, backup_lba)
    struct.pack_into("<Q", header, 40, first_usable)
    struct.pack_into("<Q", header, 48, last_usable)
    header[56:72] = str_to_guid_bytes(disk_guid)
    struct.pack_into("<Q", header, 72, entries_lba)
    struct.pack_into("<I", header, 80, GPT_ENTRY_COUNT)
    struct.pack_into("<I", header, 84, GPT_ENTRY_SIZE)
    struct.pack_into("<I", header, 88, entries_crc)
    crc = zlib.crc32(bytes(header[:GPT_MIN_HEADER_SIZE])) & 0xFFFFFFFF
    struct.pack_into("<I", header, 16, crc)
    return bytes(header)


def write_gpt(
    fd: int,
    entries: List[GptEntry],
    disk_size: int,
    sector_size: int = SECTOR_SIZE,
    boot_entry_index: Optional[int] = None,
) -> str:
    """Write a complete GPT (PMBR + primary + backup) to a file descriptor.

    ``fd`` has to be an open, writable block device or file. The table is
    written in place, so the caller owns the device exclusively.

    Args:
        fd: File descriptor of the target device.
        entries: The partition entries (1-based indices).
        disk_size: Size of the target in bytes.
        sector_size: Logical sector size of the target.
        boot_entry_index: Entry whose unique GUID goes into the PMBR, so the
            firmware boots that partition.

    Returns:
        The disk GUID that was used.
    """
    total_sectors = disk_size // sector_size
    if total_sectors < GPT_BACKUP_RESERVED_SECTORS + 4:
        raise ValueError("The target is too small for a GPT")

    # The entry array is 128 entries of 128 bytes, so it takes 32 sectors.
    table_sectors = (GPT_ENTRY_COUNT * GPT_ENTRY_SIZE) // sector_size

    disk_guid = str(uuid.uuid4())
    table = build_gpt_entries(entries)
    entries_crc = zlib.crc32(table) & 0xFFFFFFFF

    primary_entries_lba = 2
    first_usable = primary_entries_lba + table_sectors
    backup_lba = total_sectors - 1
    backup_entries_lba = backup_lba - table_sectors
    last_usable = backup_entries_lba - 1

    boot_guid = ""
    for entry in entries:
        if entry.index == boot_entry_index:
            boot_guid = entry.unique_guid
            break

    pmbr = build_pmbr(total_sectors, boot_guid)
    primary = build_gpt_header(
        current_lba=1,
        backup_lba=backup_lba,
        first_usable=first_usable,
        last_usable=last_usable,
        disk_guid=disk_guid,
        entries_lba=primary_entries_lba,
        entries_crc=entries_crc,
    )
    backup = build_gpt_header(
        current_lba=backup_lba,
        backup_lba=1,
        first_usable=first_usable,
        last_usable=last_usable,
        disk_guid=disk_guid,
        entries_lba=backup_entries_lba,
        entries_crc=entries_crc,
    )

    os_pwrite = _pwrite_all
    os_pwrite(fd, pmbr, 0)
    os_pwrite(fd, primary, sector_size)
    os_pwrite(fd, table, primary_entries_lba * sector_size)
    os_pwrite(fd, table, backup_entries_lba * sector_size)
    os_pwrite(fd, backup, backup_lba * sector_size)
    return disk_guid


def _pwrite_all(fd: int, data: bytes, offset: int):
    """Write every byte of ``data`` at ``offset``."""
    view = memoryview(data)
    while view:
        written = os.pwrite(fd, view, offset)
        if written <= 0:
            raise OSError(f"Short write at offset {offset}")
        view = view[written:]
        offset += written
