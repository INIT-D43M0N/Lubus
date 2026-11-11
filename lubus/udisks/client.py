"""UDisks2 D-Bus client for Lubus.

All disk operations go through UDisks2 via D-Bus, which handles polkit
authentication automatically. No root process of our own is needed.
"""
import os
import re
import time
import subprocess
import contextlib
from typing import Optional, List, Tuple, Dict, Any

from gi.repository import Gio, GLib

from ..utils.logging import get_logger

log = get_logger(__name__)

# D-Bus constants
UDISKS = "org.freedesktop.UDisks2"
MANAGER_PATH = "/org/freedesktop/UDisks2/Manager"
MANAGER_IFACE = "org.freedesktop.UDisks2.Manager"
OBJECT_MANAGER_IFACE = "org.freedesktop.DBus.ObjectManager"
BLOCK_IFACE = "org.freedesktop.UDisks2.Block"
FILESYSTEM_IFACE = "org.freedesktop.UDisks2.Filesystem"
PARTITION_IFACE = "org.freedesktop.UDisks2.Partition"
PARTITION_TABLE_IFACE = "org.freedesktop.UDisks2.PartitionTable"
DRIVE_IFACE = "org.freedesktop.UDisks2.Drive"
LOOP_IFACE = "org.freedesktop.UDisks2.Loop"

# Only show sd* devices (USB, SATA) - no NVMe, no mmcblk (SD cards via USB readers show as sd*)
# This matches lsblk -I 8 behavior
DEVICE_RE = re.compile(r"^/dev/sd[a-z]+$")
WHOLE_DISK_RE = re.compile(
    r"^/dev/(?:sd[a-z]+|vd[a-z]+|nvme\d+n\d+|mmcblk\d+)$"
)

# Partition type GUIDs
ESP_TYPE_GUID = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"      # EFI System Partition
BASIC_DATA_GUID = "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7"     # Microsoft Basic Data
BASIC_DATA = BASIC_DATA_GUID                                    # Short alias
LINUX_FS_GUID = "0fc63daf-8483-4772-8e79-3d69d8477de4"       # Linux Filesystem

# MBR partition types
MBR_FAT32 = "0x0c"      # FAT32 LBA
MBR_NTFS = "0x07"       # NTFS/exFAT
MBR_LINUX = "0x83"      # Linux

NO_OPTIONS = GLib.Variant("(a{sv})", ({},))

# Timeout (ms) for unmount calls: a stuck device must not freeze the wizard.
UNMOUNT_TIMEOUT = 15_000

# Timeout (ms) for detaching a loop device.
LOOP_DELETE_TIMEOUT = 15_000

# /proc/mounts lines have at least "device mountpoint fstype".
PROC_MOUNTS_MIN_FIELDS = 2


class UDisksError(Exception):
    """Base exception for UDisks operations."""
    pass


class NotAuthorizedError(UDisksError):
    """User dismissed the polkit authentication dialog."""
    pass


class DeviceNotFoundError(UDisksError):
    """Device not known to UDisks."""
    pass


def _bytes(value) -> str:
    """Convert GLib.Bytes or string to string, stripping nulls."""
    if isinstance(value, str):
        return value.rstrip("\0")
    # Assume it's GLib.Bytes or bytes-like
    return bytes(value).rstrip(b"\0").decode(errors="replace")


def _get_bus() -> Gio.DBusConnection:
    """Get the system bus connection."""
    return Gio.bus_get_sync(Gio.BusType.SYSTEM, None)


def _call(
    conn: Gio.DBusConnection,
    object_path: str,
    interface: str,
    method: str,
    args: GLib.Variant,
    reply_signature: str,
    fds: Optional[Gio.UnixFDList] = None,
    timeout: int = GLib.MAXINT,
) -> Tuple[GLib.Variant, Optional[Gio.UnixFDList]]:
    """Make a D-Bus call with interactive authorization (polkit)."""
    try:
        return conn.call_with_unix_fd_list_sync(
            UDISKS,
            object_path,
            interface,
            method,
            args,
            GLib.VariantType(reply_signature),
            Gio.DBusCallFlags.ALLOW_INTERACTIVE_AUTHORIZATION,
            timeout,
            fds,
            None,
        )
    except GLib.Error as e:
        if "NotAuthorized" in e.message or "Dismissed" in e.message:
            raise NotAuthorizedError("Authentication canceled by user") from e
        raise UDisksError(f"D-Bus call failed: {e.message}") from e


# Public aliases (used by the windows_togo module)
call = _call


def _get_objects(conn: Gio.DBusConnection) -> Dict[str, Dict[str, Dict[str, GLib.Variant]]]:
    """Get all managed objects from UDisks."""
    reply, _ = _call(
        conn,
        "/org/freedesktop/UDisks2",
        OBJECT_MANAGER_IFACE,
        "GetManagedObjects",
        None,
        "(a{oa{sa{sv}}})",
    )
    return reply.unpack()[0]


# Public alias for _get_objects
objects = _get_objects


# =============================================================================
# Drive Enumeration
# =============================================================================

def _mounted_block_devices() -> set:
    """Return real paths of mounted block devices (best effort)."""
    mounted = set()
    try:
        with open("/proc/mounts", "r", encoding="utf-8") as f:
            for line in f:
                fields = line.split()
                if fields and fields[0].startswith("/dev/"):
                    mounted.add(os.path.realpath(fields[0]))
    except OSError:
        pass
    return mounted


def _system_disks() -> set:
    """Return whole-disk names backing /, /boot and /boot/efi."""
    names = set()
    for mountpoint in ("/", "/boot", "/boot/efi"):
        try:
            st = os.stat(mountpoint)
            link = os.path.realpath(
                f"/sys/dev/block/{os.major(st.st_dev)}:{os.minor(st.st_dev)}"
            )
        except OSError:
            continue
        parts = link.split(os.sep)
        if "block" in parts:
            idx = parts.index("block")
            if idx + 1 < len(parts):
                names.add(parts[idx + 1])
    return names


def list_all_drives(include_internal: bool = False) -> List[Tuple[str, int, str, str, str]]:
    """List whole disks suitable for flashing.

    By default only removable/USB drives are returned.  With
    ``include_internal=True`` secondary internal SATA/NVMe/SSD disks are
    included, but the system disk and any disk with a mounted partition are
    always excluded.
    """
    conn = _get_bus()
    objs = _get_objects(conn)
    drives = []
    mounted = _mounted_block_devices()
    system = _system_disks()

    for obj_path, ifaces in objs.items():
        block = ifaces.get(BLOCK_IFACE)
        if not block or PARTITION_IFACE in ifaces:
            continue  # Only whole disks, not partitions

        drive = objs.get(block.get("Drive", ""), {}).get(DRIVE_IFACE)
        dev = _bytes(block.get("Device", ""))
        size = block.get("Size", 0)

        if not drive or not size:
            continue

        removable = drive.get("Removable", False)
        connection_bus = drive.get("ConnectionBus", "")

        if include_internal:
            if not WHOLE_DISK_RE.match(dev):
                continue
            if os.path.basename(dev) in system:
                continue
            # Never offer a disk that currently has any mounted filesystem.
            if any(m == dev or m.startswith(dev) for m in mounted):
                continue
        else:
            if not DEVICE_RE.match(dev):
                continue
            if not removable and connection_bus != "usb":
                continue

        vendor = _bytes(drive.get("Vendor", ""))
        model = _bytes(drive.get("Model", ""))
        serial = _bytes(drive.get("Serial", ""))

        drives.append((dev, size, f"{vendor} {model}".strip(), vendor, model, serial))

    # Sort by device path (sda, sdb, sdc...)
    drives.sort(key=lambda x: x[0])
    return drives


def list_removable_drives() -> List[Tuple[str, int, str, str, str]]:
    """List removable/USB drives only."""
    return list_all_drives(include_internal=False)


def get_drive_info(device: str) -> Optional[Dict[str, Any]]:
    """Get detailed info for a specific drive."""
    conn = _get_bus()
    objs = _get_objects(conn)

    try:
        obj_path = resolve_device(conn, device)
        block = objs[obj_path][BLOCK_IFACE]
        drive = objs.get(block["Drive"], {}).get(DRIVE_IFACE, {})

        return {
            "device": device,
            "size": block["Size"],
            "model": _bytes(drive.get("Model", "")),
            "vendor": _bytes(drive.get("Vendor", "")),
            "product": _bytes(drive.get("Model", "")),  # Use Model as product
            "serial": _bytes(drive.get("Serial", "")),
            "wwn": _bytes(drive.get("WWN", "")),
            "removable": drive.get("Removable", False),
            "connection_bus": _bytes(drive.get("ConnectionBus", "")),
            "ejectable": drive.get("Ejectable", False),
            "media_available": drive.get("MediaAvailable", False),
            "media_removable": drive.get("MediaRemovable", False),
        }
    except (KeyError, LookupError):
        return None


def resolve_device(conn: Gio.DBusConnection, device: str) -> str:
    """Resolve a device path to its UDisks object path.

    Accepts either a device path (/dev/sdd) or an already-resolved
    object path (/org/freedesktop/UDisks2/block_devices/sdd).
    Object paths are returned as-is for idempotency.
    """
    # Already an object path - return as-is
    if device.startswith("/org/freedesktop/UDisks2/"):
        return device

    reply, _ = _call(
        conn,
        MANAGER_PATH,
        MANAGER_IFACE,
        "ResolveDevice",
        GLib.Variant("(a{sv}a{sv})", ({"path": GLib.Variant("s", device)}, {})),
        "(ao)",
    )
    paths = reply.unpack()[0]
    if not paths:
        raise DeviceNotFoundError(f"UDisks does not know {device}")
    return paths[0]


def device_node(conn: Gio.DBusConnection, object_path: str) -> str:
    """The device node of a UDisks object (e.g. /dev/sdb2).

    Returns an empty string when the object has no device node (yet).
    """
    try:
        block = _get_objects(conn).get(object_path, {}).get(BLOCK_IFACE) or {}
        return _bytes(block.get("Device", ""))
    except (UDisksError, GLib.Error, OSError):
        return ""


def get_drive_identity(device: str) -> Optional[Tuple]:
    """Get a unique identity for a drive to detect swaps.

    Returns (drive_object_path, serial, wwn, size) or None.
    """
    try:
        conn = _get_bus()
        objs = _get_objects(conn)
        obj_path = resolve_device(conn, device)
        block = objs[obj_path][BLOCK_IFACE]
        drive = objs.get(block["Drive"], {}).get(DRIVE_IFACE, {})

        return (
            _bytes(block["Drive"]),
            _bytes(drive.get("Serial", "")),
            _bytes(drive.get("WWN", "")),
            block["Size"],
        )
    except (GLib.Error, LookupError, KeyError):
        return None


def get_device_size(device: str) -> int:
    """Get device size in bytes."""
    try:
        conn = _get_bus()
        obj_path = resolve_device(conn, device)
        reply, _ = _call(
            conn,
            obj_path,
            "org.freedesktop.DBus.Properties",
            "Get",
            GLib.Variant("(ss)", (BLOCK_IFACE, "Size")),
            "(v)",
        )
        return reply.unpack()[0]
    except (GLib.Error, LookupError):
        return 0


def get_bus() -> Gio.DBusConnection:
    """Get the system bus connection."""
    return _get_bus()


# Alias for backward compatibility
bus = get_bus


# Alias for resolve_device
resolve = resolve_device


def make_volume(conn: Gio.DBusConnection, device: str, scheme: str, label: str) -> str:
    """Wipe the disk and put one partition across it, starting at 1 MiB.

    gpt gets FAT32, mbr gets NTFS on an active partition. Returns the partition.
    """
    gpt = scheme == "gpt"
    obj_path = resolve_device(conn, device)
    _call(conn, obj_path, BLOCK_IFACE, "Format",
          GLib.Variant("(sa{sv})", ("gpt" if gpt else "dos", {})), "()")
    options = {} if gpt else {"partition-type": GLib.Variant("s", "primary")}
    reply, _ = _call(conn, obj_path, PARTITION_TABLE_IFACE, "CreatePartitionAndFormat",
                    GLib.Variant("(ttssa{sv}sa{sv})", (
                        1 << 20, 0, BASIC_DATA if gpt else "0x07", "", options,
                        "vfat" if gpt else "ntfs", {"label": GLib.Variant("s", label)})),
                    "(o)")
    part = reply.unpack()[0]
    if not gpt:
        # some BIOSes boot a USB disk only with an active partition
        _call(conn, part, PARTITION_IFACE, "SetFlags", GLib.Variant("(ta{sv})", (0x80, {})), "()")
    return part


# =============================================================================
# Partition Operations
# =============================================================================

def wipe_partition_table(conn: Gio.DBusConnection, device: str, scheme: str = "gpt"):
    """Wipe the disk and create a new partition table.

    Args:
        conn: D-Bus connection
        device: Device path (e.g., /dev/sdb)
        scheme: "gpt" or "mbr" (dos)
    """
    obj_path = resolve_device(conn, device)
    table_type = "gpt" if scheme == "gpt" else "dos"
    _call(conn, obj_path, BLOCK_IFACE, "Format",
          GLib.Variant("(sa{sv})", (table_type, {})), "()")


def create_partition(
    conn: Gio.DBusConnection,
    device: str,
    offset: int,
    size: int,
    part_type: str,
    name: str,
    fs_type: str,
    label: str,
    scheme: str = "gpt",
) -> str:
    """Create and format a partition.

    Args:
        conn: D-Bus connection
        device: Device path (e.g., /dev/sdb)
        offset: Start offset in bytes
        size: Size in bytes (0 = rest of disk)
        part_type: Partition type GUID (GPT) or MBR type code
        name: Partition name/label
        fs_type: Filesystem type (vfat, ntfs, ext4, btrfs, exfat)
        label: Filesystem label
        scheme: "gpt" or "mbr"

    Returns:
        Object path of the created partition.
    """
    obj_path = resolve_device(conn, device)

    # Options for partition creation
    part_options = {}
    if scheme == "mbr":
        part_options["partition-type"] = GLib.Variant("s", "primary")

    # Filesystem options
    fs_options = {"label": GLib.Variant("s", label)}

    reply, _ = _call(
        conn,
        obj_path,
        PARTITION_TABLE_IFACE,
        "CreatePartitionAndFormat",
        GLib.Variant("(ttssa{sv}sa{sv})", (
            offset, size, part_type, name, part_options,
            fs_type, fs_options
        )),
        "(o)",
    )
    return reply.unpack()[0]


def create_partition_unformatted(
    conn: Gio.DBusConnection,
    device: str,
    offset: int,
    size: int,
    part_type: str,
    name: str,
    scheme: str = "gpt",
) -> str:
    """Create a partition without formatting it (used for the MSR)."""
    obj_path = resolve_device(conn, device)
    options = {}
    if scheme == "mbr":
        options["partition-type"] = GLib.Variant("s", "primary")
    reply, _ = _call(
        conn, obj_path, PARTITION_TABLE_IFACE, "CreatePartition",
        GLib.Variant("(ttssa{sv})", (offset, size, part_type, name, options)),
        "(o)",
    )
    return reply.unpack()[0]


def set_partition_active(conn: Gio.DBusConnection, partition_path: str):
    """Set partition as active/bootable (MBR only)."""
    _call(conn, partition_path, PARTITION_IFACE, "SetFlags",
          GLib.Variant("(ta{sv})", (0x80, {})), "()")  # 0x80 = active flag


def rescan_device(conn: Gio.DBusConnection, device: str):
    """Ask UDisks2 to re-read the partition table of a device.

    Some write modes build the partition table themselves (ChromeOS and
    Brunch need very specific layouts that UDisks2 cannot create). After
    such a table was written to the disk, the kernel still holds the old
    one until somebody asks it to look again - that is what this call
    does.
    """
    obj_path = resolve_device(conn, device)
    try:
        _call(conn, obj_path, BLOCK_IFACE, "Rescan",
              GLib.Variant("(a{sv})", ({},)), "()")
    except (UDisksError, GLib.Error) as exc:
        # A missing Rescan is not fatal: the table is already on the disk,
        # the system just needs a reboot to pick it up.
        log.warning("Could not rescan %s: %s", device, exc)


def delete_all_partitions(conn: Gio.DBusConnection, device: str):
    """Delete all partitions on a device."""
    obj_path = resolve_device(conn, device)
    # Get partition table
    objs = _get_objects(conn)
    table_obj = None
    for obj, ifaces in objs.items():
        if ifaces.get(PARTITION_IFACE, {}).get("Table") == obj_path:
            table_obj = obj
            break

    if table_obj:
        # Delete each partition
        for obj, ifaces in objs.items():
            part = ifaces.get(PARTITION_IFACE)
            if part and part.get("Table") == obj_path:
                _call(conn, obj, "org.freedesktop.DBus.ObjectManager", "Unmanaged", None, "()")


# =============================================================================
# Filesystem Operations
# =============================================================================

def can_format_fstype(fstype: str) -> bool:
    """Check if UDisks can format a filesystem type."""
    try:
        conn = _get_bus()
        reply, _ = _call(
            conn, MANAGER_PATH, MANAGER_IFACE, "CanFormat",
            GLib.Variant("(s)", (fstype,)), "((bs))"
        )
        return reply.unpack()[0][0]
    except GLib.Error:
        return False


def mount_filesystem(conn: Gio.DBusConnection, partition_path: str, readonly: bool = False) -> str:
    """Mount a filesystem and return mount point."""
    # Wait for filesystem to appear
    fs = _wait_for_fs(conn, partition_path)
    if fs and fs.get("MountPoints"):
        return _bytes(fs["MountPoints"][0])

    options = {"readonly": GLib.Variant("b", readonly)} if readonly else {}
    reply, _ = _call(
        conn, partition_path, FILESYSTEM_IFACE, "Mount",
        GLib.Variant("(a{sv})", (options,)), "(s)"
    )
    return reply.unpack()[0]


def _device_mount_points(conn: Gio.DBusConnection, object_path: str) -> List[str]:
    """Mount points of a block device.

    Combines the UDisks2 Filesystem interface with /proc/mounts, because
    UDisks2 does not always expose a Filesystem interface (for instance on
    loop devices holding a UDF filesystem).
    """
    points: List[str] = []
    device = ""
    try:
        ifaces = _get_objects(conn).get(object_path, {})
        fs = ifaces.get(FILESYSTEM_IFACE) or {}
        for mount_point in fs.get("MountPoints", []) or []:
            points.append(_bytes(mount_point))
        block = ifaces.get(BLOCK_IFACE) or {}
        device = _bytes(block.get("Device", ""))
    except (UDisksError, GLib.Error, OSError):
        device = ""

    if device:
        try:
            with open("/proc/mounts", encoding="utf-8") as handle:
                for line in handle:
                    fields = line.split()
                    if len(fields) >= PROC_MOUNTS_MIN_FIELDS and fields[0] == device:
                        points.append(fields[1])
        except OSError:
            pass

    unique: List[str] = []
    for point in points:
        if point and point not in unique:
            unique.append(point)
    return unique


def unmount_filesystem(conn: Gio.DBusConnection, partition_path: str):
    """Unmount a filesystem, tolerating busy/failing devices.

    Tries a normal unmount first, then a forced unmount, and finally
    gives up quietly so a stuck loop device can never abort a flash.
    """
    # Attempt 1: normal unmount
    try:
        _call(conn, partition_path, FILESYSTEM_IFACE, "Unmount", NO_OPTIONS, "()",
              timeout=UNMOUNT_TIMEOUT)
        return
    except (UDisksError, GLib.Error):
        pass

    # Attempt 2: forced unmount (UDisks2 supports the "force" option)
    try:
        force_opts = GLib.Variant("(a{sv})", ({"force": GLib.Variant("b", True)},))
        _call(conn, partition_path, FILESYSTEM_IFACE, "Unmount", force_opts, "()",
              timeout=UNMOUNT_TIMEOUT)
        return
    except (UDisksError, GLib.Error):
        pass

    # Attempt 3: unmount through the mount point. This also works when the
    # Filesystem interface is missing (UDF loop devices, for instance).
    for mount_point in _device_mount_points(conn, partition_path):
        for command in (["umount", mount_point], ["umount", "-l", mount_point]):
            try:
                result = subprocess.run(
                    command, capture_output=True, check=False, timeout=30)
                if result.returncode == 0:
                    return
            except (OSError, subprocess.SubprocessError):
                continue

    # Give up quietly - a busy volume must never abort the write itself
    log.warning(f"Could not unmount {partition_path} (busy); continuing anyway")


def _wait_for_fs(conn: Gio.DBusConnection, path: str, timeout: float = 10.0) -> Optional[Dict]:
    """Wait for filesystem interface to appear on object."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        objs = _get_objects(conn)
        fs = objs.get(path, {}).get(FILESYSTEM_IFACE)
        if fs is not None:
            return fs
        time.sleep(0.2)
    return None


# =============================================================================
# Loop Device (for ISO mounting)
# =============================================================================

class PortalPathError(UDisksError):
    """File is only accessible via document portal (Flatpak)."""
    pass


def _partition_children(conn: Gio.DBusConnection, parent_path: str) -> List[Tuple[int, str, str]]:
    """Return child partitions of a block device.

    Each entry is (size, object_path, filesystem_id). Only partitions that
    expose a Filesystem interface are returned, sorted with the ISO
    filesystem first and the largest partition next.
    """
    try:
        objs = _get_objects(conn)
    except (UDisksError, GLib.Error) as exc:
        log.debug(f"Could not list partitions of {parent_path}: {exc}")
        return []

    children = []
    for obj, ifaces in objs.items():
        part = ifaces.get(PARTITION_IFACE)
        if not part or part.get("Table") != parent_path:
            continue
        if FILESYSTEM_IFACE not in ifaces:
            continue
        block = ifaces.get(BLOCK_IFACE) or {}
        size = block.get("Size", 0)
        fs_id = _bytes(block.get("Id", ""))
        children.append((size, obj, fs_id))

    def _sort_key(entry):
        size, _obj, fs_id = entry
        # Prefer the ISO filesystem over the (usually tiny) EFI partition.
        preferred = 0 if fs_id.lower() in ("iso9660", "udf", "isohybrid") else 1
        return (preferred, -size)

    children.sort(key=_sort_key)
    return children


def _wait_partition_children(
    conn: Gio.DBusConnection, parent_path: str, timeout: float = 5.0
) -> List[Tuple[int, str, str]]:
    """Wait for child partitions to show up after a loop setup."""
    end = time.monotonic() + timeout
    while True:
        children = _partition_children(conn, parent_path)
        if children or time.monotonic() >= end:
            return children
        time.sleep(0.2)


def _mount_loop_filesystem(conn: Gio.DBusConnection, loop_path: str) -> Tuple[str, str]:
    """Mount the filesystem of a loop device.

    Hybrid images (Fedora, recent Ubuntu and others) carry a partition table:
    the ISO9660 filesystem then lives in a child partition while the loop
    node itself only exposes a PartitionTable interface. Mounting the loop
    node directly fails with "No such interface
    org.freedesktop.UDisks2.Filesystem", so the partitions are tried first.

    Returns (mount_point, mounted_object_path).
    """
    try:
        loop_ifaces = _get_objects(conn).get(loop_path, {})
    except (UDisksError, GLib.Error):
        loop_ifaces = {}

    if PARTITION_TABLE_IFACE in loop_ifaces:
        children = _wait_partition_children(conn, loop_path, timeout=5.0)
    else:
        children = _partition_children(conn, loop_path)

    errors = []
    for _size, child, _fs_id in children:
        try:
            return mount_filesystem(conn, child, readonly=True), child
        except (UDisksError, GLib.Error) as exc:
            errors.append(f"{child}: {exc}")

    # No usable partition: mount the loop device itself (plain ISO9660).
    try:
        return mount_filesystem(conn, loop_path, readonly=True), loop_path
    except (UDisksError, GLib.Error) as exc:
        if errors:
            raise UDisksError(
                "Could not mount the image: " + "; ".join(errors)
            ) from exc
        raise


@contextlib.contextmanager
def mount_iso_readonly(iso_path: str):
    """Mount an ISO read-only via loop device.

    Yields the mount point path.
    """
    conn = _get_bus()

    # Resolve portal paths (Flatpak)
    real_path = _resolve_portal_path(iso_path)
    if real_path != iso_path:
        iso_path = real_path

    # Check if still a portal path
    if _is_portal_path(iso_path):
        raise PortalPathError(f"Cannot access {iso_path} via portal")

    # Open file and create loop device
    fd = os.open(iso_path, os.O_RDONLY | os.O_CLOEXEC)
    fds = Gio.UnixFDList.new_from_array([fd])

    try:
        reply, _ = _call(
            conn, MANAGER_PATH, MANAGER_IFACE, "LoopSetup",
            GLib.Variant("(ha{sv})", (0, {"read-only": GLib.Variant("b", True)})),
            "(o)", fds
        )
        loop_path = reply.unpack()[0]

        mounted_obj = loop_path
        try:
            mount_point, mounted_obj = _mount_loop_filesystem(conn, loop_path)
            yield mount_point
        finally:
            unmount_filesystem(conn, mounted_obj)
            # Tolerate ANY failure here: the loop device may already be gone
            # (e.g. detached by a prior cleanup) and that must never abort a flash
            try:
                _call(conn, loop_path, LOOP_IFACE, "Delete", NO_OPTIONS, "()")
            except (UDisksError, GLib.Error):
                log.debug(f"Loop device {loop_path} already detached or busy; ignoring")
    finally:
        # The fd is owned by the UnixFDList; steal it so it is closed once.
        try:
            stolen = list(fds.steal_fds())
        except AttributeError:
            stolen = []
        for stolen_fd in stolen:
            try:
                os.close(stolen_fd)
            except OSError:
                pass


def _resolve_portal_path(path: str) -> str:
    """Resolve a document portal path to its host path."""
    real = os.path.realpath(path)
    portal_prefixes = (
        "/run/flatpak/doc/",
        f"/run/user/{os.getuid()}/doc/",
    )

    for prefix in portal_prefixes:
        if real.startswith(prefix):
            doc_id = real[len(prefix):].split("/", 1)[0]
            try:
                session_bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
                reply = session_bus.call_sync(
                    "org.freedesktop.portal.Documents",
                    "/org/freedesktop/portal/documents",
                    "org.freedesktop.portal.Documents",
                    "GetHostPaths",
                    GLib.Variant("(as)", ([doc_id],)),
                    GLib.VariantType("(a{say})"),
                    Gio.DBusCallFlags.NONE,
                    -1,
                    None,
                )
                host = _bytes(reply.unpack()[0][doc_id])
                if os.access(host, os.R_OK):
                    return host
            except (GLib.Error, KeyError):
                pass
    return path


def _is_portal_path(path: str) -> bool:
    """Check if a path is under the document portal."""
    real = os.path.realpath(path)
    portal_prefixes = (
        "/run/flatpak/doc/",
        f"/run/user/{os.getuid()}/doc/",
    )
    return any(real.startswith(p) for p in portal_prefixes)


# =============================================================================
# Device Access (for dd-style writes)
# =============================================================================

def open_device_exclusive(conn: Gio.DBusConnection, device: str, mode: str = "w", flags: int = 0, exclusive: bool = True) -> int:
    """Open a block device through UDisks2 and return a file descriptor.

    ``mode`` is the access mode: "r" (read only), "w" (write only) or "rw".
    UDisks2 takes the access mode in the first argument of
    ``Block.OpenDevice`` and refuses the O_RDONLY / O_WRONLY / O_RDWR bits in
    the "flags" option ("Using 'O_RDWR', 'O_RDONLY' and 'O_WRONLY' flags is
    not permitted. Use 'mode' argument instead."). That has been the case
    since UDisks2 2.7.3 (2017), so the access bits are never sent in the
    flags here.

    ``exclusive`` adds O_EXCL, so that nothing else has the device open.
    Pass ``exclusive=False`` when the device may legitimately still be in
    use (for instance while reading it back for verification).
    """
    if mode not in ("r", "w", "rw"):
        raise UDisksError(f"Invalid access mode: {mode!r}")

    obj = resolve_device(conn, device)
    extra_flags = os.O_CLOEXEC | flags
    if exclusive:
        extra_flags |= os.O_EXCL

    try:
        reply, fds = _call(
            conn, obj, BLOCK_IFACE, "OpenDevice",
            GLib.Variant("(sa{sv})", (
                mode, {"flags": GLib.Variant("i", extra_flags)})),
            "(h)",
        )
    except UDisksError as exc:
        # Block.OpenDevice was added in UDisks2 2.7.3. Older daemons only
        # have OpenForBackup / OpenForRestore, which cannot give us a
        # read-write descriptor for the write-and-verify flow.
        if "UnknownMethod" in str(exc) or "unknown method" in str(exc).lower():
            raise UDisksError(
                "This UDisks2 version does not implement Block.OpenDevice. "
                "UDisks2 2.7.3 or newer is required to write to a drive."
            ) from exc
        raise

    return fds.get(reply.unpack()[0])


def unmount_all(conn: Gio.DBusConnection, device_path: str):
    """Unmount all filesystems on a device and its partitions.

    Args:
        conn: D-Bus connection
        device_path: UDisks object path (e.g., /org/freedesktop/UDisks2/block_devices/sdd)
                     OR a device path (e.g., /dev/sdd). Object paths start with '/org/'.
    """
    objs = _get_objects(conn)

    # Accept both object paths and device paths
    if device_path.startswith("/org/freedesktop/UDisks2/"):
        target_obj = device_path
    else:
        target_obj = resolve_device(conn, device_path)

    for obj, ifaces in objs.items():
        part = ifaces.get(PARTITION_IFACE)
        if obj != target_obj and (not part or part.get("Table") != target_obj):
            continue
        fs = ifaces.get(FILESYSTEM_IFACE)
        if fs and fs.get("MountPoints"):
            # Tolerate any unmount failure here (busy, already gone, etc.)
            try:
                _call(conn, obj, FILESYSTEM_IFACE, "Unmount", NO_OPTIONS, "()")
            except (UDisksError, GLib.Error):
                # Retry once with force, then give up quietly
                try:
                    force_opts = GLib.Variant("(a{sv})", ({"force": GLib.Variant("b", True)},))
                    _call(conn, obj, FILESYSTEM_IFACE, "Unmount", force_opts, "()")
                except (UDisksError, GLib.Error):
                    log.warning(f"Could not unmount {obj} (busy); continuing anyway")


# =============================================================================
# Utility
# =============================================================================

def is_canceled_error(error: Exception) -> bool:
    """Check if an error is from user canceling polkit auth."""
    return isinstance(error, NotAuthorizedError)