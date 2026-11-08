"""Windows boot configuration for direct installs.

Creating a bootable Windows installation requires a valid BCD store.  The
project supports two strategies, in order of reliability:

1. **bcdboot** (preferred).  On a pure Linux host, ``bcdboot.exe`` is not
   executable; set ``LUBUS_BCDBOOT`` to a small wrapper (or a Wine/PE
   launcher) with this contract::

       wrapper <windows_mount> <esp_mount> <uefi|bios>

   The wrapper must create the BCD store and copy the boot manager files.

2. **BCD template**.  A known-good ``uefi.bcd`` / ``bios.bcd`` can be placed
   in ``<data>/bcd/`` (or supplied with ``LUBUS_BCD_TEMPLATE``).  This is a
   fallback for controlled environments where the template is known to match
   the target layout.

There is deliberately no silently-generated placeholder BCD: writing an
invalid boot store would produce a drive that looks successful but cannot
boot.  If neither strategy is available, the install fails with a clear
actionable error.
"""
import os
import shutil
import subprocess

from lubus.utils.logging import get_logger
from lubus.utils.resources import data_dir
from .writer import WriteError

log = get_logger(__name__)


def _bcdboot_command() -> str | None:
    env = os.environ.get("LUBUS_BCDBOOT")
    if env:
        return env
    return shutil.which("bcdboot")


def _run_bcdboot(win_mount: str, esp_mount: str | None, mode: str) -> bool:
    exe = _bcdboot_command()
    if not exe:
        return False
    command = [exe, win_mount, esp_mount or "", mode]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, check=False, timeout=180
        )
        if result.returncode == 0:
            return True
        log.warning("bcdboot helper failed (%s): %s",
                    result.returncode, result.stderr.strip())
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("bcdboot helper could not run: %s", exc)
    return False


def _template_for(mode: str) -> str | None:
    env = os.environ.get("LUBUS_BCD_TEMPLATE")
    candidates = []
    if env:
        candidates.append(env)
    candidates.append(str(data_dir() / "bcd" / f"{mode}.bcd"))
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def _write_template_bcd(dest: str, mode: str):
    template = _template_for(mode)
    if not template:
        raise WriteError(
            "Windows boot configuration needs bcdboot or a BCD template. "
            "Set LUBUS_BCDBOOT to a bcdboot wrapper, or place "
            f"bcd/{mode}.bcd in the Lubus data directory. "
            "See data/bcd/README.md for details."
        )
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copyfile(template, dest)


def configure_uefi_boot(win_mount: str, esp_mount: str, boot_src: str):
    """Install the UEFI Windows boot manager and BCD store."""
    ms_boot = os.path.join(esp_mount, "EFI", "Microsoft", "Boot")
    shutil.copytree(boot_src, ms_boot, dirs_exist_ok=True)

    # Removable-media fallback path used by many firmwares.
    fallback = os.path.join(esp_mount, "EFI", "Boot")
    os.makedirs(fallback, exist_ok=True)
    shutil.copy2(
        os.path.join(boot_src, "bootmgfw.efi"),
        os.path.join(fallback, "bootx64.efi"),
    )

    if _run_bcdboot(win_mount, esp_mount, "uefi"):
        return
    _write_template_bcd(os.path.join(ms_boot, "BCD"), "uefi")


def configure_bios_boot(win_mount: str, boot_src: str):
    """Install the BIOS boot manager and BCD store.

    The caller is responsible for installing GRUB or another MBR bootloader;
    this function only places ``bootmgr`` + ``\\Boot`` on the Windows volume.
    """
    # boot_src points at the extracted Windows/Boot/PCAT directory.
    shutil.copytree(boot_src, win_mount, dirs_exist_ok=True)

    if _run_bcdboot(win_mount, None, "bios"):
        return
    _write_template_bcd(os.path.join(win_mount, "Boot", "BCD"), "bios")


def configure_boot(
    boot_mode: str,
    win_mount: str,
    esp_mount: str | None,
    boot_src: str,
):
    """Configure UEFI or BIOS boot files for a direct Windows install."""
    if boot_mode == "uefi":
        if not esp_mount:
            raise WriteError("UEFI boot requires an EFI System Partition")
        configure_uefi_boot(win_mount, esp_mount, boot_src)
    else:
        configure_bios_boot(win_mount, boot_src)
