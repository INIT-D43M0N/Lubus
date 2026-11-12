"""Tests for the packaging helpers and the version numbers."""
import importlib.util
import os
import re
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def _load_release_notes():
    path = os.path.join(ROOT, "packaging", "release-notes.py")
    spec = importlib.util.spec_from_file_location("release_notes", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestVersion:
    """Every file that carries the version has to carry the same one."""

    def _versions(self):
        return {
            "meson.build": re.search(
                r"version:\s*'([^']+)'", _read("meson.build")).group(1),
            "pyproject.toml": re.search(
                r'^version\s*=\s*"([^"]+)"', _read("pyproject.toml"),
                re.M).group(1),
            "yaku/__init__.py": re.search(
                r'__version__\s*=\s*"([^"]+)"', _read("yaku", "__init__.py"),
            ).group(1),
            "yaku/main.py": re.search(
                r'print\("Yaku ([^"]+)"\)', _read("yaku", "main.py")).group(1),
            "packaging/yaku.spec": re.search(
                r"^Version:\s*(\S+)", _read("packaging", "yaku.spec"),
                re.M).group(1),
            "packaging/build-rpm.sh": re.search(
                r'^VERSION="([^"]+)"', _read("packaging", "build-rpm.sh"),
                re.M).group(1),
            "packaging/build-deb.sh": re.search(
                r'^VERSION="([^"]+)"', _read("packaging", "build-deb.sh"),
                re.M).group(1),
        }

    def test_all_files_agree(self):
        versions = self._versions()
        assert len(set(versions.values())) == 1, versions

    def test_version_is_a_release_number(self):
        version = self._versions()["meson.build"]
        assert re.fullmatch(r"\d+\.\d+\.\d+", version), version

    def test_changelog_has_a_section(self):
        version = self._versions()["meson.build"]
        assert f"## [{version}]" in _read("CHANGELOG.md")

    def test_metainfo_has_a_release(self):
        version = self._versions()["meson.build"]
        metainfo = _read("data", "com.github.init_d43m0n.yaku.metainfo.xml.in")
        assert f'<release version="{version}"' in metainfo

    def test_app_version_matches_the_package(self):
        out = subprocess.run(
            [sys.executable, "-m", "yaku.main", "--version"],
            capture_output=True, text=True, cwd=ROOT,
        )
        version = self._versions()["meson.build"]
        assert out.stdout.strip() == f"Yaku {version}", out.stdout


class TestReleaseNotes:
    """The notes of a release come from the changelog."""

    def test_extracts_the_section_of_the_version(self, tmp_path):
        module = _load_release_notes()
        changelog = tmp_path / "CHANGELOG.md"
        changelog.write_text(
            "## [2.0.0] - 2026-01-02\n\n### Added\n- Something new\n\n"
            "## [1.0.0] - 2026-01-01\n\n### Added\n- Something older\n",
            encoding="utf-8",
        )
        notes = module.extract("2.0.0", str(changelog))
        assert "Something new" in notes
        assert "Something older" not in notes

    def test_unknown_version_has_no_notes(self):
        module = _load_release_notes()
        assert module.extract("9.9.9", os.path.join(ROOT, "CHANGELOG.md")) == ""

    def test_script_prints_a_header(self):
        result = subprocess.run(
            [sys.executable, os.path.join(ROOT, "packaging", "release-notes.py"),
             "v6.1.2"],
            capture_output=True, text=True, cwd=ROOT,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.startswith("## Yaku 6.1.2")


class TestBuildScripts:
    """The packaging scripts must not use a pattern that matches too much."""

    def test_rpm_script_filters_the_source_rpm_by_name(self):
        script = _read("packaging", "build-rpm.sh")
        assert "! -name \"*.src.rpm\"" in script
        # A dot in a grep pattern matches any character, so "src.rpm" would
        # also discard every file below a path like /src/rpmbuild.
        assert "grep -v src.rpm" not in script

    @pytest.mark.parametrize("script", ["build-rpm.sh", "build-deb.sh"])
    def test_scripts_are_executable(self, script):
        path = os.path.join(ROOT, "packaging", script)
        assert os.access(path, os.X_OK), path
