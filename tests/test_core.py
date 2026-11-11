"""Tests for Yaku core modules."""
import os
import tempfile
import pytest
from unittest.mock import Mock, patch, MagicMock

# Test imports
from yaku.core.images import (
    ImageType, PartitionScheme, TargetSystem, FileSystem,
    read_volume_label, identify_distro, scan_iso_for_markers,
    detect_image_type, parse_wim_info,
)
from yaku.core.partitions import (
    PartitionType, PartitionSpec, PartitionLayout,
    get_windows_installer_layout, get_windows_togo_layout,
    get_generic_layout, calculate_partition_offsets,
    validate_layout, format_size,
)
from yaku.core.devices import Drive, BusType, format_size as dev_format_size
from yaku.core.verify import verify_device_matches_image
from yaku.config import load_config, save_config, DEFAULT_CONFIG
from yaku.utils.i18n import _, set_language, get_language


class TestFormatSize:
    """Test size formatting."""

    def test_bytes(self):
        assert format_size(512) == "512 B"
        assert format_size(1023) == "1023 B"

    def test_kibibytes(self):
        assert format_size(1024) == "1 KiB"
        assert format_size(1536) == "2 KiB"
        assert format_size(1024 * 1024 - 1) == "1024 KiB"

    def test_mebibytes(self):
        assert format_size(1024 * 1024) == "1 MiB"
        assert format_size(1024 * 1024 * 100) == "100 MiB"

    def test_gibibytes(self):
        assert format_size(1024 * 1024 * 1024) == "1 GiB"
        assert format_size(1024 * 1024 * 1024 * 50) == "50 GiB"

    def test_tebibytes(self):
        assert format_size(1024**4) == "1 TiB"


class TestPartitionLayouts:
    """Test partition layout generation."""

    def test_windows_installer_gpt(self):
        layout = get_windows_installer_layout(
            PartitionScheme.GPT, TargetSystem.UEFI,
            FileSystem.FAT32, "WINUSB", 16 * 1024**3
        )
        assert layout.scheme == PartitionScheme.GPT
        assert layout.target == TargetSystem.UEFI
        assert len(layout.partitions) == 2
        assert layout.partitions[0].type == PartitionType.EFI_SYSTEM
        assert layout.partitions[0].fs == FileSystem.FAT32
        assert layout.partitions[1].type == PartitionType.MICROSOFT_BASIC_DATA
        assert layout.partitions[1].fs == FileSystem.NTFS

    def test_windows_installer_mbr(self):
        layout = get_windows_installer_layout(
            PartitionScheme.MBR, TargetSystem.BIOS,
            FileSystem.NTFS, "WINUSB", 16 * 1024**3
        )
        assert layout.scheme == PartitionScheme.MBR
        assert layout.target == TargetSystem.BIOS
        assert len(layout.partitions) == 1
        assert layout.partitions[0].flags == ["boot"]

    def test_windows_togo(self):
        layout = get_windows_togo_layout(
            FileSystem.NTFS, "WINDOWS_TO_GO", 32 * 1024**3
        )
        assert layout.scheme == PartitionScheme.GPT
        assert layout.target == TargetSystem.UEFI
        assert len(layout.partitions) == 2

    def test_generic_gpt_uefi(self):
        layout = get_generic_layout(
            PartitionScheme.GPT, TargetSystem.UEFI,
            FileSystem.FAT32, "LINUX", 16 * 1024**3
        )
        assert len(layout.partitions) == 2
        assert layout.partitions[0].type == PartitionType.EFI_SYSTEM

    def test_generic_mbr_bios(self):
        layout = get_generic_layout(
            PartitionScheme.MBR, TargetSystem.BIOS,
            FileSystem.EXT4, "LINUX", 16 * 1024**3
        )
        assert len(layout.partitions) == 1
        assert layout.partitions[0].flags == ["boot"]


class TestPartitionOffsets:
    """Test partition offset calculation."""

    def test_gpt_two_partitions(self):
        layout = PartitionLayout(
            scheme=PartitionScheme.GPT,
            target=TargetSystem.UEFI,
            partitions=[
                PartitionSpec(PartitionType.EFI_SYSTEM, 512*1024*1024, FileSystem.FAT32, "ESP", "ESP", ["boot", "esp"]),
                PartitionSpec(PartitionType.LINUX_FILESYSTEM, 0, FileSystem.EXT4, "ROOT", "ROOT", []),
            ],
            alignment=1024*1024,
        )
        drive_size = 16 * 1024**3
        partitions = calculate_partition_offsets(layout, drive_size)

        assert len(partitions) == 2
        assert partitions[0].offset == 1024*1024  # 1 MiB alignment
        assert partitions[0].size == 512*1024*1024
        assert partitions[1].offset == partitions[0].offset + partitions[0].size
        assert partitions[1].size > 0


class TestValidateLayout:
    """Test layout validation."""

    def test_valid_layout(self):
        layout = get_windows_installer_layout(
            PartitionScheme.GPT, TargetSystem.UEFI,
            FileSystem.FAT32, "WINUSB", 16 * 1024**3
        )
        ok, msg = validate_layout(layout, 16 * 1024**3, 4 * 1024**3)
        assert ok

    def test_drive_too_small(self):
        # 100 MiB cannot hold the layout itself (512 MiB ESP + overhead)
        layout = get_windows_installer_layout(
            PartitionScheme.GPT, TargetSystem.UEFI,
            FileSystem.FAT32, "WINUSB", 16 * 1024**3
        )
        ok, msg = validate_layout(layout, 100 * 1024 * 1024, 50 * 1024 * 1024)
        assert not ok
        assert "too small" in msg.lower()

    def test_image_too_large(self):
        layout = get_generic_layout(
            PartitionScheme.GPT, TargetSystem.UEFI,
            FileSystem.FAT32, "TEST", 16 * 1024**3
        )
        ok, msg = validate_layout(layout, 16 * 1024**3, 32 * 1024**3)
        assert not ok
        assert "too large" in msg.lower()


class TestImageDetection:
    """Test ISO image detection."""

    def test_windows_markers(self):
        # Create a mock ISO with Windows markers
        with tempfile.NamedTemporaryFile(suffix=".iso", delete=False) as f:
            # Write a fake ISO with Windows markers
            f.write(b"\x00" * 32768)  # Skip to sector 16
            f.write(b"CD001")  # ISO signature
            f.write(b"\x00" * 32)
            f.write(b"WINDOWS_INSTALL\x00" + b"\x00" * 16)  # Volume label
            # Add Windows markers in the data area
            f.write(b"\x00" * 1000000)
            f.write(b"install.wim")
            f.write(b"\x00" * 1000000)
            f.write(b"bootmgr.efi")
            temp_path = f.name

        try:
            has_windows, has_linux = scan_iso_for_markers(temp_path)
            assert has_windows == True
            assert has_linux == False
            assert detect_image_type(temp_path) == ImageType.WINDOWS_INSTALLER
        finally:
            os.unlink(temp_path)

    def test_linux_markers(self):
        with tempfile.NamedTemporaryFile(suffix=".iso", delete=False) as f:
            f.write(b"\x00" * 32768)
            f.write(b"CD001")
            f.write(b"\x00" * 32)
            f.write(b"UBUNTU_22_04\x00" + b"\x00" * 16)
            f.write(b"\x00" * 1000000)
            f.write(b"casper")
            f.write(b"\x00" * 1000000)
            f.write(b"vmlinuz")
            temp_path = f.name

        try:
            has_windows, has_linux = scan_iso_for_markers(temp_path)
            assert has_windows == False
            assert has_linux == True
            # Since no MBR signature, it's LINUX_INSTALLER
            assert detect_image_type(temp_path) in (ImageType.LINUX_ISHYBRID, ImageType.LINUX_INSTALLER)
        finally:
            os.unlink(temp_path)

    def test_volume_label(self):
        # Build a minimal but standard-compliant Primary Volume Descriptor:
        # byte 0 = type, 1-5 = "CD001", 6 = version, 7-38 = system id,
        # 39-70 = volume id (ECMA-119)
        pvd = bytearray(72)
        pvd[0] = 0
        pvd[1:6] = b"CD001"
        pvd[6] = 1
        pvd[7:39] = b" " * 32
        pvd[39:71] = b"TEST_LABEL_123".ljust(32)

        with tempfile.NamedTemporaryFile(suffix=".iso", delete=False) as f:
            f.write(b"\x00" * (16 * 2048))  #sectors 0-15
            f.write(bytes(pvd))
            f.write(b"\x00" * 2048)
            temp_path = f.name

        try:
            label = read_volume_label(temp_path)
            assert label == "TEST_LABEL_123"
        finally:
            os.unlink(temp_path)

    def test_identify_distro(self):
        assert "Ubuntu" in identify_distro("Ubuntu 22.04", "ubuntu-22.04.iso")
        assert "Fedora" in identify_distro("Fedora", "fedora-38.iso")
        assert "Arch Linux" in identify_distro("Arch", "archlinux.iso")
        assert "Unknown" not in identify_distro("Custom", "custom.iso")


class TestWIMInfoParsing:
    """Test WIM info parsing."""

    def test_parse_wim_info(self):
        sample_output = """Index: 1
Name: Windows 11 Pro
Description: Windows 11 Pro
Total Bytes: 12345678900
Hard Link Bytes: 1234567890

Index: 2
Name: Windows 11 Home
Description: Windows 11 Home
Total Bytes: 11223344556
Hard Link Bytes: 1122334455
"""
        editions = parse_wim_info(sample_output)
        assert len(editions) == 2
        assert editions[0] == (1, "Windows 11 Pro", 12345678900 - 1234567890)
        assert editions[1] == (2, "Windows 11 Home", 11223344556 - 1122334455)


class TestConfig:
    """Test configuration handling."""

    def test_default_config(self):
        cfg = DEFAULT_CONFIG
        assert cfg["theme"] == 0
        assert cfg["lang"] == ""
        assert cfg["verify"] == True

    def test_load_save_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = os.path.join(tmpdir, "config.json")

            # Mock the config path
            import yaku.config as config_module
            from pathlib import Path
            original_config_file = config_module.CONFIG_FILE
            original_config_dir = config_module.CONFIG_DIR
            config_module.CONFIG_FILE = Path(config_file)
            config_module.CONFIG_DIR = Path(tmpdir)

            try:
                # Save config
                test_cfg = {"theme": 2, "lang": "pt_BR", "verify": False}
                save_config(test_cfg)

                # Load config
                loaded = load_config()
                assert loaded["theme"] == 2
                assert loaded["lang"] == "pt_BR"
                assert loaded["verify"] == False
            finally:
                config_module.CONFIG_FILE = original_config_file
                config_module.CONFIG_DIR = original_config_dir


class TestI18n:
    """Test internationalization."""

    def test_set_language(self):
        # Test setting language
        set_language("pt_BR")
        assert get_language() == "pt_BR"

        set_language(None)
        # Should fall back to system default

    def test_translation_function(self):
        # _() should return the string itself when no translation
        assert _("Test string") == "Test string"


class TestDrive:
    """Test Drive dataclass."""

    def test_drive_properties(self):
        drive = Drive(
            device="/dev/sdb",
            size=16 * 1024**3,
            model="SanDisk Ultra",
            vendor="SanDisk",
            product="Ultra",
            serial="123456789",
            wwn="0x123456789",
            bus_type=BusType.USB,
            removable=True,
            ejectable=True,
            media_available=True,
            media_removable=True,
        )
        assert drive.size_human == "16 GiB"
        assert "sdb" in drive.display_name
        assert "SanDisk" in drive.display_name
        assert "16 GiB" in drive.display_name


if __name__ == "__main__":
    pytest.main([__file__, "-v"])