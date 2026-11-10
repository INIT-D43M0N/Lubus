"""GPT (GUID Partition Table) parsing.

ChromeOS recovery images are raw disk images whose partition table tells
what they are: the layout is always the same (EFI-SYSTEM, CHROMEOS, OEM,
STATE). Reading the table is far more reliable than looking for strings in
the image bytes, and the same parser is used to inspect the macOS images
that are built on an Apple disk layout.
"""
import struct
from dataclasses import dataclass, field
from typing import List, Optional

# GPT always uses 512-byte logical sectors for its own structures.
SECTOR_SIZE = 512

GPT_SIGNATURE = b"EFI PART"
GPT_MIN_HEADER_SIZE = 92
GPT_ENTRY_SIZE = 128
GPT_ENTRY_NAME_BYTES = 72


@dataclass
class GptPartition:
    """One entry of the partition table."""
    index: int
    type_guid: str
    name: str
    first_lba: int
    last_lba: int
    attributes: int = 0

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
