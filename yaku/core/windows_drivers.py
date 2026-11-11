"""Stage storage-controller drivers on Windows deployment media.

Windows Setup's boot image only ships the in-box AHCI/NVMe drivers.  On
machines with Intel VMD/RST or AMD RAID enabled the target disk is invisible
to Setup, which then asks for a driver.  Slipstreaming a driver into
``boot.wim`` from Linux requires DISM's offline driver store, so instead we
copy the vendor's extracted driver folder next to the installation files.
The user can then use *Load driver -> Browse* in Setup without needing a
second USB stick.

The helpers here only copy files; they never modify the Windows image.
"""
from __future__ import annotations

import os
import shutil
from typing import Callable, Optional

from ..utils.logging import get_logger

log = get_logger(__name__)

DRIVERS_DIRNAME = "drivers"
README_NAME = "YAKU-DRIVERS.txt"

_README = """Yaku storage drivers
=====================

If Windows Setup reports that no device drivers were found, click
"Load driver", choose "Browse", and point it at this folder.  Pick the
controller that matches the machine.

Intel VMD / RST
  Intel RST VMD driver (f6vmdflpy-x64.zip) -> iaStorVD.inf
  Intel RST AHCI/RAID driver (f6flpy-x64.zip) -> iaStorAC.inf

AMD RAID
  AMD RAID driver -> rcraid.inf, rcbottom.inf, rccfg.inf

If Setup still cannot see the disk, reboot into the firmware settings and
disable Intel VMD / RST, or switch the SATA mode from RAID to AHCI.
"""


def find_driver_infs(root: str, limit: Optional[int] = None) -> list[str]:
    """Return every ``.inf`` file below *root* (case-insensitive).

    Pass *limit* to stop after the first matches, which keeps folder
    validation responsive when the user accidentally picks a huge tree.
    """
    if not root or not os.path.isdir(root):
        return []
    found: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name.lower().endswith(".inf"):
                found.append(os.path.join(dirpath, name))
                if limit is not None and len(found) >= limit:
                    return found
    return sorted(found)


def stage_drivers(
    source_dir: Optional[str],
    mount_root: str,
    status_cb: Optional[Callable[[str], None]] = None,
) -> Optional[str]:
    """Copy *source_dir* into ``<mount_root>/drivers``.

    Returns the destination directory on success, or ``None`` when no
    drivers were requested, the folder is unusable, or the copy failed.
    Driver staging is best-effort: media that is otherwise valid must not
    be reported as failed because an optional copy went wrong.
    """
    emit = status_cb or (lambda _msg: None)
    if not source_dir:
        return None

    source_dir = os.path.abspath(os.path.expanduser(source_dir))
    if not os.path.isdir(source_dir):
        log.warning("Driver folder does not exist: %s", source_dir)
        emit(f"Storage drivers not staged: folder not found ({source_dir})")
        return None

    infs = find_driver_infs(source_dir, limit=1)
    if not infs:
        log.warning("No .inf files found in driver folder: %s", source_dir)
        emit("Storage drivers not staged: no .inf files found")
        return None

    dest = os.path.join(mount_root, DRIVERS_DIRNAME)
    emit(f"Staging storage drivers to \\{DRIVERS_DIRNAME}...")
    try:
        shutil.copytree(
            source_dir, dest,
            dirs_exist_ok=True,
            ignore_dangling_symlinks=True,
        )
        readme = os.path.join(dest, README_NAME)
        with open(readme, "w", encoding="utf-8") as f:
            f.write(_README)
    except OSError as exc:
        log.warning("Could not stage storage drivers: %s", exc)
        emit(f"Storage drivers not staged: {exc}")
        return None

    log.info("Staged storage drivers from %s to %s", source_dir, dest)
    emit("Storage drivers staged; use Load driver in Windows Setup if needed.")
    return dest
