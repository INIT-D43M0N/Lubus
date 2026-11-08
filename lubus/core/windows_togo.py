"""Windows To Go implementation for Lubus.

This is a simplified version. Full Windows To Go requires:
1. wimlib with NTFS-3G support (for apply to NTFS)
2. BCD store generation
3. UEFI bootloader installation
"""
import contextlib
import os
import shutil
import struct
import subprocess
import tempfile
import ctypes
import ctypes.util
import fcntl

from gi.repository import GLib

from ..utils.logging import get_logger
from ..udisks import (
    bus, resolve, unmount_all, mount_filesystem, unmount_filesystem,
    open_device_exclusive, objects, call, NO_OPTIONS, mount_iso_readonly,
    BASIC_DATA, BLOCK, FILESYSTEM, PARTITION,
)
from .writer import (
    StoppedException, WriteError, CHUNK_SIZE, SYNC_EVERY,
    release_stale_iso_mounts,
)
from .provisioning import write_windows_provisioning

log = get_logger(__name__)

# GPT partition type GUIDs
ESP_TYPE_GUID = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
BASIC_DATA_GUID = "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7"
ESP_SIZE = 512 * 1024 * 1024  # 512 MiB

# BLKSSZGET ioctl
BLKSSZGET = 0x1268

# wimlib constants (from wimlib.h)
EXTRACT_FLAG_NTFS = 1
MSG_EXTRACT_STREAMS = 4
STATUS_ABORT = 1
ERR_ABORTED_BY_PROGRESS = 76

PROGRESS_FUNC = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)


class _ExtractInfo(ctypes.Structure):
    _fields_ = [
        ("image", ctypes.c_uint32),
        ("extract_flags", ctypes.c_uint32),
        ("wimfile_name", ctypes.c_void_p),
        ("image_name", ctypes.c_void_p),
        ("target", ctypes.c_void_p),
        ("reserved", ctypes.c_void_p),
        ("total_bytes", ctypes.c_uint64),
        ("completed_bytes", ctypes.c_uint64),
    ]


class _NtfsDevice(ctypes.Structure):
    _fields_ = [
        ("d_ops", ctypes.c_void_p),
        ("d_state", ctypes.c_ulong),
        ("d_name", ctypes.c_char_p),
        ("d_private", ctypes.c_void_p),
    ]


NTFS_OPEN_FUNC = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(_NtfsDevice), ctypes.c_int)

# ntfs-3g device state flags
ND_OPEN = 1
ND_READONLY = 2
ND_BLOCK = 8

_libwim = None
_keep_refs = []


def _load_wimlib():
    """Load wimlib and hook ntfs-3g."""
    global _libwim
    if _libwim:
        return _libwim

    lib = ctypes.CDLL(ctypes.util.find_library("wim") or "libwim.so.15")
    lib.wimlib_open_wim.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
    lib.wimlib_register_progress_function.argtypes = [ctypes.c_void_p, PROGRESS_FUNC, ctypes.c_void_p]
    lib.wimlib_extract_image.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    lib.wimlib_free.argtypes = [ctypes.c_void_p]
    lib.wimlib_get_error_string.restype = ctypes.c_char_p

    # Hook ntfs-3g to accept /proc/self/fd/N paths
    with open("/proc/self/maps") as maps:
        ntfs_path = next((line.split()[-1] for line in maps if "/libntfs-3g.so" in line), None)

    if not ntfs_path:
        raise WriteError("wimlib is built without NTFS-3G support")

    _hook_ntfs_open(ntfs_path)
    _libwim = lib
    return lib


def _hook_ntfs_open(ntfs_path):
    """Hook ntfs-3g's open to accept udisks fds."""
    ops_ptr = ctypes.c_void_p.in_dll(ctypes.CDLL(ntfs_path), "ntfs_device_unix_io_ops")
    orig = NTFS_OPEN_FUNC(ops_ptr.value)
    libc = ctypes.CDLL(None)
    libc.malloc.restype = ctypes.c_void_p

    def open_fd(dev, flags):
        name = dev.contents.d_name.decode(errors="replace")
        if not name.startswith("/proc/self/fd/"):
            return orig(dev, flags)
        # Duplicate the fd for ntfs-3g
        priv = libc.malloc(ctypes.sizeof(ctypes.c_int))
        ctypes.c_int.from_address(priv).value = os.dup(int(name[14:]))
        dev.contents.d_private = priv
        state = ND_OPEN | ND_BLOCK
        if flags & os.O_ACCMODE != os.O_RDWR:
            state |= ND_READONLY
        dev.contents.d_state |= state
        return 0

    hook = NTFS_OPEN_FUNC(open_fd)
    _keep_refs.extend((orig, hook))
    ops_ptr.value = ctypes.cast(hook, ctypes.c_void_p).value


def _wim_error(lib, code):
    return lib.wimlib_get_error_string(code).decode(errors="replace")


def _apply_wim(image_path: str, index: int, fd: int, progress: callable, stopped: callable):
    """Apply WIM image to NTFS filesystem via wimlib.

    Returns the WIM's total byte count. The callback never reports 100%;
    the caller must emit the final ``progress(total, total)`` *after* its
    ``os.fsync()`` so the UI cannot claim completion while writeback is
    still pending.
    """
    lib = _load_wimlib()
    wim = ctypes.c_void_p()

    code = lib.wimlib_open_wim(image_path.encode(), 0, ctypes.byref(wim))
    if code:
        raise WriteError(_wim_error(lib, code))

    synced = [0]
    failed = []
    total_seen = [1]

    def progress_callback(msg, info, _ctx):
        if stopped():
            return STATUS_ABORT
        if msg == MSG_EXTRACT_STREAMS:
            e = _ExtractInfo.from_address(info)
            # Sync periodically to avoid huge page cache buildup
            if e.completed_bytes - synced[0] >= SYNC_EVERY:
                try:
                    os.fdatasync(fd)
                except OSError as err:
                    failed.append(err)
                    return STATUS_ABORT
                synced[0] = e.completed_bytes
            total = max(e.total_bytes, 1)
            total_seen[0] = total
            # Reserve 100% for the post-fsync event in the caller.
            done = min(e.completed_bytes, total - 1)
            progress(done, total)
        return 0

    callback = PROGRESS_FUNC(progress_callback)
    try:
        lib.wimlib_register_progress_function(wim, callback, None)
        code = lib.wimlib_extract_image(wim, index, f"/proc/self/fd/{fd}".encode(), EXTRACT_FLAG_NTFS)
    finally:
        lib.wimlib_free(wim)

    if failed:
        raise failed[0]
    if code == ERR_ABORTED_BY_PROGRESS:
        raise StoppedException()
    if code:
        raise WriteError(_wim_error(lib, code))
    return total_seen[0]


def _extract_boot_files(
    image_path: str,
    index: int,
    dest: str,
    boot_subdir: str = "Windows/Boot/EFI",
) -> str:
    """Extract Windows boot files from WIM.

    ``boot_subdir`` is ``Windows/Boot/EFI`` for UEFI or
    ``Windows/Boot/PCAT`` for BIOS.  Returns the directory containing the boot
    manager (``bootmgfw.efi`` or ``bootmgr``).
    """
    env = dict(os.environ, WIMLIB_IMAGEX_IGNORE_CASE="1")
    wimlib = shutil.which("wimlib-imagex") or "wimlib-imagex"

    result = subprocess.run([
        wimlib, "extract", image_path, str(index), f"/{boot_subdir}",
        f"--dest-dir={dest}"
    ], env=env, capture_output=True, check=False)

    if result.returncode != 0:
        raise WriteError("Failed to extract boot files from WIM")

    # wimlib may place the directory itself (EFI/PCAT) or its contents
    # directly under dest depending on version/path handling.
    candidates = [
        os.path.join(dest, os.path.basename(boot_subdir)),
        os.path.join(dest, boot_subdir),
        dest,
    ]
    required = "bootmgfw.efi" if boot_subdir.endswith("EFI") else "bootmgr"
    for path in candidates:
        if os.path.isfile(os.path.join(path, required)):
            return path
    raise WriteError(f"{required} not found in extracted boot files")


def _install_uefi_bootloader(boot_src: str, esp_mount: str, bcd_data: bytes):
    """Install UEFI bootloader and BCD to ESP."""
    ms_boot = os.path.join(esp_mount, "EFI", "Microsoft", "Boot")
    shutil.copytree(boot_src, ms_boot, dirs_exist_ok=True)

    # Also install as removable media path
    boot_dir = os.path.join(esp_mount, "EFI", "Boot")
    os.makedirs(boot_dir, exist_ok=True)
    shutil.copy2(
        os.path.join(boot_src, "bootmgfw.efi"),
        os.path.join(boot_dir, "bootx64.efi")
    )

    # Write BCD
    with open(os.path.join(ms_boot, "BCD"), "wb") as f:
        f.write(bcd_data)


def _build_bcd(part_guid: str, disk_guid: str) -> bytes:
    """Build BCD store for Windows To Go."""
    # This is a simplified BCD - real implementation uses bcd_logic.py
    # For now, return a minimal BCD
    # In production, use the bcd_logic.build_bcd function
    from .bcd import build_bcd
    return build_bcd(part_guid, disk_guid)


def _get_gpt_guids(fd: int, part_number: int) -> tuple:
    """Read GPT partition GUIDs."""
    # Read GPT header (LBA 1)
    header = os.pread(fd, 512, 512)
    if header[:8] != b"EFI PART":
        raise WriteError("Not a GPT disk")

    # Partition entry array location
    entries_lba = struct.unpack("<Q", header[72:80])[0]
    entry_size = struct.unpack("<I", header[80:84])[0]
    num_entries = struct.unpack("<I", header[84:88])[0]

    # Read partition entry
    entry_offset = entries_lba * 512 + (part_number - 1) * entry_size
    entry = os.pread(fd, entry_size, entry_offset)

    # Partition type GUID (offset 0), Unique partition GUID (offset 16)
    part_guid = struct.unpack("<16s", entry[16:32])[0]
    # Disk GUID is in header at offset 56
    disk_guid = struct.unpack("<16s", header[56:72])[0]

    def guid_to_str(g):
        return "".join(f"{b:02x}" for b in g)

    return guid_to_str(part_guid), guid_to_str(disk_guid)


def write_windows_togo(
    iso_path: str,
    device: str,
    image_index: int,
    progress: callable,
    stopped: callable,
    ask_speed: callable,
    provisioning: dict | None = None,
    windows_size: int = 0,
    iso_root: str | None = None,
    release_stale: bool = True,
) -> bool:
    """Write Windows To Go / portable Windows to a drive.

    ``windows_size`` may be 0 (use the rest of the disk) or a custom byte
    size. ``provisioning`` writes an unattend.xml for first-boot account
    creation. ``iso_root`` lets a multi-drive coordinator share one ISO
    mount between several concurrent writes.
    """
    conn = bus()
    dev_path = resolve(conn, device)

    # Unmount and wipe
    status("Preparing drive...")
    if release_stale and iso_root is None:
        release_stale_iso_mounts(iso_path)
    unmount_all(conn, dev_path)
    call(conn, dev_path, BLOCK, "Format", GLib.Variant("(sa{sv})", ("empty", {})), "()")

    # Speed test
    status("Measuring drive speed...")
    speed = measure_drive_speed(conn, dev_path)
    if speed and not ask_speed(*speed):
        return False

    # Create GPT with ESP + Windows partitions
    status("Creating partitions...")
    call(conn, dev_path, BLOCK, "Format", GLib.Variant("(sa{sv})", ("gpt", {})), "()")

    # ESP partition
    esp_part = create_partition_udisks(conn, dev_path, 1024*1024, ESP_SIZE, ESP_TYPE_GUID, "ESP", "vfat", "ESP")
    # Windows partition (custom size or the rest of the disk)
    win_part = create_partition_udisks(conn, dev_path, 1024*1024 + ESP_SIZE, windows_size, BASIC_DATA_GUID, "Windows", "ntfs", "Windows")

    # Get partition GUIDs
    fd = open_device_exclusive(conn, device, "r")
    try:
        objs = objects(conn)
        part_info = objs[win_part][PARTITION]
        part_guid, disk_guid = _get_gpt_guids(fd, part_info["Number"])
    finally:
        os.close(fd)

    iso_cm = (contextlib.nullcontext(iso_root) if iso_root
              else mount_iso_readonly(iso_path))
    try:
        with iso_cm as iso_root, tempfile.TemporaryDirectory(prefix="lubus-wtg-") as tmp:
            # Find WIM file
            wim_path = None
            for name in ("install.wim", "install.esd"):
                candidate = os.path.join(iso_root, "sources", name)
                if os.path.exists(candidate):
                    wim_path = candidate
                    break

            if not wim_path:
                raise WriteError("install.wim/install.esd not found in ISO")

            # Extract boot files
            boot_src = _extract_boot_files(wim_path, image_index, tmp)

            # Apply WIM to NTFS partition
            status("Deploying Windows (this takes a while)...")
            unmount_filesystem(conn, win_part)
            ntfs_fd = open_device_exclusive(conn, win_part, "rw")
            try:
                # Fix NTFS partition start sector
                fix_ntfs_start_sector(ntfs_fd, part_info["Offset"])
                wim_total = _apply_wim(wim_path, image_index, ntfs_fd, progress, stopped)
                os.fsync(ntfs_fd)
                # 100% only after the WIM writeback has been flushed.
                progress(wim_total, wim_total)
            finally:
                os.close(ntfs_fd)

            # Optional first-boot provisioning (unattend.xml + metadata).
            # A provisioning failure must not destroy an otherwise valid
            # Windows installation, so log and continue.
            if provisioning:
                try:
                    win_mount = mount_filesystem(conn, win_part)
                    try:
                        write_windows_provisioning(win_mount, provisioning)
                    finally:
                        unmount_filesystem(conn, win_part)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Windows provisioning failed: %s", exc)

            # Install UEFI bootloader
            status("Installing UEFI bootloader...")
            esp_mount = mount_filesystem(conn, esp_part)
            try:
                bcd = _build_bcd(part_guid, disk_guid)
                _install_uefi_bootloader(boot_src, esp_mount, bcd)
                # Tolerant unmount: never let a busy ESP volume abort the flash
                unmount_filesystem(conn, esp_part)
            except BaseException:
                unmount_filesystem(conn, esp_part)
                raise

    except StoppedException:
        return False

    status("Syncing...")
    return True


def create_partition_udisks(conn, device, offset, size, part_type, name, fs_type, label):
    """Create partition via UDisks."""
    from ..udisks import create_partition as udisks_create
    return udisks_create(conn, device, offset, size, part_type, name, fs_type, label, "gpt")


def fix_ntfs_start_sector(fd: int, partition_offset: int):
    """Write correct partition start sector to NTFS boot sector."""
    boot = bytearray(os.pread(fd, 512, 0))
    if boot[3:11] != b"NTFS    ":
        raise WriteError("No NTFS boot sector found")

    sector_size = struct.unpack("<H", boot[0x0b:0x0d])[0]
    total_sectors = struct.unpack("<Q", boot[0x28:0x30])[0]
    start_sector = partition_offset // sector_size

    struct.pack_into("<I", boot, 0x1c, start_sector)
    os.pwrite(fd, boot, 0)

    # Backup boot sector at end of volume
    backup_offset = total_sectors * sector_size
    if os.pread(fd, 8, backup_offset + 3) == b"NTFS    ":
        os.pwrite(fd, boot, backup_offset)


def measure_drive_speed(conn, device) -> tuple:
    """Measure drive sequential and random write speed."""
    try:
        fd = open_device_exclusive(conn, device, "rw", os.O_DIRECT | os.O_SYNC)
    except Exception:
        return None

    try:
        return _measure_fd(fd)
    except Exception:
        return None
    finally:
        os.close(fd)


def _measure_fd(fd: int) -> tuple:
    """Measure speed on an open fd."""
    import time

    # Sequential write (64 MiB)
    test_size = 64 * 1024 * 1024
    buf = os.urandom(min(1024 * 1024, test_size))

    start = time.monotonic()
    written = 0
    while written < test_size:
        n = os.write(fd, buf)
        if n <= 0:
            break
        written += n
    os.fsync(fd)
    seq_time = time.monotonic() - start
    seq_mbps = (written / 1024 / 1024) / seq_time if seq_time > 0 else 0

    # Random 4K write (1000 ops)
    ops = 1000
    block_size = 4096
    buf = os.urandom(block_size)

    start = time.monotonic()
    for _ in range(ops):
        offset = os.urandom(8)[0] * (test_size // 256)
        os.pwrite(fd, buf, offset)
    os.fsync(fd)
    rand_time = time.monotonic() - start
    iops = ops / rand_time if rand_time > 0 else 0

    return (seq_mbps, iops)


def status(msg: str):
    pass