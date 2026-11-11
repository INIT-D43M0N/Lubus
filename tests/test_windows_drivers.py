"""Tests for storage-driver staging on Windows deployment media."""
import os

from yaku.core.direct_install import InstallConfig
from yaku.core.windows_drivers import (
    DRIVERS_DIRNAME,
    README_NAME,
    find_driver_infs,
    stage_drivers,
)


def _make_driver_tree(root):
    driver = root / "x64"
    driver.mkdir(parents=True)
    (driver / "iaStorVD.inf").write_text("[Version]\n", encoding="utf-8")
    (driver / "iaStorVD.sys").write_text("binary", encoding="utf-8")
    (driver / "iaStorVD.cat").write_text("catalog", encoding="utf-8")
    (root / "release-notes.txt").write_text("vendor", encoding="utf-8")
    return driver


def test_find_driver_infs_is_recursive_and_case_insensitive(tmp_path):
    driver = _make_driver_tree(tmp_path)
    (driver / "OTHER.INF").write_text("[Version]\n", encoding="utf-8")
    (driver / "notes.txt").write_text("not a driver", encoding="utf-8")

    found = find_driver_infs(str(tmp_path))

    assert sorted(os.path.basename(p) for p in found) == ["OTHER.INF", "iaStorVD.inf"]


def test_find_driver_infs_limit_stops_early(tmp_path):
    driver = _make_driver_tree(tmp_path)
    (driver / "OTHER.INF").write_text("[Version]\n", encoding="utf-8")

    assert len(find_driver_infs(str(tmp_path), limit=1)) == 1
    assert len(find_driver_infs(str(tmp_path))) == 2


def test_find_driver_infs_rejects_missing_or_non_directory(tmp_path):
    assert find_driver_infs("") == []
    assert find_driver_infs(str(tmp_path / "nope")) == []

    plain = tmp_path / "plain.txt"
    plain.write_text("x", encoding="utf-8")
    assert find_driver_infs(str(plain)) == []


def test_stage_drivers_copies_tree_and_writes_readme(tmp_path):
    source = tmp_path / "F6"
    _make_driver_tree(source)
    mount = tmp_path / "usb"
    mount.mkdir()
    messages = []

    dest = stage_drivers(str(source), str(mount), messages.append)

    assert dest == str(mount / DRIVERS_DIRNAME)
    assert (mount / DRIVERS_DIRNAME / "x64" / "iaStorVD.inf").is_file()
    assert (mount / DRIVERS_DIRNAME / "x64" / "iaStorVD.sys").is_file()
    assert (mount / DRIVERS_DIRNAME / README_NAME).is_file()
    assert "iaStorVD.inf" in (mount / DRIVERS_DIRNAME / README_NAME).read_text(
        encoding="utf-8"
    )
    assert any("Staging" in message for message in messages)
    assert any("staged" in message for message in messages)


def test_stage_drivers_noop_without_source(tmp_path):
    mount = tmp_path / "usb"
    mount.mkdir()

    assert stage_drivers(None, str(mount)) is None
    assert stage_drivers("", str(mount)) is None
    assert not (mount / DRIVERS_DIRNAME).exists()


def test_stage_drivers_rejects_folder_without_inf(tmp_path):
    source = tmp_path / "not-drivers"
    source.mkdir()
    (source / "readme.txt").write_text("no driver here", encoding="utf-8")
    mount = tmp_path / "usb"
    mount.mkdir()
    messages = []

    assert stage_drivers(str(source), str(mount), messages.append) is None
    assert not (mount / DRIVERS_DIRNAME).exists()
    assert any("no .inf" in message for message in messages)


def test_stage_drivers_rejects_missing_folder(tmp_path):
    mount = tmp_path / "usb"
    mount.mkdir()
    messages = []

    assert stage_drivers(str(tmp_path / "gone"), str(mount), messages.append) is None
    assert not (mount / DRIVERS_DIRNAME).exists()
    assert any("not found" in message for message in messages)


def test_install_config_serializes_drivers_path():
    config = InstallConfig(drivers_path="/home/user/F6")
    assert config.to_dict()["drivers_path"] == "/home/user/F6"
    assert InstallConfig().drivers_path == ""
