"""Regression tests for Windows installer media layout.

Windows 11 24H2+ (ConX) locates the installation media by checking for
``Sources\\SetupHost.exe`` and then expects ``install.wim``/``install.esd``
on that same volume. The old Lubus layout put the boot files on the FAT32
volume and only ``install.wim`` on the NTFS volume, so Setup showed
"Install driver to show hardware" even though the USB was fine.
"""
import os

import pytest

from lubus.core import writer
from lubus.core.images import PartitionScheme
from lubus.core.writer import (
    WIN_BOOT_SKIP_PATHS,
    WriteError,
    _compute_esp_size,
    _measure_iso_tree,
)


def _make_iso(root):
    sources = root / "sources"
    efi = root / "efi" / "boot"
    sources.mkdir(parents=True)
    efi.mkdir(parents=True)
    (sources / "install.wim").write_bytes(b"W" * 4096)
    (sources / "boot.wim").write_bytes(b"B" * 8192)
    (sources / "SetupHost.exe").write_bytes(b"H" * 512)
    (root / "setup.exe").write_bytes(b"S" * 256)
    (efi / "bootx64.efi").write_bytes(b"E" * 1024)


def test_boot_volume_excludes_setuphost_and_install_image(tmp_path):
    _make_iso(tmp_path)

    boot_bytes, skipped_bytes, oversized = _measure_iso_tree(
        str(tmp_path), WIN_BOOT_SKIP_PATHS
    )

    assert boot_bytes == 8192 + 256 + 1024
    assert skipped_bytes == 4096 + 512
    assert not oversized


def test_esp_size_accounts_for_full_media_tree(tmp_path):
    _make_iso(tmp_path)
    boot_bytes, skipped_bytes, _ = _measure_iso_tree(
        str(tmp_path), WIN_BOOT_SKIP_PATHS
    )

    # The NTFS volume holds the complete tree, including the files that also
    # go to the boot volume.
    esp = _compute_esp_size(boot_bytes, boot_bytes + skipped_bytes, 32 << 30)
    assert esp >= 512 << 20
    assert esp + boot_bytes + skipped_bytes + (256 << 20) <= 32 << 30

    with pytest.raises(WriteError, match="too small"):
        _compute_esp_size(700 << 20, 1100 << 20, 1 << 30)


def test_gpt_media_keeps_setuphost_and_install_image_together(
    tmp_path, monkeypatch
):
    iso = tmp_path / "iso"
    iso.mkdir()
    _make_iso(iso)
    esp_mount = tmp_path / "esp"
    win_mount = tmp_path / "win"
    esp_mount.mkdir()
    win_mount.mkdir()

    created = []

    def create_partition(_conn, _dev, _offset, size, *_args):
        created.append(size)
        return f"/obj/{len(created)}"

    def mount_filesystem(_conn, part):
        return str(esp_mount if part.endswith("1") else win_mount)

    class FakeISO:
        def __enter__(self):
            return str(iso)

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(writer, "bus", lambda: object())
    monkeypatch.setattr(writer, "resolve", lambda _conn, _dev: "/obj")
    monkeypatch.setattr(writer, "unmount_all", lambda _conn, _dev: None)
    monkeypatch.setattr(writer, "unmount_filesystem", lambda _conn, _p: None)
    monkeypatch.setattr(writer, "mount_filesystem", mount_filesystem)
    monkeypatch.setattr(writer, "create_partition", create_partition)
    monkeypatch.setattr(writer, "set_partition_active", lambda _c, _p: None)
    monkeypatch.setattr(writer, "wipe_partition_table", lambda _c, _d, _s: None)
    monkeypatch.setattr(writer, "get_device_size", lambda _dev: 32 << 30)
    monkeypatch.setattr(
        writer, "release_stale_iso_mounts", lambda _path=None: 0
    )
    monkeypatch.setattr(writer, "mount_iso_readonly", lambda _p: FakeISO())
    monkeypatch.setattr(writer, "_apply_windows_icon", lambda _m: False)
    monkeypatch.setattr(writer.os, "sync", lambda: None)

    progress = []
    ok = writer.write_windows_installer(
        "/tmp/fake.iso", "/dev/sdd", PartitionScheme.GPT, "WINUSB",
        lambda done, total: progress.append((done, total)),
        lambda: False,
    )

    assert ok is True
    assert (win_mount / "sources" / "install.wim").is_file()
    assert (win_mount / "sources" / "SetupHost.exe").is_file()
    assert (win_mount / "setup.exe").is_file()
    assert not (esp_mount / "sources" / "SetupHost.exe").exists()
    assert not (esp_mount / "sources" / "install.wim").exists()
    assert (esp_mount / "sources" / "boot.wim").is_file()

    boot_bytes = 8192 + 256 + 1024
    media_bytes = 4096 + 8192 + 512 + 256 + 1024
    assert progress[-1] == (boot_bytes + media_bytes, boot_bytes + media_bytes)
