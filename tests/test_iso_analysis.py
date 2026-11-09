"""Regression tests for ISO analysis and loop-device mounting.

Covers the failure reported when selecting a Fedora KDE image:

* the raw byte scan matched the strings "windows", "bootmgr" and "bcd" that
  are embedded in the GRUB binaries shipped inside the ISO, so Linux media
  was misdetected as a Windows installation;
* hybrid images carry a partition table, so the ISO9660 filesystem lives in
  a child partition and the loop device itself exposes no Filesystem
  interface, which made mounting fail with
  "No such interface org.freedesktop.UDisks2.Filesystem".
"""
import os
import tempfile
from unittest.mock import patch

import pytest

from lubus.core.images import (
    ImageType,
    _iso_paths,
    analyze_image,
    detect_image_type,
    scan_iso_for_markers,
)
from lubus.udisks import client

# ---------------------------------------------------------------------------
# Minimal ISO9660 builder
# ---------------------------------------------------------------------------

SECTOR = 2048


def _record(name: bytes, extent: int, size: int, is_dir: bool) -> bytes:
    """Build one ISO9660 directory record."""
    rec = bytearray(33 + len(name))
    rec[0] = len(rec)
    if len(rec) % 2:
        rec += b"\x00"
        rec[0] = len(rec)
    rec[1] = 0
    rec[2:6] = extent.to_bytes(4, "little")
    rec[6:10] = extent.to_bytes(4, "big")
    rec[10:14] = size.to_bytes(4, "little")
    rec[14:18] = size.to_bytes(4, "big")
    rec[25] = 0x02 if is_dir else 0x00
    rec[32] = len(name)
    rec[33:33 + len(name)] = name
    return bytes(rec)


def _build_iso(path: str, tree: dict, hybrid: bool = False) -> str:
    """Write a minimal but valid ISO9660 image containing ``tree``.

    ``tree`` maps names to either a nested dict (directory) or bytes (file).
    """
    next_sector = [18]
    dirs = {}

    def build_dir(node):
        extent = next_sector[0]
        next_sector[0] += 1
        entries = [(b"\x00", extent, SECTOR, True), (b"\x01", extent, SECTOR, True)]
        for name, value in node.items():
            identifier = name.upper().encode() + b";1"
            if isinstance(value, dict):
                child = build_dir(value)
                entries.append((identifier, child, SECTOR, True))
            else:
                entries.append((identifier, 0, len(value), False))
        data = b"".join(_record(*entry) for entry in entries)
        data += b"\x00" * ((-len(data)) % SECTOR)
        dirs[extent] = data
        return extent

    root_extent = build_dir(tree)

    pvd = bytearray(SECTOR)
    pvd[0] = 1
    pvd[1:6] = b"CD001"
    pvd[6] = 1
    pvd[8:40] = b" " * 32
    pvd[40:72] = b" " * 32
    pvd[80:84] = next_sector[0].to_bytes(4, "little")
    pvd[84:88] = next_sector[0].to_bytes(4, "big")
    pvd[120:122] = (1).to_bytes(2, "little")
    pvd[122:124] = (1).to_bytes(2, "big")
    pvd[124:126] = (1).to_bytes(2, "little")
    pvd[126:128] = (1).to_bytes(2, "big")
    pvd[128:130] = SECTOR.to_bytes(2, "little")
    pvd[130:132] = SECTOR.to_bytes(2, "big")
    pvd[156:190] = _record(b"\x00", root_extent, SECTOR, True)

    terminator = bytearray(SECTOR)
    terminator[0] = 255
    terminator[1:6] = b"CD001"
    terminator[6] = 1

    image = bytearray(next_sector[0] * SECTOR)
    image[16 * SECTOR:17 * SECTOR] = pvd
    image[17 * SECTOR:18 * SECTOR] = terminator
    for extent, data in dirs.items():
        image[extent * SECTOR:extent * SECTOR + len(data)] = data

    if hybrid:
        # MBR boot signature: the image is an isohybrid.
        image[510:512] = b"\x55\xaa"

    with open(path, "wb") as handle:
        handle.write(image)
    return path


FEDORA_TREE = {
    "EFI": {
        "BOOT": {
            "BOOTX64.EFI": b"",
            "GRUB.CFG": b"",
        },
    },
    "LIVEOS": {
        "SQUASHFS.IMG": b"",
    },
    "BOOT": {
        "GRUB2": {
            "GRUB.CFG": b"",
            "I386_PC": {
                "BIOSDISK.MOD": b"",
            },
        },
    },
}

WINDOWS_TREE = {
    "SOURCES": {
        "INSTALL.WIM": b"",
    },
    "EFI": {
        "BOOT": {
            "BOOTX64.EFI": b"",
        },
    },
    "BOOTMGR": b"",
    "BOOTMGR.EFI": b"",
}


# ---------------------------------------------------------------------------
# ISO9660 tree parsing
# ---------------------------------------------------------------------------

class TestIsoTree:
    def test_tree_is_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            iso = _build_iso(os.path.join(tmp, "fedora.iso"), FEDORA_TREE)
            paths = _iso_paths(iso)
            assert paths is not None
            assert "efi/boot/bootx64.efi" in paths
            assert "liveos/squashfs.img" in paths
            assert "boot/grub2/i386_pc/biosdisk.mod" in paths

    def test_non_iso_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            blob = os.path.join(tmp, "disk.img")
            with open(blob, "wb") as handle:
                handle.write(b"\x00" * 8192)
            assert _iso_paths(blob) is None


# ---------------------------------------------------------------------------
# Image type detection
# ---------------------------------------------------------------------------

class TestImageTypeDetection:
    def test_fedora_like_linux_iso_is_not_windows(self):
        with tempfile.TemporaryDirectory() as tmp:
            iso = _build_iso(os.path.join(tmp, "fedora.iso"), FEDORA_TREE, hybrid=True)
            assert detect_image_type(iso) == ImageType.LINUX_ISHYBRID

    def test_windows_iso_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            iso = _build_iso(os.path.join(tmp, "windows.iso"), WINDOWS_TREE)
            assert detect_image_type(iso) == ImageType.WINDOWS_INSTALLER

    def test_bootloader_strings_do_not_trigger_windows(self):
        """GRUB embeds "windows", "bootmgr" and "bcd" in its binaries."""
        with tempfile.TemporaryDirectory() as tmp:
            tree = dict(FEDORA_TREE)
            tree["BOOT"] = dict(tree["BOOT"])
            tree["BOOT"]["GRUB.CFG"] = b"menuentry windows { bootmgr bcd }"
            iso = _build_iso(os.path.join(tmp, "fedora.iso"), tree, hybrid=True)
            assert detect_image_type(iso) == ImageType.LINUX_ISHYBRID

    def test_raw_scan_ignores_generic_bootloader_strings(self):
        with tempfile.TemporaryDirectory() as tmp:
            blob = os.path.join(tmp, "bootloader.bin")
            with open(blob, "wb") as handle:
                handle.write(b"\x00" * 4096 + b"windows bootmgr bcd" + b"\x00" * 4096)
            has_windows, has_linux = scan_iso_for_markers(blob)
            assert has_windows is False
            assert has_linux is False

    def test_raw_scan_still_finds_windows_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            blob = os.path.join(tmp, "windows.iso")
            with open(blob, "wb") as handle:
                handle.write(b"\x00" * 4096 + b"sources/install.wim" + b"\x00" * 4096)
            has_windows, _has_linux = scan_iso_for_markers(blob)
            assert has_windows is True


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

class TestAnalyzeImage:
    def test_bootability_is_read_from_the_iso_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            iso = _build_iso(os.path.join(tmp, "fedora.iso"), FEDORA_TREE, hybrid=True)
            info = analyze_image(iso)
            assert info.image_type == ImageType.LINUX_ISHYBRID
            assert info.is_efi_bootable is True
            assert info.is_bios_bootable is True

    def test_analysis_does_not_mount_the_image(self):
        """Analysis must not need UDisks2 (the source of the reported error)."""
        with tempfile.TemporaryDirectory() as tmp:
            iso = _build_iso(os.path.join(tmp, "fedora.iso"), FEDORA_TREE, hybrid=True)
            with patch("lubus.udisks.mount_iso_readonly") as mount:
                info = analyze_image(iso)
            assert mount.called is False
            assert info.image_type == ImageType.LINUX_ISHYBRID

    def test_analysis_survives_a_mount_failure(self):
        """A D-Bus/mount error must never abort the analysis."""
        with tempfile.TemporaryDirectory() as tmp:
            blob = os.path.join(tmp, "disk.img")
            with open(blob, "wb") as handle:
                handle.write(b"\x00" * 65536)
            with patch("lubus.udisks.mount_iso_readonly",
                       side_effect=client.UDisksError("D-Bus call failed")):
                info = analyze_image(blob)
            assert info.image_type == ImageType.UNKNOWN


# ---------------------------------------------------------------------------
# Loop device mounting
# ---------------------------------------------------------------------------

class TestLoopMounting:
    """A hybrid image exposes the filesystem on a child partition."""

    LOOP = "/org/freedesktop/UDisks2/block_devices/loop0"
    PART = "/org/freedesktop/UDisks2/block_devices/loop0p1"
    EFI = "/org/freedesktop/UDisks2/block_devices/loop0p2"

    def _objects(self):
        return {
            self.LOOP: {
                client.PARTITION_TABLE_IFACE: {},
                client.LOOP_IFACE: {},
            },
            self.PART: {
                client.PARTITION_IFACE: {"Table": self.LOOP},
                client.FILESYSTEM_IFACE: {},
                client.BLOCK_IFACE: {"Size": 3543603200, "Id": "iso9660"},
            },
            self.EFI: {
                client.PARTITION_IFACE: {"Table": self.LOOP},
                client.FILESYSTEM_IFACE: {},
                client.BLOCK_IFACE: {"Size": 31457280, "Id": "vfat"},
            },
        }

    def test_partition_is_mounted_instead_of_the_loop_device(self):
        mounted = []

        def fake_mount(conn, obj, readonly=False):
            mounted.append((obj, readonly))
            return "/run/media/test"

        with patch.object(client, "_get_objects", return_value=self._objects()), \
                patch.object(client, "mount_filesystem", side_effect=fake_mount):
            point, obj = client._mount_loop_filesystem(None, self.LOOP)

        assert mounted == [(self.PART, True)]
        assert point == "/run/media/test"
        assert obj == self.PART

    def test_plain_iso_mounts_the_loop_device(self):
        objects = {
            self.LOOP: {
                client.LOOP_IFACE: {},
                client.FILESYSTEM_IFACE: {},
            },
        }
        mounted = []

        def fake_mount(conn, obj, readonly=False):
            mounted.append(obj)
            return "/run/media/test"

        with patch.object(client, "_get_objects", return_value=objects), \
                patch.object(client, "mount_filesystem", side_effect=fake_mount):
            point, obj = client._mount_loop_filesystem(None, self.LOOP)

        assert mounted == [self.LOOP]
        assert obj == self.LOOP
        assert point == "/run/media/test"


if __name__ == "__main__":
    pytest.main([__file__])
