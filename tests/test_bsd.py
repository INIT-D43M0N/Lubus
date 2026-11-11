"""Tests for the detection of BSD and other Unix boot media."""
import os
import struct
import tempfile
import unittest
from unittest import mock

from yaku.core.bsd import (
    MBR_DRAGONFLY, MBR_FREEBSD, MBR_HAIKU, MBR_NETBSD, MBR_OPENBSD,
    MBR_SOLARIS, detect_unix_image, detect_unix_iso, has_mbr_signature,
    read_mbr_partitions,
)
from yaku.core.gpt import GptEntry, write_gpt
from yaku.core.images import ImageType, _detect_image_type, analyze_image

EFI_GUID = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
FREEBSD_BOOT_GUID = "83bd6b9d-7f41-11dc-be0b-001560b84f0f"
NETBSD_FFS_GUID = "49f48d5a-b10e-11dc-b99b-0019d1879648"
ILLUMOS_BOOT_GUID = "6a82cb45-1dd2-11b2-99a6-080020736631"


def _write_mbr(path: str, entries):
    """Write a 512 byte MBR with the given partition entries."""
    sector = bytearray(512)
    for index, (part_type, lba, size) in enumerate(entries):
        start = 446 + index * 16
        sector[start] = 0x00
        sector[start + 4] = part_type
        struct.pack_into("<I", sector, start + 8, lba)
        struct.pack_into("<I", sector, start + 12, size)
    sector[510:512] = b"\x55\xaa"
    with open(path, "wb") as handle:
        handle.write(bytes(sector))
        handle.truncate(4096)
    return path


class TestUnixIsoDetection(unittest.TestCase):
    """The BSDs are recognised from the files inside their ISOs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.plain = os.path.join(self.tmp.name, "plain.iso")
        with open(self.plain, "wb") as handle:
            handle.truncate(1024)
        self.hybrid = os.path.join(self.tmp.name, "hybrid.iso")
        with open(self.hybrid, "wb") as handle:
            handle.write(bytes(510) + b"\x55\xaa")
            handle.truncate(1024)

    def test_freebsd(self):
        paths = {"boot/isoboot", "usr/freebsd_dist/base.txz",
                 "etc/freebsd_update.conf"}
        found = detect_unix_iso(self.hybrid, paths)
        self.assertIsNotNone(found)
        self.assertEqual(found.name, "FreeBSD")
        self.assertTrue(found.bootable)

    def test_freebsd_cd_is_not_bootable(self):
        found = detect_unix_iso(self.plain, {"usr/freebsd_dist/base.txz"})
        self.assertIsNotNone(found)
        self.assertFalse(found.bootable)

    def test_openbsd(self):
        paths = {"8.0/amd64/bsd.rd", "8.0/amd64/cdboot",
                 "8.0/amd64/buildinfo"}
        found = detect_unix_iso(self.plain, paths)
        self.assertIsNotNone(found)
        self.assertEqual(found.name, "OpenBSD")
        self.assertFalse(found.bootable)
        self.assertIn("installXX.img", found.advice)

    def test_openbsd_other_version(self):
        found = detect_unix_iso(self.plain, {"7.6/amd64/bsd.rd"})
        self.assertIsNotNone(found)
        self.assertEqual(found.name, "OpenBSD")

    def test_netbsd(self):
        paths = {"etc/release", "boot.cfg",
                 "amd64/installation/miniroot/miniroot.kmod"}
        found = detect_unix_iso(self.plain, paths)
        self.assertIsNotNone(found)
        self.assertEqual(found.name, "NetBSD")
        self.assertFalse(found.bootable)

    def test_dragonfly(self):
        found = detect_unix_iso(self.plain, {"etc.hdd/rc.conf",
                                             "boot/efiboot.img"})
        self.assertIsNotNone(found)
        self.assertEqual(found.name, "DragonFly BSD")

    def test_illumos(self):
        found = detect_unix_iso(self.hybrid, {"kayak/installer/install-image",
                                              ".volsetid"})
        self.assertIsNotNone(found)
        self.assertEqual(found.name, "illumos")
        self.assertTrue(found.bootable)

    def test_haiku(self):
        found = detect_unix_iso(self.hybrid, {"esp.image",
                                              "haiku-boot-floppy.image"})
        self.assertIsNotNone(found)
        self.assertEqual(found.name, "Haiku")

    def test_hurd(self):
        found = detect_unix_iso(self.hybrid, {"boot/kernel/gnumach.gz"})
        self.assertIsNotNone(found)
        self.assertEqual(found.name, "GNU/Hurd")

    def test_linux_is_not_a_unix_image(self):
        paths = {"boot/grub/grub.cfg", "casper/filesystem.squashfs",
                 "live/vmlinuz"}
        self.assertIsNone(detect_unix_iso(self.hybrid, paths))

    def test_illumos_markers_do_not_collide_with_freebsd(self):
        # boot/isoboot and boot/cdboot exist in both, only the other
        # markers tell them apart.
        found = detect_unix_iso(self.hybrid, {"boot/isoboot", "boot/cdboot",
                                              "boot/gptzfsboot"})
        self.assertEqual(found.name, "illumos")
        found = detect_unix_iso(self.hybrid, {"boot/isoboot", "boot/cdboot",
                                              "usr/freebsd_dist/base.txz"})
        self.assertEqual(found.name, "FreeBSD")


class TestUnixRawImageDetection(unittest.TestCase):
    """The raw images of the BSDs are recognised from their partition table."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _path(self, name):
        return os.path.join(self.tmp.name, name)

    def test_mbr_types(self):
        for part_type, expected in (
            (MBR_FREEBSD, "FreeBSD"),
            (MBR_OPENBSD, "OpenBSD"),
            (MBR_NETBSD, "NetBSD"),
            (MBR_DRAGONFLY, "DragonFly BSD"),
            (MBR_HAIKU, "Haiku"),
            (MBR_SOLARIS, "illumos"),
        ):
            path = _write_mbr(self._path(f"mbr{part_type}.img"),
                              [(part_type, 1024, 2048)])
            found = detect_unix_image(path)
            self.assertIsNotNone(found, f"0x{part_type:02X} not detected")
            self.assertEqual(found.name, expected)
            self.assertTrue(found.bootable)
            self.assertFalse(found.is_iso)

    def test_efi_partition_is_not_an_os(self):
        path = _write_mbr(self._path("efi.img"), [(0xEF, 1024, 2048)])
        self.assertIsNone(detect_unix_image(path))

    def test_gpt_types(self):
        path = self._path("netbsd.img")
        with open(path, "wb") as handle:
            handle.truncate(8 * 1024 * 1024)
        entries = [
            GptEntry(1, EFI_GUID, "EFI", 2048, 4095),
            GptEntry(2, NETBSD_FFS_GUID, "NBImgRoot", 4096, 8191),
        ]
        with open(path, "r+b") as handle:
            write_gpt(handle.fileno(), entries, 8 * 1024 * 1024)
        found = detect_unix_image(path)
        self.assertIsNotNone(found)
        self.assertEqual(found.name, "NetBSD")
        self.assertTrue(found.efi)

    def test_freebsd_gpt(self):
        path = self._path("freebsd.img")
        with open(path, "wb") as handle:
            handle.truncate(8 * 1024 * 1024)
        entries = [GptEntry(1, FREEBSD_BOOT_GUID, "freebsd-boot", 34, 58)]
        with open(path, "r+b") as handle:
            write_gpt(handle.fileno(), entries, 8 * 1024 * 1024)
        found = detect_unix_image(path)
        self.assertIsNotNone(found)
        self.assertEqual(found.name, "FreeBSD")

    def test_illumos_gpt(self):
        path = self._path("omnios.img")
        with open(path, "wb") as handle:
            handle.truncate(8 * 1024 * 1024)
        entries = [GptEntry(1, ILLUMOS_BOOT_GUID, "illumos boot", 34, 2047)]
        with open(path, "r+b") as handle:
            write_gpt(handle.fileno(), entries, 8 * 1024 * 1024)
        found = detect_unix_image(path)
        self.assertIsNotNone(found)
        self.assertEqual(found.name, "illumos")

    def test_plain_disk_image(self):
        path = self._path("plain.img")
        with open(path, "wb") as handle:
            handle.truncate(4096)
        self.assertIsNone(detect_unix_image(path))


class TestMbrHelpers(unittest.TestCase):
    def test_mbr_signature(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "x.img")
            with open(path, "wb") as handle:
                handle.write(bytes(512))
            self.assertFalse(has_mbr_signature(path))
            with open(path, "r+b") as handle:
                handle.seek(510)
                handle.write(b"\x55\xaa")
            self.assertTrue(has_mbr_signature(path))

    def test_partition_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_mbr(os.path.join(tmp, "x.img"),
                              [(0xA6, 1024, 2048), (0, 0, 0)])
            parts = read_mbr_partitions(path)
            self.assertEqual(len(parts), 1)
            self.assertEqual(parts[0]["type"], 0xA6)
            self.assertEqual(parts[0]["lba"], 1024)
            self.assertEqual(parts[0]["size"], 2048)


class TestImageTypeDetection(unittest.TestCase):
    """The BSD media end up in their own image type."""

    def test_iso_with_bsd_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "install.iso")
            with open(path, "wb") as handle:
                handle.write(bytes(510) + b"\x55\xaa")
                handle.truncate(1024)
            paths = {"usr/freebsd_dist/base.txz"}
            with mock.patch("yaku.core.images._iso_paths", return_value=paths):
                self.assertEqual(_detect_image_type(path, paths),
                                 ImageType.BSD_UNIX)

    def test_raw_image_with_bsd_partition(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_mbr(os.path.join(tmp, "install.img"),
                              [(MBR_OPENBSD, 1024, 2048)])
            with mock.patch("yaku.core.images._iso_paths", return_value=None):
                self.assertEqual(_detect_image_type(path, None),
                                 ImageType.BSD_UNIX)

    def test_linux_iso_stays_linux(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "fedora.iso")
            with open(path, "wb") as handle:
                handle.write(bytes(510) + b"\x55\xaa")
                handle.truncate(1024)
            paths = {"casper/filesystem.squashfs", "boot/vmlinuz"}
            with mock.patch("yaku.core.images._iso_paths", return_value=paths):
                self.assertEqual(_detect_image_type(path, paths),
                                 ImageType.LINUX_ISHYBRID)


class TestAnalyzeBsdImage(unittest.TestCase):
    """The analysis fills in the name of the system and the advice."""

    def test_analyze_raw_openbsd_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_mbr(os.path.join(tmp, "install80.img"),
                              [(0xEF, 1024, 2048), (MBR_OPENBSD, 4096, 8192)])
            with mock.patch("yaku.core.images._iso_paths", return_value=None):
                info = analyze_image(path)
            self.assertEqual(info.image_type, ImageType.BSD_UNIX)
            self.assertEqual(info.os_name, "OpenBSD")
            self.assertTrue(info.is_efi_bootable)
            self.assertTrue(info.is_bios_bootable)
            self.assertEqual(info.boot_warning, "")

    def test_analyze_openbsd_cd_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "install80.iso")
            with open(path, "wb") as handle:
                handle.truncate(1024)
            paths = {"8.0/amd64/bsd.rd"}
            with mock.patch("yaku.core.images._iso_paths", return_value=paths):
                info = analyze_image(path)
            self.assertEqual(info.image_type, ImageType.BSD_UNIX)
            self.assertEqual(info.os_name, "OpenBSD")
            self.assertIn("installXX.img", info.boot_warning)


if __name__ == "__main__":
    unittest.main()
