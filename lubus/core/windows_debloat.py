"""Optional Windows image debloat (Defender removal) for Lubus 5.0.

Two strategies are supported:

* **DISM helper** (preferred).  Set ``LUBUS_DISM`` to a wrapper that accepts
  ``<wim_path> <index1,index2,...>`` and mounts the image, removes the
  Defender packages, disables the offline services, and commits.  A wrapper
  can invoke DISM inside Wine/Windows PE if the host is Linux.

* **wimlib fallback**.  ``wimlib-imagex update`` deletes the well-known
  Defender payload directories/files from the selected image index(es).  It
  cannot unregister servicing packages or edit the offline registry; the
  generated autounattend.xml disables the services at first logon instead.
  This is a payload-level debloat, not full DISM servicing.

If neither strategy is available, the install fails with a clear error rather
than pretending Defender was removed.
"""
import os
import re
import shutil
import subprocess

from .writer import WriteError
from ..utils.logging import get_logger

log = get_logger(__name__)

# Directories are deleted recursively; files with --force.
_DEFENDER_DIRS = (
    "/Program Files/Windows Defender",
    "/Program Files (x86)/Windows Defender",
    "/ProgramData/Microsoft/Windows Defender",
    "/Windows/System32/Windows Defender",
    "/Windows/System32/SecurityHealth",
)
_DEFENDER_FILES = (
    "/Windows/System32/SecurityHealthService.exe",
    "/Windows/System32/SecurityHealthService.dll",
    "/Windows/System32/SecurityHealthSS.dll",
    "/Windows/System32/MpCmdRun.exe",
    "/Windows/System32/MpMpeng.dll",
    "/Windows/System32/MpSvc.dll",
    "/Windows/System32/mpuxsrv.dll",
)


def get_wim_indexes(wim_path: str) -> list[int]:
    """Return the 1-based indexes in a WIM/ESD."""
    wimlib = shutil.which("wimlib-imagex") or "wimlib-imagex"
    try:
        result = subprocess.run(
            [wimlib, "info", wim_path],
            capture_output=True, text=True, check=False, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return [1]
    indexes = {
        int(n) for n in re.findall(r"^Index:\s+(\d+)", result.stdout, re.M)
    }
    return sorted(indexes) or [1]


def _run_dism_helper(wim_path: str, indexes: list[int]) -> bool:
    exe = os.environ.get("LUBUS_DISM")
    if not exe:
        return False
    try:
        result = subprocess.run(
            [exe, wim_path, ",".join(str(i) for i in indexes)],
            capture_output=True, text=True, check=False, timeout=3600,
        )
        if result.returncode == 0:
            return True
        log.warning("DISM helper failed (%s): %s",
                    result.returncode, result.stderr.strip())
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("DISM helper could not run: %s", exc)
    return False


def _wimlib_delete_commands() -> str:
    commands = [f'delete --force --recursive "{path}"' for path in _DEFENDER_DIRS]
    commands += [f'delete --force "{path}"' for path in _DEFENDER_FILES]
    return "\n".join(commands) + "\n"


def remove_defender_from_wim(
    wim_path: str,
    indexes: list[int],
    status_cb=None,
) -> bool:
    """Remove the Defender payload from the given WIM image index(es).

    The preferred DISM helper performs full package/registry servicing.  The
    wimlib fallback removes the payload files; the autounattend first-logon
    commands disable ``WinDefend``, ``WdNisSvc`` and ``Sense``.
    """
    emit = status_cb or (lambda _m: None)
    emit("Removing Windows Defender from image...")

    if _run_dism_helper(wim_path, indexes):
        emit("Windows Defender removed via DISM helper.")
        return True

    wimlib = shutil.which("wimlib-imagex") or "wimlib-imagex"
    commands = _wimlib_delete_commands()
    for index in indexes:
        try:
            result = subprocess.run(
                [wimlib, "update", wim_path, str(index), "--force"],
                input=commands, text=True,
                capture_output=True, check=False, timeout=3600,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise WriteError(f"wimlib Defender removal failed: {exc}") from exc
        if result.returncode != 0:
            raise WriteError(
                "wimlib Defender removal failed for image "
                f"{index}: {result.stderr.strip() or result.stdout.strip()}"
            )
    emit("Defender payload removed; services will be disabled on first logon.")
    return True


def defender_first_logon_commands() -> list[str]:
    """Registry/service commands used by autounattend when DISM is absent."""
    return [
        "sc stop WinDefend",
        "sc config WinDefend start= disabled",
        'reg add "HKLM\\SYSTEM\\CurrentControlSet\\Services\\WinDefend" '
        "/v Start /t REG_DWORD /d 4 /f",
        "sc stop WdNisSvc",
        "sc config WdNisSvc start= disabled",
        'reg add "HKLM\\SYSTEM\\CurrentControlSet\\Services\\WdNisSvc" '
        "/v Start /t REG_DWORD /d 4 /f",
        "sc stop Sense",
        "sc config Sense start= disabled",
        'reg add "HKLM\\SYSTEM\\CurrentControlSet\\Services\\Sense" '
        "/v Start /t REG_DWORD /d 4 /f",
    ]
