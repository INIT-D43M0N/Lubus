"""Write operations for Lubus (DD, ISO extract, Windows)."""
import os
import errno
import mmap
import fcntl
import shutil
import subprocess
import tempfile
import struct
import contextlib
from typing import Callable, Optional, List
from pathlib import Path

from gi.repository import GLib, Gio

from ..utils.logging import get_logger
from ..udisks import (
    bus, resolve, unmount_all, open_device_exclusive, mount_filesystem,
    unmount_filesystem, make_volume, can_format_fstype, is_canceled_error,
    mount_iso_readonly, PortalPathError, UDisksError, NotAuthorizedError,
)
from .images import ImageType, PartitionScheme, TargetSystem, FileSystem
from .partitions import (
    PartitionLayout, PartitionSpec, PartitionType,
    get_windows_installer_layout, get_windows_togo_layout,
    get_linux_ishybrid_layout, get_generic_layout,
    calculate_partition_offsets, get_partition_type_guid, format_size,
)
from .icon_utils import get_or_create_windows_icon
from ..udisks.client import _get_objects, _get_bus, FILESYSTEM_IFACE, LOOP_IFACE

log = get_logger(__name__)


def release_stale_iso_mounts(iso_path: str = None) -> int:
    """Release any loop-device mounts left over from a previous analysis.

    The Step 2 analysis mounts the ISO (as loopN) to inspect it. If that
    analysis thread is still holding the mount when flashing starts, the
    later unmount fails with "target is busy". This releases them.

    Returns:
        Number of stale loop mounts released.
    """
    released = 0
    try:
        conn = _get_bus()
        objs = _get_objects(conn)
        real_iso = os.path.realpath(iso_path) if iso_path else None

        for obj, ifaces in objs.items():
            # Only loop devices have the Loop interface
            if LOOP_IFACE not in ifaces:
                continue
            if "/loop" not in obj:
                continue

            fs = ifaces.get(FILESYSTEM_IFACE)
            has_mount = fs and fs.get("MountPoints")

            # If we know the ISO path, only release mounts of that same file
            # (safer than nuking every loop device on the system)
            if real_iso:
                backing = ifaces.get(LOOP_IFACE, {}).get("BackingFile", "")
                backing_str = _bytes_safe(backing)
                if backing_str and os.path.realpath(backing_str) != real_iso:
                    continue

            try:
                if has_mount:
                    try:
                        conn.call_sync(
                            "org.freedesktop.UDisks2", obj, FILESYSTEM_IFACE,
                            "Unmount", GLib.Variant("(a{sv})", ({},)), None,
                            Gio.DBusCallFlags.NONE, -1, None)
                    except GLib.Error:
                        try:
                            force_opts = GLib.Variant("(a{sv})", ({"force": GLib.Variant("b", True)},))
                            conn.call_sync(
                                "org.freedesktop.UDisks2", obj, FILESYSTEM_IFACE,
                                "Unmount", force_opts, None,
                                Gio.DBusCallFlags.NONE, -1, None)
                        except GLib.Error:
                            pass
                conn.call_sync(
                    "org.freedesktop.UDisks2", obj, LOOP_IFACE,
                    "Delete", GLib.Variant("(a{sv})", ({},)), None,
                    Gio.DBusCallFlags.NONE, -1, None)
                released += 1
                log.info(f"Released stale loop mount: {obj}")
            except GLib.Error as e:
                log.warning(f"Could not release {obj}: {e}")
    except Exception as e:
        log.warning(f"release_stale_iso_mounts failed: {e}")

    return released


def _bytes_safe(value) -> str:
    """Convert GLib.Bytes/bytearray to a path string, tolerating plain str."""
    if isinstance(value, str):
        return value
    try:
        return bytes(value).rstrip(b"\0").decode(errors="replace")
    except Exception:
        return ""

# Constants
CHUNK_SIZE = 4 * 1024 * 1024  # 4 MiB
SYNC_EVERY = 64 * 1024 * 1024  # 64 MiB
TAIL_WIPE_SIZE = 1 * 1024 * 1024  # 1 MiB (for backup GPT)

# GRUB constants for MBR/BIOS boot
SECTOR_SIZE = 512
GRUB_DIRS = ("/usr/lib/grub/i386-pc", "/usr/lib/grub2/i386-pc", "/app/lib/grub/i386-pc")
GRUB_MODULES = ("biosdisk", "part_msdos", "ntfs", "ntldr")
GRUB_CFG = "ntldr /bootmgr\nboot\n"

# Windows installer: everything except the install image goes to the FAT32 ESP
WIN_SKIP_PATHS = ["sources/install.wim", "sources/install.esd"]
FAT32_MAX_FILE = (1 << 32) - 1  # FAT32 cannot store a file >= 4 GiB


class StoppedException(Exception):
    """Operation was stopped by user."""
    pass


class WriteError(Exception):
    """Write operation failed."""
    pass


class VerifyMismatchError(Exception):
    """Verification failed - data mismatch."""
    pass


ProgressCallback = Callable[[int, int], None]  # (done, total)
StatusCallback = Callable[[str], None]          # (message)
StoppedCallback = Callable[[], bool]            # () -> should_stop


class _ProgressMeter:
    """Cumulative byte progress for a multi-file/multi-phase write.

    ``total`` is the number of bytes for the *whole* operation, not for the
    current file. While copying, reports are capped at ``total - 1`` so the
    UI cannot display 100% while data is still in the page cache. The caller
    must invoke :meth:`finish` only after the final ``fsync``/``sync`` has
    completed; that is the only event that reports exactly 100%.
    """

    def __init__(self, total: int, callback: ProgressCallback):
        self.total = max(int(total), 1)
        self.done = 0
        self._callback = callback
        self._finished = False

    def add(self, count: int):
        """Account for ``count`` bytes written to the target."""
        self.done = min(self.total, self.done + max(0, int(count)))
        self._report()

    def finish(self):
        """Report 100% after the physical write has been flushed."""
        if self._finished:
            return
        self._finished = True
        self.done = self.total
        self._callback(self.total, self.total)

    def _report(self):
        done = self.done
        if not self._finished and done >= self.total:
            # Keep the UI below 100% until finish() is called after sync.
            done = max(0, self.total - 1)
        self._callback(done, self.total)


def _copy_file_with_progress(src: str, dst: str, advance, stopped: StoppedCallback):
    """Copy one file, calling ``advance(len(chunk))`` after each write.

    ``advance`` is a one-argument byte counter. It lets callers keep a single
    cumulative total across several files/phases instead of reporting each
    file as a separate 0–100% run.
    """
    unsynced = 0
    try:
        with open(src, "rb") as fi, open(dst, "wb") as fo:
            while chunk := fi.read(CHUNK_SIZE):
                if stopped():
                    raise StoppedException()
                fo.write(chunk)
                unsynced += len(chunk)
                if unsynced >= SYNC_EVERY:
                    fo.flush()
                    os.fdatasync(fo.fileno())
                    unsynced = 0
                advance(len(chunk))
            fo.flush()
            os.fdatasync(fo.fileno())
    except OSError as e:
        if e.errno == errno.ENOSPC:
            raise WriteError(
                f"Out of space on target drive while copying "
                f"'{os.path.basename(src)}' ({format_size(os.path.getsize(src))} needed)"
            ) from e
        raise


def _copy_tree_with_progress(
    src_root: str, dst_root: str, skip_paths: List[str],
    progress: ProgressCallback, stopped: StoppedCallback,
    on_bytes=None,
):
    """Copy a directory tree.

    With the default ``on_bytes=None`` this reports ``(done, total)`` for the
    tree. If ``on_bytes`` is supplied it is called with each chunk length and
    the caller owns the cumulative total (used by the Windows installer,
    which spans both the boot-file tree and install.wim).
    """
    # Compare case-insensitively: ISO9660/UDF names may differ in case
    # (e.g. "EFI" vs "efi", "SOURCES/INSTALL.WIM" vs "sources/install.wim")
    skip = {os.path.normpath(p).lower() for p in skip_paths}

    # Collect all files first
    files = []
    total = 0
    for root, _, names in os.walk(src_root):
        rel = os.path.relpath(root, src_root)
        if rel == ".":
            rel = ""
        dst_dir = os.path.join(dst_root, rel)
        os.makedirs(dst_dir, exist_ok=True)
        for name in names:
            src_path = os.path.normpath(os.path.join(rel, name))
            if src_path.lower() not in skip:
                full_src = os.path.join(src_root, rel, name)
                try:
                    size = os.path.getsize(full_src)
                except OSError:
                    size = 0
                files.append((full_src, os.path.join(dst_root, src_path), size))
                total += size

    total = total or 1
    done = 0

    def advance(n):
        nonlocal done
        done += n
        if on_bytes is not None:
            on_bytes(n)
        else:
            progress(done, total)

    for src, dst, _size in files:
        _copy_file_with_progress(src, dst, advance, stopped)


def _find_install_image(iso_root: str) -> Optional[str]:
    """Return the install.wim/esd inside an ISO tree (case-insensitive)."""
    try:
        entries = os.listdir(iso_root)
    except OSError:
        return None

    for entry in entries:
        if entry.lower() != "sources":
            continue
        sources = os.path.join(iso_root, entry)
        if not os.path.isdir(sources):
            continue
        try:
            names = os.listdir(sources)
        except OSError:
            continue
        for name in names:
            if name.lower() in ("install.wim", "install.esd"):
                candidate = os.path.join(sources, name)
                if os.path.isfile(candidate):
                    return candidate
    return None


def copy_image_dd(
    iso_path: str,
    device: str,
    progress: ProgressCallback,
    stopped: StoppedCallback,
    verify: bool = False,
    release_stale: bool = True,
) -> bool:
    """Write ISO image to device using dd-style block copy.

    Args:
        iso_path: Path to ISO image
        device: Target device (e.g., /dev/sdb)
        progress: Progress callback (done, total)
        stopped: Stop check callback
        verify: Verify after write

    Returns:
        True if completed, False if stopped.
    """
    conn = bus()
    dev_path = resolve(conn, device)

    # Unmount everything first
    status("Unmounting...")
    # Release stale ISO loop mounts from analysis before touching the device
    if release_stale:
        release_stale_iso_mounts(iso_path)
    unmount_all(conn, dev_path)

    # Open device with O_SYNC for honest progress
    fd = open_device_exclusive(conn, device, "rw" if verify else "w", os.O_SYNC)
    try:
        total = os.path.getsize(iso_path)
        device_size = os.fstat(fd).st_size or os.lseek(fd, 0, os.SEEK_END)

        if total > device_size:
            raise WriteError(
                f"Image too large for this drive: {format_size(total)} "
                f"> {format_size(device_size)}"
            )

        # Clear tail (backup GPT area)
        if device_size > total + TAIL_WIPE_SIZE:
            os.pwrite(fd, bytes(TAIL_WIPE_SIZE), device_size - TAIL_WIPE_SIZE)

        os.lseek(fd, 0, os.SEEK_SET)

        status("Copying image...")
        done = 0
        with open(iso_path, "rb") as src:
            while chunk := src.read(CHUNK_SIZE):
                if stopped():
                    return False
                view = memoryview(chunk)
                while view:
                    view = view[os.write(fd, view):]
                done += len(chunk)
                progress(done, total)

        status("Syncing...")
        os.fsync(fd)

        if verify:
            status("Verifying...")
            if not verify_image_dd(iso_path, fd, progress, stopped):
                return False

    finally:
        os.close(fd)

    return True


def verify_image_dd(
    iso_path: str,
    fd: int,
    progress: ProgressCallback,
    stopped: StoppedCallback,
) -> bool:
    """Verify device matches ISO by reading back (bypassing page cache)."""
    # Drop page cache or use O_DIRECT
    try:
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_DIRECT)
    except OSError:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)

    total = os.path.getsize(iso_path)
    buf = mmap.mmap(-1, CHUNK_SIZE)
    done = 0

    with open(iso_path, "rb") as src:
        while chunk := src.read(CHUNK_SIZE):
            if stopped():
                return False
            got = 0
            while got < len(chunk):
                n = os.preadv(fd, [memoryview(buf)[got:]], done + got)
                if n == 0:
                    break
                got += n
            if got < len(chunk) or buf[:len(chunk)] != chunk:
                raise VerifyMismatchError("Image verification failed: data mismatch")
            done += len(chunk)
            progress(done, total)

    return True


def _measure_iso_tree(iso_root: str, skip_paths: List[str]):
    """Measure the contents of a mounted ISO.

    Returns (fat32_bytes, skipped_bytes, oversized) where:
      - fat32_bytes: total size of files destined for the FAT32 ESP
      - skipped_bytes: total size of the skipped files (install.wim/esd)
      - oversized: [(rel_path, size)] files >= 4 GiB (impossible on FAT32)
    """
    skip = {os.path.normpath(p).lower() for p in skip_paths}
    fat32_bytes = 0
    skipped_bytes = 0
    oversized = []
    for root, _, names in os.walk(iso_root):
        rel = os.path.relpath(root, iso_root)
        rel = "" if rel == "." else rel
        for name in names:
            rel_path = name if not rel else os.path.join(rel, name)
            try:
                size = os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
            if os.path.normpath(rel_path).lower() in skip:
                skipped_bytes += size
            else:
                fat32_bytes += size
                if size > FAT32_MAX_FILE:
                    oversized.append((rel_path, size))
    return fat32_bytes, skipped_bytes, oversized


def _compute_esp_size(fat32_bytes: int, wim_bytes: int, drive_size: int) -> int:
    """Choose an ESP size that actually fits the ISO boot files.

    Raises WriteError if the drive cannot hold boot files + install image.
    """
    align = 1024 * 1024
    # Slack for FAT32 metadata/cluster rounding plus the icon/autorun files
    need = fat32_bytes + 64 * 1024 * 1024
    esp_size = max(512 * 1024 * 1024, -(-need // align) * align)

    # The NTFS partition must still hold install.wim + GPT backup structures
    max_esp = drive_size - wim_bytes - 256 * 1024 * 1024
    if esp_size > max_esp:
        raise WriteError(
            f"Drive too small for this image: boot files need "
            f"{format_size(esp_size)}, install image needs {format_size(wim_bytes)}, "
            f"drive has {format_size(drive_size)}"
        )
    return esp_size


def write_windows_installer(
    iso_path: str,
    device: str,
    scheme: PartitionScheme,
    label: str,
    progress: ProgressCallback,
    stopped: StoppedCallback,
    status_cb: Optional[StatusCallback] = None,
    release_stale: bool = True,
    iso_root: Optional[str] = None,
    windows_options: Optional[dict] = None,
) -> bool:
    """Write Windows installer media (file-based).

    Progress is reported against one cumulative total for the whole write
    (boot files + install.wim/esd). The final 100% event is emitted only in
    ``_ProgressMeter.finish()``, after all files have been unmounted and
    ``os.sync()`` has flushed the block-device caches.
    """
    emit_status = status_cb or (lambda _msg: None)
    conn = bus()
    dev_path = resolve(conn, device)

    emit_status("Preparing drive...")

    # Release any stale ISO loop mounts from the Step 2 analysis first,
    # otherwise unmounting later fails with "target is busy". A multi-drive
    # coordinator may already hold a shared mount and pass iso_root.
    if release_stale and iso_root is None:
        release_stale_iso_mounts(iso_path)

    def _iso_ctx():
        return (contextlib.nullcontext(iso_root) if iso_root
                else mount_iso_readonly(iso_path))

    unmount_all(conn, dev_path)

    drive_size = get_device_size(device)
    target = TargetSystem.UEFI if scheme == PartitionScheme.GPT else TargetSystem.BIOS
    fs = FileSystem.FAT32 if scheme == PartitionScheme.GPT else FileSystem.NTFS

    if scheme == PartitionScheme.GPT:
        # Mount the ISO first and measure it: the ESP holds the whole ISO
        # tree except install.wim (~2.5 GiB on Windows 11), so its size must
        # come from the actual contents - a fixed 512 MiB ESP dies with
        # ENOSPC partway through the copy.
        with _iso_ctx() as iso_root:
            fat32_bytes, _skipped, oversized = _measure_iso_tree(
                iso_root, WIN_SKIP_PATHS
            )
            if oversized:
                names = ", ".join(n for n, _ in oversized[:3])
                raise WriteError(
                    f"File(s) too large for the FAT32 boot partition: {names} "
                    f"(FAT32 limit is 4 GiB per file)"
                )

            wim_src = _find_install_image(iso_root)
            if wim_src is None:
                raise WriteError(
                    "No install.wim or install.esd found in the Windows image"
                )
            wim_size = os.path.getsize(wim_src)

            esp_size = _compute_esp_size(fat32_bytes, wim_size, drive_size)
            emit_status(f"Boot partition sized to {format_size(esp_size)}...")

            layout = get_windows_installer_layout(scheme, target, fs, label, drive_size, esp_size)
            partitions = calculate_partition_offsets(layout, drive_size)

            # Wipe and create partitions
            wipe_partition_table(conn, device, scheme.value)
            created_parts = _create_partitions(conn, device, partitions, scheme)

            # One meter for the complete write: boot files + install image.
            meter = _ProgressMeter(fat32_bytes + wim_size, progress)

            # Mount and copy files
            try:
                esp_part, win_part = created_parts[0], created_parts[1]
                esp_mount = mount_filesystem(conn, esp_part)
                win_mount = mount_filesystem(conn, win_part)

                # Copy all files except WIM to ESP (for UEFI boot)
                _copy_tree_with_progress(
                    iso_root, esp_mount,
                    WIN_SKIP_PATHS,
                    progress, stopped,
                    on_bytes=meter.add,
                )

                # Copy WIM to Windows partition (fresh NTFS: create sources/)
                os.makedirs(os.path.join(win_mount, "sources"), exist_ok=True)
                wim_dest = os.path.join(
                    win_mount, "sources", os.path.basename(wim_src).lower()
                )
                _copy_file_with_progress(wim_src, wim_dest, meter.add, stopped)

                if windows_options and windows_options.get("remove_defender"):
                    from .windows_debloat import (
                        get_wim_indexes, remove_defender_from_wim,
                    )
                    remove_defender_from_wim(
                        wim_dest, get_wim_indexes(wim_dest),
                        status_cb=emit_status,
                    )

                if windows_options:
                    from .provisioning import write_installer_autounattend
                    write_installer_autounattend(esp_mount, windows_options)
                    write_installer_autounattend(win_mount, windows_options)

                # Apply custom Windows icon to the Windows partition
                _apply_windows_icon(win_mount)

                unmount_filesystem(conn, esp_part)
                unmount_filesystem(conn, win_part)
            except StoppedException:
                return False
            except OSError as e:
                if e.errno == errno.ENOSPC:
                    raise WriteError(f"Out of space on target drive: {e}") from e
                raise

            # 100% is only reported after the write has been flushed.
            emit_status("Syncing...")
            os.sync()
            meter.finish()

        return True

    # MBR: single NTFS partition holds the whole ISO (no 4 GiB limit)
    if os.path.getsize(iso_path) > drive_size:
        raise WriteError(
            f"Image too large for this drive: {format_size(os.path.getsize(iso_path))} "
            f"> {format_size(drive_size)}"
        )

    # Validate and measure before wiping anything.
    with _iso_ctx() as iso_root:
        if _find_install_image(iso_root) is None:
            raise WriteError(
                "No install.wim or install.esd found in the Windows image"
            )
        total_bytes, _skipped, _oversized = _measure_iso_tree(iso_root, [])

    layout = get_windows_installer_layout(scheme, target, fs, label, drive_size)
    partitions = calculate_partition_offsets(layout, drive_size)

    wipe_partition_table(conn, device, scheme.value)
    created_parts = _create_partitions(conn, device, partitions, scheme)

    # One meter for the complete file copy.
    meter = _ProgressMeter(total_bytes, progress)

    try:
        win_part = created_parts[0]
        win_mount = mount_filesystem(conn, win_part)

        with _iso_ctx() as iso_root:
            _copy_tree_with_progress(
                iso_root, win_mount, [], progress, stopped,
                on_bytes=meter.add,
            )

        if windows_options:
            wim_dest = _find_install_image(win_mount)
            if windows_options.get("remove_defender") and wim_dest:
                from .windows_debloat import (
                    get_wim_indexes, remove_defender_from_wim,
                )
                remove_defender_from_wim(
                    wim_dest, get_wim_indexes(wim_dest),
                    status_cb=emit_status,
                )
            from .provisioning import write_installer_autounattend
            write_installer_autounattend(win_mount, windows_options)

        # Apply custom Windows icon to the Windows partition
        _apply_windows_icon(win_mount)

        unmount_filesystem(conn, win_part)
        emit_status("Installing BIOS bootloader (GRUB)...")
        install_grub_bios(conn, device)

    except StoppedException:
        return False
    except OSError as e:
        if e.errno == errno.ENOSPC:
            raise WriteError(f"Out of space on target drive: {e}") from e
        raise

    # 100% is only reported after the write has been flushed.
    emit_status("Syncing...")
    os.sync()
    meter.finish()
    return True


def _create_partitions(conn, device, partitions, scheme: PartitionScheme) -> List[str]:
    """Create partitions from calculated specs, returning their object paths."""
    created = []
    for part_spec in partitions:
        part_type_guid = get_partition_type_guid(part_spec.type, scheme)
        part = create_partition(
            conn, device,
            part_spec.offset, part_spec.size,
            part_type_guid, part_spec.name,
            part_spec.fs.value, part_spec.label,
            scheme.value,
        )
        created.append(part)
        if scheme == PartitionScheme.MBR:
            set_partition_active(conn, part)
    return created


def install_grub_bios(conn, device):
    """Install GRUB for BIOS/MBR boot."""
    # Build GRUB images
    mkimage = shutil.which("grub-mkimage") or shutil.which("grub2-mkimage")
    grub_dir = next((d for d in GRUB_DIRS if os.path.isfile(os.path.join(d, "boot.img"))), None)

    if not mkimage or not grub_dir:
        raise WriteError("grub-mkimage or GRUB modules not found")

    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = os.path.join(tmp, "early.cfg")
        with open(cfg_path, "w") as f:
            f.write(GRUB_CFG)

        core_path = os.path.join(tmp, "core.img")
        subprocess.run([
            mkimage, "-O", "i386-pc", "-d", grub_dir, "-o", core_path,
            "-c", cfg_path, "-p", "(,msdos1)/", *GRUB_MODULES
        ], check=True, capture_output=True)

        with open(os.path.join(grub_dir, "boot.img"), "rb") as f:
            boot_img = f.read()
        with open(core_path, "rb") as f:
            core_img = f.read()

    # Write to device
    fd = open_device_exclusive(conn, device, "rw")
    try:
        # Read original sector 0
        sector0 = os.pread(fd, SECTOR_SIZE, 0)

        # Build boot sector
        boot, core = build_bios_boot_code(boot_img, core_img, sector0)
        os.pwrite(fd, core, SECTOR_SIZE)
        os.pwrite(fd, boot, 0)
        os.fsync(fd)
    finally:
        os.close(fd)


def build_bios_boot_code(boot_img, core_img, sector0):
    """Build MBR boot sector with embedded GRUB core.img."""
    boot = bytearray(boot_img)
    boot[0x3:0x5a] = sector0[0x3:0x5a]      # BPB
    boot[0x1b8:0x1fe] = sector0[0x1b8:0x1fe] # Partition table
    boot[0x66:0x68] = b"\x90\x90"            # Drive check patch
    boot[0x64] = 0xFF                        # Force drive 0x80
    boot[0x5c:0x64] = (1).to_bytes(8, "little")  # Core.img starts at sector 1

    nsec = -(-len(core_img) // SECTOR_SIZE)
    if nsec > 2047:
        raise WriteError("core.img too large for 1 MiB gap")

    core = bytearray(core_img) + bytes(nsec * SECTOR_SIZE - len(core_img))

    # Patch blocklist in core.img
    first = SECTOR_SIZE - 12
    at = first
    while int.from_bytes(core[at + 8:at + 10], "little"):
        core[at:at + 12] = bytes(12)
        at -= 12

    end = first
    if nsec > 1:
        core[first:first + 12] = (
            (2).to_bytes(8, "little") +  # start sector
            (nsec - 1).to_bytes(2, "little") +  # sector count
            (0x800 + (SECTOR_SIZE >> 4)).to_bytes(2, "little")  # segment
        )
        end -= 12

    core[end:end + 12] = bytes(12)
    return bytes(boot), bytes(core)


# Windows To Go support (simplified - full implementation in windows_togo.py)
def write_windows_togo(
    iso_path: str,
    device: str,
    image_index: int,
    progress: ProgressCallback,
    stopped: StoppedCallback,
    ask_speed: Callable[[float, float], bool],
) -> bool:
    """Write Windows To Go (delegates to windows_togo module)."""
    from .windows_togo import write_windows_togo as wtg_write
    return wtg_write(iso_path, device, image_index, progress, stopped, ask_speed)


# Generic ISO extract (for Linux installers, etc.)
def extract_iso_files(
    iso_path: str,
    device: str,
    layout: PartitionLayout,
    progress: ProgressCallback,
    stopped: StoppedCallback,
    release_stale: bool = True,
    iso_root: Optional[str] = None,
) -> bool:
    """Extract ISO files to partitioned drive."""
    conn = bus()
    dev_path = resolve(conn, device)

    status("Preparing drive...")
    if release_stale and iso_root is None:
        release_stale_iso_mounts(iso_path)
    unmount_all(conn, dev_path)
    wipe_partition_table(conn, device, layout.scheme.value)

    # Create partitions
    partitions = calculate_partition_offsets(layout, get_device_size(device))
    created = []
    for part_spec in partitions:
        part_type_guid = get_partition_type_guid(part_spec.type, layout.scheme)
        part = create_partition(
            conn, device,
            part_spec.offset, part_spec.size,
            part_type_guid, part_spec.name,
            part_spec.fs.value, part_spec.label,
            layout.scheme.value,
        )
        created.append(part)

    # Mount and copy
    try:
        iso_cm = (contextlib.nullcontext(iso_root) if iso_root
                  else mount_iso_readonly(iso_path))
        with iso_cm as iso_root:
            if layout.scheme == PartitionScheme.GPT and layout.target == TargetSystem.UEFI:
                # ESP + data partition
                esp_mount = mount_filesystem(conn, created[0])
                data_mount = mount_filesystem(conn, created[1])

                # Copy EFI boot files to ESP
                efi_src = os.path.join(iso_root, "EFI")
                if os.path.exists(efi_src):
                    _copy_tree_with_progress(efi_src, os.path.join(esp_mount, "EFI"), [], progress, stopped)

                # Copy rest to data partition
                _copy_tree_with_progress(iso_root, data_mount, ["EFI"], progress, stopped)

                unmount_filesystem(conn, created[0])
                unmount_filesystem(conn, created[1])
            else:
                # Single partition
                data_mount = mount_filesystem(conn, created[0])
                _copy_tree_with_progress(iso_root, data_mount, [], progress, stopped)
                unmount_filesystem(conn, created[0])

                if layout.scheme == PartitionScheme.MBR and layout.target == TargetSystem.BIOS:
                    status("Installing BIOS bootloader (GRUB)...")
                    install_grub_bios(conn, device)

    except StoppedException:
        return False

    status("Syncing...")
    return True


def _apply_windows_icon(mount_point: str) -> bool:
    """
    Apply custom Windows icon to the mounted Windows partition.
    
    Creates an autorun.inf file and copies the .ico file to the root
    of the Windows partition to set a custom drive icon.
    
    Args:
        mount_point: Path to the mounted Windows partition
        
    Returns:
        True if icon was applied successfully, False otherwise
    """
    try:
        ico_path = get_or_create_windows_icon()
        if not ico_path or not ico_path.exists():
            log.info("No custom Windows icon available, skipping icon customization")
            return False
        
        # Copy the .ico file to the root of the Windows partition
        ico_dest = os.path.join(mount_point, "drive.ico")
        shutil.copy2(ico_path, ico_dest)
        log.info(f"Copied custom icon to {ico_dest}")
        
        # Create autorun.inf to set the custom icon
        autorun_content = f"[AutoRun]\nIcon=drive.ico\nLabel=Windows Install\n"
        autorun_path = os.path.join(mount_point, "autorun.inf")
        with open(autorun_path, "w") as f:
            f.write(autorun_content)
        log.info(f"Created autorun.inf at {autorun_path}")
        
        return True
    except Exception as e:
        log.warning(f"Failed to apply custom Windows icon: {e}")
        return False


# Helper functions
def get_device_size(device: str) -> int:
    """Get device size."""
    from ..udisks import get_device_size as udisks_get_size
    return udisks_get_size(device)


def create_partition(conn, device, offset, size, part_type, name, fs_type, label, scheme):
    """Create a partition via UDisks."""
    from ..udisks import create_partition as udisks_create_part
    return udisks_create_part(conn, device, offset, size, part_type, name, fs_type, label, scheme)


def wipe_partition_table(conn, device, scheme):
    """Wipe partition table via UDisks."""
    from ..udisks import wipe_partition_table as udisks_wipe
    udisks_wipe(conn, device, scheme)


def set_partition_active(conn, partition_path):
    """Set partition active flag."""
    from ..udisks import set_partition_active as udisks_set_active
    udisks_set_active(conn, partition_path)


def status(msg: str):
    """Default status callback (no-op)."""
    pass