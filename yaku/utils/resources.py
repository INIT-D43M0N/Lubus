"""Runtime lookup for installed data files (GTK4 .ui definitions).

Resolution order:
1. ``YAKU_DATA_DIR`` (explicit override, e.g. for tests)
2. ``/app/share/yaku`` (Flatpak sandbox)
3. source-checkout ``data/`` — only when running from a checkout
   (i.e. the module is not imported from the interpreter's site-packages),
   so ``python3 -m yaku`` never picks up a stale system install
4. ``<sys.prefix>/share/yaku`` (system install, e.g. /usr)

The RPM installs package data into ``<prefix>/share/yaku``; Flatpak uses
``/app``. A source checkout keeps its own ``data/ui`` directory.
"""
import os
import sys
import sysconfig
from pathlib import Path

import yaku

__all__ = ["data_dir", "ui_file", "asset_file"]


def _running_from_source() -> bool:
    """True when yaku is imported from a checkout, not site-packages."""
    module_path = str(Path(yaku.__file__).resolve())
    site_dir = str(Path(sysconfig.get_paths()["purelib"]))
    return not module_path.startswith(site_dir)


def _search_dirs() -> list:
    """Candidate data directories, most specific first."""
    entries = []

    env_value = os.environ.get("YAKU_DATA_DIR")
    if env_value:
        entries.append(Path(env_value))

    source_data = Path(yaku.__file__).resolve().parents[1] / "data"
    installed_data = Path(sys.prefix) / "share/yaku"

    entries.append(Path("/app/share/yaku"))  # Flatpak

    if _running_from_source():
        entries.append(source_data)           # dev runs: checkout wins
    entries.append(installed_data)            # installed package
    if not _running_from_source():
        entries.append(source_data)           # fallback only

    return entries


def data_dir() -> Path:
    """Return the directory containing yaku data files (ui/, styles.css)."""
    searched = _search_dirs()
    for base in searched:
        if base.is_dir() and (base / "ui").is_dir():
            return base
    raise FileNotFoundError(
        "Yaku data directory not found. Searched: "
        + ", ".join(str(d) for d in searched)
    )


def ui_file(name: str) -> Path:
    """Return the absolute path of a .ui file by name (e.g. \"drive_page.ui\")."""
    for base in _search_dirs():
        candidate = base / "ui" / name
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"UI file '{name}' not found. Searched: "
        + ", ".join(str(d / 'ui') for d in _search_dirs())
    )


def asset_file(name: str) -> Path:
    """Return the absolute path of a shipped asset (e.g. \"discback.png\")."""
    for base in _search_dirs():
        candidate = base / "assets" / name
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"Asset '{name}' not found. Searched: "
        + ", ".join(str(d / 'assets') for d in _search_dirs())
    )
