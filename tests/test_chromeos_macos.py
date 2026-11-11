"""Tests for the ChromeOS and macOS image support.

Covers:

* the GPT parser (partition names, layout checks);
* the detection of a ChromeOS recovery image, which is a raw disk image
  with the ChromeOS partition layout - the way Google writes it to a drive;
* the detection of a macOS installer disk image (Apple's DMG format);
* the discovery of the HFS+ volume inside a converted macOS image;
* the partition layout used for a macOS installer USB.
"""
import os
import struct
import tempfile

import pytest

from yaku.core.chromeos import is_chromeos_image
from yaku.core.gpt import guid_to_str, read_gpt
from yaku.core.images import ImageType, detect_image_type
from yaku.core.macos import (
    find_bootloader,
    find_hfsplus_volume,
    is_dmg,
    read_dmg_info,
)
from yaku.core.partitions import (
    PartitionType,
    get_macos_layout,
    get_partition_type_guid,
)

SECTOR = 512


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _guid(text: str) -> bytes:
    """Encode a GUID in the mixed-endian layout the GPT uses."""
    parts = text.split("-")
    return (
        int(parts[0], 16).to_bytes(4, "little")
        + int(parts[1], 16).to_bytes(2, "little")
        + int(parts[2], 16).to_bytes(2, "little")
        + int(parts[3], 16).to_bytes(2, "big")
        + bytes.fromhex(parts[4])
    )


def _build_gpt_image(path: str, partitions, payload: bytes = b"") -> str:
    """Write a raw disk image with a GPT.

    ``partitions`` is a list of (name, type_guid) tuples.
    """
    with open(path, "wb") as handle:
        # Protective MBR
        mbr = bytearray(SECTOR)
        mbr[446] = 0x00
        mbr[510:512] = b"\x55\xaa"
        handle.write(bytes(mbr))

        # GPT header
        header = bytearray(SECTOR)
        header[0:8] = b"EFI PART"
        header[8:12] = struct.pack("<I", 0x00010000)
        header[12:16] = struct.pack("<I", 92)
        header[24:32] = struct.pack("<Q", 1)
        header[32:40] = struct.pack("<Q", 0x22)
        header[40:48] = struct.pack("<Q", 34)
        header[48:56] = struct.pack("<Q", 0xFFFFFFFF)
        header[56:72] = _guid("12345678-1234-5678-9abc-def012345678")
        header[72:80] = struct.pack("<Q", 2)
        header[80:84] = struct.pack("<I", len(partitions))
        header[84:88] = struct.pack("<I", 128)
        handle.write(bytes(header))

        # Partition entries
        entries = bytearray(128 * 16)
        for index, (name, type_guid) in enumerate(partitions):
            base = index * 128
            entries[base:base + 16] = _guid(type_guid)
            entries[base + 16:base + 32] = _guid("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
            entries[base + 32:base + 40] = struct.pack("<Q", 2048 + index * 8192)
            entries[base + 40:base + 48] = struct.pack("<Q", 8191)
            entries[base + 56:base + 56 + len(name) * 2] = name.encode("utf-16-le")
        handle.write(bytes(entries))
        handle.write(payload)
    return path


ESP_GUID = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
CHROMEOS_KERNEL_GUID = "fe3a2a5d-4f32-41a7-b725-accc3285a309"
CHROMEOS_ROOTFS_GUID = "3cb8e202-3b7e-47dd-8a3c-7ff2a13cfcec"

CHROMEOS_LAYOUT = [
    ("EFI-SYSTEM", ESP_GUID),
    ("CHROMEOS", CHROMEOS_KERNEL_GUID),
    ("OEM", CHROMEOS_ROOTFS_GUID),
    ("STATE", CHROMEOS_ROOTFS_GUID),
]


def _build_dmg(path: str, payload_size: int = 4096) -> str:
    """Write a file shaped like an Apple disk image."""
    with open(path, "wb") as handle:
        handle.write(b"\x00" * 4096)
        handle.write(os.urandom(payload_size))
        trailer = bytearray(512)
        trailer[0:4] = b"koly"
        trailer[4:8] = struct.pack(">I", 4)          # version
        trailer[16:20] = struct.pack(">I", 0)         # data fork offset
        trailer[20:24] = struct.pack(">I", payload_size)   # data fork length
        trailer[36:40] = struct.pack(">I", 1)         # segment count
        trailer[48:52] = struct.pack(">I", 4096)      # XML offset
        trailer[52:56] = struct.pack(">I", 128)       # XML length
        handle.write(bytes(trailer))
    return path


def _build_hfsplus_image(path: str, block_size: int = 4096,
                         total_blocks: int = 256) -> str:
    """Write a raw image holding a small HFS+ volume."""
    volume_size = block_size * total_blocks
    with open(path, "wb") as handle:
        # A 512 byte Apple style MBR in front of the volume
        mbr = bytearray(SECTOR)
        mbr[446] = 0x00
        mbr[510:512] = b"\x55\xaa"
        handle.write(bytes(mbr))

        volume = bytearray(volume_size)
        # The HFS+ header sits 1024 bytes into the volume
        header = 1024
        volume[header:header + 2] = b"H+"
        volume[header + 2:header + 4] = struct.pack(">H", 4)
        volume[header + 40:header + 44] = struct.pack(">I", block_size)
        volume[header + 44:header + 48] = struct.pack(">I", total_blocks)
        handle.write(bytes(volume))
    return path


# ---------------------------------------------------------------------------
# GPT
# ---------------------------------------------------------------------------

class TestGpt:
    def test_parses_the_partition_names(self, tmp_path):
        path = _build_gpt_image(str(tmp_path / "disk.img"), CHROMEOS_LAYOUT)

        gpt = read_gpt(path)

        assert gpt is not None
        assert [p.name for p in gpt.partitions] == [
            "EFI-SYSTEM", "CHROMEOS", "OEM", "STATE",
        ]
        assert gpt.partitions[0].type_guid == ESP_GUID
        assert gpt.partitions[1].first_lba == 2048 + 8192

    def test_returns_none_without_a_gpt(self, tmp_path):
        path = tmp_path / "plain.img"
        path.write_bytes(b"\x00" * 4096)

        assert read_gpt(str(path)) is None

    def test_layout_check(self, tmp_path):
        path = _build_gpt_image(str(tmp_path / "disk.img"), CHROMEOS_LAYOUT)
        gpt = read_gpt(path)

        assert gpt.has_layout(("EFI-SYSTEM", "CHROMEOS"))
        assert not gpt.has_layout(("EFI-SYSTEM", "NOTTHERE"))

    def test_guid_round_trip(self):
        assert guid_to_str(_guid("c12a7328-f81f-11d2-ba4b-00a0c93ec93b")) == \
            "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"


# ---------------------------------------------------------------------------
# ChromeOS
# ---------------------------------------------------------------------------

class TestChromeOS:
    def test_recovery_image_is_recognised(self, tmp_path):
        path = _build_gpt_image(str(tmp_path / "chromeos_image.bin"), CHROMEOS_LAYOUT)

        assert is_chromeos_image(path) is True
        assert detect_image_type(path) == ImageType.CHROMEOS

    def test_a_plain_linux_disk_image_is_not_chromeos(self, tmp_path):
        path = _build_gpt_image(
            str(tmp_path / "linux.img"),
            [("EFI System Partition", ESP_GUID), ("root", CHROMEOS_ROOTFS_GUID)],
        )

        assert is_chromeos_image(path) is False
        assert detect_image_type(path) != ImageType.CHROMEOS

    def test_a_file_without_a_partition_table_is_not_chromeos(self, tmp_path):
        path = tmp_path / "random.bin"
        path.write_bytes(b"\x00" * 8192)

        assert is_chromeos_image(str(path)) is False

    def test_detection_does_not_need_the_kernel_strings(self, tmp_path):
        """The partition table decides, not the bytes of the kernel.

        A recovery image without any of the marker strings used by the raw
        byte scan still has to be recognised.
        """
        path = _build_gpt_image(str(tmp_path / "chromeos_image.bin"), CHROMEOS_LAYOUT)

        assert detect_image_type(path) == ImageType.CHROMEOS

    def test_the_current_recovery_layout_is_recognised(self, tmp_path):
        """The images Google ships now use ROOT-A and KERN-A.

        The names changed over the years, so the detection has to work
        with the partition types as well.
        """
        layout = [
            ("STATE", "0fc63daf-8483-4772-8e79-3d69d8477de4"),
            ("KERN-A", CHROMEOS_KERNEL_GUID),
            ("ROOT-A", CHROMEOS_ROOTFS_GUID),
            ("KERN-B", CHROMEOS_KERNEL_GUID),
            ("ROOT-B", CHROMEOS_ROOTFS_GUID),
            ("OEM", "0fc63daf-8483-4772-8e79-3d69d8477de4"),
            ("EFI-SYSTEM", ESP_GUID),
        ]
        path = _build_gpt_image(str(tmp_path / "chromeos_image.bin"), layout)

        assert is_chromeos_image(path) is True
        assert detect_image_type(path) == ImageType.CHROMEOS

    def test_a_linux_image_with_only_an_efi_partition_is_not_chromeos(self, tmp_path):
        layout = [
            ("EFI System Partition", ESP_GUID),
            ("boot", "0fc63daf-8483-4772-8e79-3d69d8477de4"),
        ]
        path = _build_gpt_image(str(tmp_path / "linux.img"), layout)

        assert is_chromeos_image(path) is False


# ---------------------------------------------------------------------------
# macOS
# ---------------------------------------------------------------------------

class TestMacos:
    def test_dmg_is_recognised(self, tmp_path):
        path = _build_dmg(str(tmp_path / "installer.dmg"))

        assert is_dmg(path) is True
        assert detect_image_type(path) == ImageType.MACOS

    def test_a_non_dmg_is_not_a_dmg(self, tmp_path):
        path = tmp_path / "installer.iso"
        path.write_bytes(b"\x00" * 8192)

        assert is_dmg(str(path)) is False
        assert detect_image_type(str(path)) != ImageType.MACOS

    def test_dmg_trailer_is_parsed(self, tmp_path):
        path = _build_dmg(str(tmp_path / "installer.dmg"))

        info = read_dmg_info(path)

        assert info is not None
        assert info.version == 4
        assert info.data_length == 4096

    def test_hfsplus_volume_is_found_behind_the_partition_table(self, tmp_path):
        path = _build_hfsplus_image(str(tmp_path / "macos.img"))

        volume = find_hfsplus_volume(path)

        assert volume is not None
        assert volume.offset == SECTOR
        assert volume.block_size == 4096
        assert volume.total_blocks == 256
        assert volume.size == 4096 * 256

    def test_no_volume_in_a_random_image(self, tmp_path):
        path = tmp_path / "random.img"
        path.write_bytes(b"\x00" * 65536)

        assert find_hfsplus_volume(str(path)) is None

    def test_bootloader_is_found_in_the_extracted_tree(self, tmp_path):
        tree = tmp_path / "tree"
        preboot = tree / "Preboot" / "0A2B3C4D"
        preboot.mkdir(parents=True)
        (preboot / "boot.efi").write_bytes(b"\xef\x01")
        (preboot / "helper.bin").write_bytes(b"x")

        found = find_bootloader(str(tree))

        assert found is not None
        assert found.endswith("boot.efi")

    def test_no_bootloader_in_an_empty_tree(self, tmp_path):
        tree = tmp_path / "tree"
        tree.mkdir()

        assert find_bootloader(str(tree)) is None


class TestMacosLayout:
    def test_layout_has_an_efi_and_an_hfs_partition(self):
        layout = get_macos_layout("macOS", 16 * 1024 ** 3)

        assert layout.scheme.value == "gpt"
        assert len(layout.partitions) == 2
        efi, hfs = layout.partitions
        assert efi.type == PartitionType.EFI_SYSTEM
        assert efi.size == 200 * 1024 * 1024
        assert hfs.type == PartitionType.APPLE_HFS
        assert hfs.size == 0  # takes the rest of the drive

    def test_partition_type_guids(self):
        scheme = get_macos_layout("macOS", 16 * 1024 ** 3).scheme

        assert get_partition_type_guid(PartitionType.EFI_SYSTEM, scheme) == \
            "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
        assert get_partition_type_guid(PartitionType.APPLE_HFS, scheme) == \
            "7c3457ef-0000-11aa-aa11-00306543ecac"


# ---------------------------------------------------------------------------
# The write path
# ---------------------------------------------------------------------------

class TestWriteMacos:
    """The macOS writer, with the privileged UDisks calls replaced."""

    def _run(self, tmp_path, tree_has_bootloader=True):
        from unittest.mock import patch
        from yaku.core import writer

        dmg = _build_dmg(str(tmp_path / "installer.dmg"))
        raw = _build_hfsplus_image(str(tmp_path / "macos.img"))

        tree = tmp_path / "tree"
        preboot = tree / "Preboot" / "0A2B"
        preboot.mkdir(parents=True)
        (preboot / "boot.efi").write_bytes(b"\xef\x01BOOT")

        target = tmp_path / "partition.img"

        calls = {"partitions": [], "unformatted": [], "wiped": []}

        def fake_open_device(conn, node, mode):
            """Stand in for the privileged device open.

            The writer owns (and closes) the descriptor it gets back.
            """
            return os.open(str(target), os.O_RDWR | os.O_CREAT)

        def fake_create_partition(conn, device, offset, size, part_type, name,
                                  fs_type, label, scheme):
            calls["partitions"].append({
                "offset": offset, "size": size, "type": part_type,
                "name": name, "fs": fs_type, "label": label, "scheme": scheme,
            })
            return "/org/freedesktop/UDisks2/block_devices/sdb1"

        def fake_create_unformatted(conn, device, offset, size, part_type,
                                    name, scheme):
            calls["unformatted"].append({
                "offset": offset, "size": size, "type": part_type,
                "name": name, "scheme": scheme,
            })
            return "/org/freedesktop/UDisks2/block_devices/sdb2"

        esp_mount = tmp_path / "esp"
        esp_mount.mkdir()

        with patch.object(writer, "bus", return_value=None), \
                patch.object(writer, "resolve", return_value="/dev/sdb"), \
                patch.object(writer, "unmount_all"), \
                patch.object(writer, "release_stale_iso_mounts", return_value=0), \
                patch.object(writer, "wipe_partition_table",
                             side_effect=lambda c, d, s: calls["wiped"].append(s)), \
                patch.object(writer, "create_partition",
                             side_effect=fake_create_partition), \
                patch.object(writer, "create_partition_unformatted",
                             side_effect=fake_create_unformatted), \
                patch.object(writer, "get_device_size",
                             return_value=16 * 1024 ** 3), \
                patch.object(writer, "mount_filesystem",
                             return_value=str(esp_mount)), \
                patch.object(writer, "unmount_filesystem"), \
                patch.object(writer, "open_device_exclusive",
                             side_effect=fake_open_device), \
                patch.object(writer, "_wait_for_device_node",
                             return_value="/dev/sdb2"), \
                patch.object(writer, "extract_dmg",
                             return_value=raw), \
                patch.object(writer, "extract_dmg_tree",
                             return_value=str(tree) if tree_has_bootloader else None), \
                patch.object(writer, "find_bootloader",
                             return_value=str(preboot / "boot.efi")
                             if tree_has_bootloader else None):
            progress = []
            ok = writer.write_macos_installer(
                dmg, "/dev/sdb",
                lambda done, total: progress.append((done, total)),
                lambda: False,
            )

        return ok, calls, target, esp_mount, progress

    def test_creates_the_apple_layout(self, tmp_path):
        ok, calls, _target, _esp, _progress = self._run(tmp_path)

        assert ok is True
        assert calls["wiped"] == ["gpt"]

        # The EFI partition comes first and is formatted as FAT32.
        assert len(calls["partitions"]) == 1
        efi = calls["partitions"][0]
        assert efi["type"] == "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
        assert efi["fs"] == "vfat"
        assert efi["size"] == 200 * 1024 * 1024

        # The HFS+ partition is created without formatting: the volume is
        # written as raw blocks.
        assert len(calls["unformatted"]) == 1
        hfs = calls["unformatted"][0]
        assert hfs["type"] == "7c3457ef-0000-11aa-aa11-00306543ecac"
        assert hfs["offset"] >= efi["offset"] + efi["size"]

    def test_the_bootloader_lands_in_the_efi_partition(self, tmp_path):
        _ok, _calls, _target, esp_mount, _progress = self._run(tmp_path)

        bootloader = esp_mount / "EFI" / "BOOT" / "boot.efi"
        assert bootloader.is_file()
        assert bootloader.read_bytes() == b"\xef\x01BOOT"

    def test_the_hfs_volume_is_written_to_the_partition(self, tmp_path):
        _ok, _calls, target, _esp, progress = self._run(tmp_path)

        # Only the volume is copied - the Apple MBR in front of it stays
        # behind, the partition starts with the HFS+ volume itself.
        assert target.stat().st_size == 4096 * 256
        # The HFS+ header sits 1024 bytes into the volume.
        assert target.read_bytes()[1024:1026] == b"H+"
        assert progress and progress[-1][0] == 4096 * 256

    def test_without_a_bootloader_the_installer_is_still_written(self, tmp_path):
        ok, _calls, target, esp_mount, _progress = self._run(
            tmp_path, tree_has_bootloader=False
        )

        assert ok is True
        assert target.stat().st_size == 4096 * 256
        assert not (esp_mount / "EFI").exists()


if __name__ == "__main__":
    pytest.main([__file__])
