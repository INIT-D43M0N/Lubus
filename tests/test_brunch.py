"""Tests for the Brunch (ChromeOS on a generic PC) support."""
import os
import struct
import tempfile
import unittest
from unittest import mock

from lubus.core.brunch import (
    ATTR_KERN_A, ATTR_KERN_B, ATTR_KERN_C, BrunchAssets, BrunchError,
    MIN_DRIVE_SIZE, TYPE_DATA, TYPE_EFI, TYPE_FIRMWARE, TYPE_KERNEL,
    TYPE_RESERVED, TYPE_ROOTFS, brunch_gpt_entries, brunch_layout,
    build_copy_plan, is_chromeos_recovery_image, kernel_attributes,
    source_partitions,
)
from lubus.core.gpt import (
    GptEntry, read_gpt, str_to_guid_bytes, write_gpt,
)

DISK_SIZE = 20 * 1024 ** 3


def _write_synthetic_recovery(path: str, sectors: int = 64):
    """Build a small file that looks like a ChromeOS recovery image.

    The layout follows the real one: EFI-SYSTEM at index 12, ROOT-A at
    index 3 and the kernel at index 4.
    """
    size = sectors * 512
    with open(path, "wb") as handle:
        handle.write(b"\x33\xc0\xfa\x8e")
        handle.truncate(size)

    entries = [
        GptEntry(1, TYPE_DATA, "STATE", 34, 40),
        GptEntry(2, TYPE_KERNEL, "KERN-B", 41, 48),
        GptEntry(3, TYPE_ROOTFS, "ROOT-A", 49, 56),
        GptEntry(4, TYPE_KERNEL, "KERN-A", 57, 60),
        GptEntry(8, TYPE_DATA, "OEM", 61, 62),
        GptEntry(11, TYPE_FIRMWARE, "RWFW", 63, 63),
        GptEntry(12, TYPE_EFI, "EFI-SYSTEM", 64, 64),
    ]
    with open(path, "r+b") as handle:
        write_gpt(handle.fileno(), entries, size, boot_entry_index=12)

    # Fill the partitions with recognisable data.
    with open(path, "r+b") as handle:
        for index, label in ((3, "ROOT"), (4, "KERN"), (1, "STATE"),
                             (8, "OEM"), (11, "RWFW")):
            part = {p.index + 1: p for p in read_gpt(path).partitions}[index]
            handle.seek(part.first_lba * 512)
            handle.write(label.encode() * 64)

    # A real recovery image carries the ChromeOS boot code in the first
    # sector; the upstream installer checks for it.
    with open(path, "r+b") as handle:
        handle.seek(0)
        handle.write(b"\x33\xc0\xfa\x8e")
    return path


class TestBrunchLayout(unittest.TestCase):
    """The layout has to match the upstream chromeos-install.sh exactly."""

    def test_partition_count_and_indices(self):
        layout = brunch_layout(DISK_SIZE)
        self.assertEqual(len(layout), 12)
        self.assertEqual(
            sorted(part.index for part in layout),
            list(range(1, 13)),
        )

    def test_names_and_types(self):
        layout = {part.index: part for part in brunch_layout(DISK_SIZE)}
        self.assertEqual(layout[11].name, "RWFW")
        self.assertEqual(layout[11].type_guid, TYPE_FIRMWARE)
        self.assertEqual(layout[6].name, "KERN-C")
        self.assertEqual(layout[6].type_guid, TYPE_KERNEL)
        self.assertEqual(layout[7].name, "ROOT-C")
        self.assertEqual(layout[7].type_guid, TYPE_ROOTFS)
        self.assertEqual(layout[2].name, "KERN-A")
        self.assertEqual(layout[4].name, "KERN-B")
        self.assertEqual(layout[8].name, "OEM")
        self.assertEqual(layout[8].type_guid, TYPE_DATA)
        self.assertEqual(layout[12].name, "EFI-SYSTEM")
        self.assertEqual(layout[12].type_guid, TYPE_EFI)
        self.assertEqual(layout[3].name, "ROOT-A")
        self.assertEqual(layout[5].name, "ROOT-B")
        self.assertEqual(layout[1].name, "STATE")
        self.assertEqual(layout[9].type_guid, TYPE_RESERVED)
        self.assertEqual(layout[10].type_guid, TYPE_RESERVED)

    def test_known_offsets(self):
        """The offsets of the upstream script, in 512 byte sectors."""
        layout = {part.index: part for part in brunch_layout(DISK_SIZE)}
        self.assertEqual(layout[11].first_lba, 64)          # RWFW
        self.assertEqual(layout[6].first_lba, 16448)        # KERN-C
        self.assertEqual(layout[7].first_lba, 16456)        # ROOT-C
        self.assertEqual(layout[2].first_lba, 2117638)      # KERN-A
        self.assertEqual(layout[4].first_lba, 2248710)      # KERN-B
        self.assertEqual(layout[8].first_lba, 2379784)      # OEM
        self.assertEqual(layout[12].first_lba, 2543624)     # EFI-SYSTEM
        self.assertEqual(layout[5].first_lba, 2674696)      # ROOT-B
        self.assertEqual(layout[3].first_lba, 11063304)     # ROOT-A
        self.assertEqual(layout[1].first_lba, 19451912)     # STATE

    def test_sizes(self):
        layout = {part.index: part for part in brunch_layout(DISK_SIZE)}
        self.assertEqual(layout[11].size, 8 * 1024 * 1024)
        self.assertEqual(layout[6].size, 512)
        self.assertEqual(layout[7].size, 1024 * 1024 * 1024)
        self.assertEqual(layout[2].size, 64 * 1024 * 1024)
        self.assertEqual(layout[4].size, 64 * 1024 * 1024)
        self.assertEqual(layout[8].size, 16 * 1024 * 1024)
        self.assertEqual(layout[12].size, 64 * 1024 * 1024)
        self.assertEqual(layout[3].size, 4 * 1024 ** 3)
        self.assertEqual(layout[5].size, 4 * 1024 ** 3)

    def test_kernel_attributes(self):
        layout = {part.index: part for part in brunch_layout(DISK_SIZE)}
        self.assertEqual(layout[2].attributes, ATTR_KERN_A)
        self.assertEqual(layout[4].attributes, ATTR_KERN_B)
        self.assertEqual(layout[6].attributes, ATTR_KERN_C)
        # priority 15, tries 15, successful 0
        self.assertEqual(ATTR_KERN_A, 0x00FF000000000000)
        # priority 0, tries 15, successful 0
        self.assertEqual(ATTR_KERN_B, 0x00F0000000000000)
        self.assertEqual(kernel_attributes(3, 5, 1),
                         (3 << 48) | (5 << 52) | (1 << 56))

    def test_state_takes_the_rest(self):
        layout = {part.index: part for part in brunch_layout(DISK_SIZE)}
        state = layout[1]
        # The last 24 KiB stay free for the backup partition table.
        self.assertEqual(state.offset + state.size,
                         DISK_SIZE - 24576)

    def test_small_drive_is_refused(self):
        with self.assertRaises(ValueError):
            brunch_layout(MIN_DRIVE_SIZE - 512)


class TestGptWriter(unittest.TestCase):
    """The table Lubus writes has to be a valid GPT."""

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "disk.img")
            with open(path, "wb") as handle:
                handle.truncate(DISK_SIZE)
                entries = brunch_gpt_entries(DISK_SIZE)
                write_gpt(handle.fileno(), entries, DISK_SIZE,
                          boot_entry_index=12)

            gpt = read_gpt(path)
            self.assertIsNotNone(gpt)
            self.assertEqual(len(gpt.partitions), 12)
            names = gpt.names
            for expected in ("RWFW", "KERN-A", "KERN-B", "KERN-C", "ROOT-A",
                             "ROOT-B", "ROOT-C", "OEM", "EFI-SYSTEM", "STATE"):
                self.assertIn(expected, names)

            layout = {part.index: part for part in brunch_layout(DISK_SIZE)}
            for part in gpt.partitions:
                reference = layout[part.index + 1]
                self.assertEqual(part.type_guid, reference.type_guid)
                self.assertEqual(part.first_lba, reference.first_lba)
                self.assertEqual(part.last_lba, reference.last_lba)
                self.assertEqual(part.attributes, reference.attributes)

    def test_protective_mbr(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "disk.img")
            with open(path, "wb") as handle:
                handle.truncate(DISK_SIZE)
                entries = brunch_gpt_entries(DISK_SIZE)
                write_gpt(handle.fileno(), entries, DISK_SIZE,
                          boot_entry_index=12)

            with open(path, "rb") as handle:
                mbr = handle.read(512)
            self.assertEqual(mbr[510:512], b"\x55\xaa")
            self.assertEqual(mbr[0x1BC:0x1BE], b"\x1d\x9a")
            self.assertEqual(mbr[0x1C2], 0xEE)
            # The boot GUID of the MBR is the GUID of EFI-SYSTEM.
            gpt = read_gpt(path)
            efi = gpt.find("EFI-SYSTEM")
            self.assertEqual(mbr[0x1A8:0x1B8],
                             str_to_guid_bytes(efi.unique_guid))


class TestBrunchSource(unittest.TestCase):
    """Recognition of the ChromeOS recovery image."""

    def test_detects_recovery_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_synthetic_recovery(os.path.join(tmp, "chromeos.bin"))
            self.assertTrue(is_chromeos_recovery_image(path))

    def test_rejects_other_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "other.bin")
            with open(path, "wb") as handle:
                handle.write(b"not a chrome os image")
                handle.truncate(4096)
            self.assertFalse(is_chromeos_recovery_image(path))

    def test_rejects_missing_file(self):
        self.assertFalse(is_chromeos_recovery_image("/nonexistent/file.bin"))

    def test_source_partitions_by_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_synthetic_recovery(os.path.join(tmp, "chromeos.bin"))
            parts = source_partitions(path)
            self.assertIn(3, parts)
            self.assertIn(4, parts)
            self.assertIn(12, parts)
            self.assertEqual(parts[3].name, "ROOT-A")
            self.assertEqual(parts[4].name, "KERN-A")
            self.assertEqual(parts[12].name, "EFI-SYSTEM")


class TestCopyPlan(unittest.TestCase):
    """The mapping of the source partitions onto the Brunch layout."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = _write_synthetic_recovery(
            os.path.join(self.tmp.name, "chromeos.bin"))
        self.rootc = os.path.join(self.tmp.name, "rootc.img")
        with open(self.rootc, "wb") as handle:
            handle.write(b"R" * 4096)
        self.efi = os.path.join(self.tmp.name, "efi_secure.img")
        with open(self.efi, "wb") as handle:
            handle.write(b"E" * 2048)

    def _plan(self):
        layout = brunch_layout(DISK_SIZE)
        return build_copy_plan(self.source, layout, self.rootc, self.efi)

    def test_every_destination(self):
        plan = self._plan()
        destinations = {job.dest.index: job for job in plan}
        self.assertEqual(sorted(destinations), [1, 2, 3, 4, 5, 7, 8, 11, 12])

    def test_reserved_partitions_are_skipped(self):
        plan = self._plan()
        self.assertNotIn(6, {job.dest.index for job in plan})
        self.assertNotIn(9, {job.dest.index for job in plan})
        self.assertNotIn(10, {job.dest.index for job in plan})

    def test_kernel_and_rootfs_are_copied_twice(self):
        plan = {job.dest.index: job for job in self._plan()}
        parts = source_partitions(self.source)
        kernel = parts[4]
        rootfs = parts[3]
        for index in (2, 4):
            self.assertEqual(plan[index].source_path, self.source)
            self.assertEqual(plan[index].source_offset,
                             kernel.first_lba * 512)
        for index in (3, 5):
            self.assertEqual(plan[index].source_path, self.source)
            self.assertEqual(plan[index].source_offset,
                             rootfs.first_lba * 512)

    def test_brunch_files_go_to_rootc_and_efi(self):
        plan = {job.dest.index: job for job in self._plan()}
        self.assertEqual(plan[7].source_path, self.rootc)
        self.assertEqual(plan[7].length, 4096)
        self.assertEqual(plan[12].source_path, self.efi)
        self.assertEqual(plan[12].length, 2048)

    def test_copy_never_overruns_a_partition(self):
        for job in self._plan():
            self.assertLessEqual(job.length, job.dest.size)


class TestBrunchAssets(unittest.TestCase):
    """The Brunch files are downloaded once and then reused."""

    def test_missing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets = BrunchAssets(tmp)
            self.assertFalse(assets.is_available())
            self.assertEqual(len(assets.missing_files()), 3)

    def test_available_when_all_files_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets = BrunchAssets(tmp)
            for path in (assets.rootc_path, assets.efi_secure_path,
                         assets.efi_legacy_path):
                path.write_bytes(b"x" * 16)
            self.assertTrue(assets.is_available())

    def test_efi_path_follows_the_boot_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets = BrunchAssets(tmp)
            self.assertEqual(assets.efi_path(legacy=False),
                             assets.efi_secure_path)
            self.assertEqual(assets.efi_path(legacy=True),
                             assets.efi_legacy_path)

    def test_download_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets = BrunchAssets(tmp)
            assets.release = type(assets.release)(
                tag="broken", url="https://127.0.0.1:1/nothing.tar.gz")
            with self.assertRaises(BrunchError):
                assets.download()


class TestBrunchWriter(unittest.TestCase):
    """The writer puts the table and the partitions on the drive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

        self.disk = os.path.join(self.tmp.name, "disk.img")
        with open(self.disk, "wb") as handle:
            handle.truncate(DISK_SIZE)

        self.source = _write_synthetic_recovery(
            os.path.join(self.tmp.name, "chromeos.bin"))
        self.rootc = os.path.join(self.tmp.name, "rootc.img")
        with open(self.rootc, "wb") as handle:
            handle.write(b"R" * 8192)
        self.efi = os.path.join(self.tmp.name, "efi_secure.img")
        with open(self.efi, "wb") as handle:
            handle.write(b"E" * 4096)

    def _fake_open(self, _conn, _device, _mode="w", _flags=0):
        return os.open(self.disk, os.O_RDWR)

    def _run(self):
        from lubus.core import writer
        progress = []
        with mock.patch.object(writer, "bus", return_value=object()), \
             mock.patch.object(writer, "resolve", return_value="/dev/test"), \
             mock.patch.object(writer, "unmount_all"), \
             mock.patch.object(writer, "get_device_size",
                               return_value=DISK_SIZE), \
             mock.patch.object(writer, "rescan_device") as rescan, \
             mock.patch.object(writer, "open_device_exclusive",
                               side_effect=self._fake_open):
            result = writer.write_brunch_installer(
                self.source, "/dev/test", self.tmp.name,
                lambda done, total: progress.append((done, total)),
                lambda: False,
                status_cb=lambda _msg: None,
            )
        return result, progress, rescan

    def test_write_succeeds(self):
        result, progress, rescan = self._run()
        self.assertTrue(result)
        self.assertTrue(progress)
        self.assertEqual(progress[-1][0], progress[-1][1])
        # The system is asked to look at the new table.
        rescan.assert_called_once()

    def test_table_on_the_drive(self):
        self._run()
        gpt = read_gpt(self.disk)
        self.assertIsNotNone(gpt)
        self.assertEqual(len(gpt.partitions), 12)
        for name in ("RWFW", "KERN-A", "KERN-B", "KERN-C", "ROOT-A", "ROOT-B",
                     "ROOT-C", "OEM", "EFI-SYSTEM", "STATE"):
            self.assertIn(name, gpt.names)
        layout = {part.index: part for part in brunch_layout(DISK_SIZE)}
        for part in gpt.partitions:
            reference = layout[part.index + 1]
            self.assertEqual(part.first_lba, reference.first_lba)
            self.assertEqual(part.attributes, reference.attributes)

    def test_partition_contents(self):
        self._run()
        with open(self.disk, "rb") as handle:
            gpt = read_gpt(self.disk)
            for name, expected in (("ROOT-A", b"ROOT"), ("KERN-A", b"KERN"),
                                   ("STATE", b"STATE"), ("OEM", b"OEM"),
                                   ("RWFW", b"RWFW")):
                part = gpt.find(name)
                handle.seek(part.first_lba * 512)
                self.assertTrue(handle.read(64).startswith(expected),
                                f"{name} does not hold the source data")
            # ROOT-C gets the Brunch root filesystem, EFI-SYSTEM the bootloader
            part = gpt.find("ROOT-C")
            handle.seek(part.first_lba * 512)
            self.assertEqual(handle.read(4), b"RRRR")
            part = gpt.find("EFI-SYSTEM")
            handle.seek(part.first_lba * 512)
            self.assertEqual(handle.read(4), b"EEEE")

    def test_refuses_a_file_that_is_not_a_chromeos_image(self):
        from lubus.core import writer
        other = os.path.join(self.tmp.name, "other.bin")
        with open(other, "wb") as handle:
            handle.write(b"nothing to see here")
            handle.truncate(4096)
        with mock.patch.object(writer, "bus", return_value=object()), \
             mock.patch.object(writer, "resolve", return_value="/dev/test"), \
             mock.patch.object(writer, "unmount_all"), \
             mock.patch.object(writer, "get_device_size",
                               return_value=DISK_SIZE):
            with self.assertRaises(writer.WriteError):
                writer.write_brunch_installer(
                    other, "/dev/test", self.tmp.name,
                    lambda _d, _t: None, lambda: False,
                )


if __name__ == "__main__":
    unittest.main()
