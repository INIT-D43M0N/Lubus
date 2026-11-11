"""Device detection and management for Yaku."""
import os
from dataclasses import dataclass
from typing import List, Optional
from enum import Enum

from ..udisks import (
    list_removable_drives,
    list_all_drives,
    get_drive_info,
    get_drive_identity,
    get_device_size,
)
from ..utils.logging import get_logger

log = get_logger(__name__)


class BusType(Enum):
    """Bus type for a drive."""
    USB = "usb"
    SATA = "ata"
    NVME = "nvme"
    SD = "sd"
    UNKNOWN = "unknown"


@dataclass
class Drive:
    """Represents a removable drive."""
    device: str              # e.g., /dev/sdb
    size: int                # Size in bytes
    model: str               # Human-readable model string
    vendor: str              # Vendor name
    product: str             # Product name
    serial: str              # Serial number
    wwn: str                 # World Wide Name
    bus_type: BusType        # Connection bus type
    removable: bool          # Drive reports as removable
    ejectable: bool          # Can be ejected
    media_available: bool    # Media is present
    media_removable: bool    # Media is removable

    @property
    def size_human(self) -> str:
        """Human-readable size."""
        return format_size(self.size)

    @property
    def display_name(self) -> str:
        """Display name for UI."""
        parts = [self.device.removeprefix("/dev/")]
        if self.model:
            parts.append(self.model)
        parts.append(self.size_human)
        return "  ".join(p for p in parts if p)

    def matches_identity(self, identity: tuple) -> bool:
        """Check if this drive matches a previously saved identity."""
        if not identity:
            return False
        return (
            identity[1] == self.serial and
            identity[2] == self.wwn and
            identity[3] == self.size
        )


def _trim(value: float) -> str:
    """Format with one decimal, dropping a trailing '.0' (1.0 -> '1')."""
    return f"{value:.1f}".rstrip("0").rstrip(".")


def format_size(size: int) -> str:
    """Format bytes as human-readable string."""
    if size < 1024:
        return f"{size} B"
    if size >= 1024**4:
        return f"{_trim(size / 1024**4)} TiB"
    if size >= 1024**3:
        return f"{_trim(size / 1024**3)} GiB"
    if size >= 1024**2:
        return f"{size / 1024**2:.0f} MiB"
    return f"{size / 1024:.0f} KiB"


def detect_bus_type(connection_bus: str, device: str) -> BusType:
    """Detect bus type from udev info."""
    bus = connection_bus.lower()
    if bus == "usb":
        return BusType.USB
    if bus in ("ata", "sata"):
        return BusType.SATA
    if bus == "nvme":
        return BusType.NVME
    if bus == "sd" or device.startswith("/dev/mmcblk"):
        return BusType.SD
    # Fallback: check /sys/block
    try:
        dev_name = os.path.basename(device)
        uevent_path = f"/sys/block/{dev_name}/device/uevent"
        if os.path.exists(uevent_path):
            with open(uevent_path) as f:
                for line in f:
                    if line.startswith("DRIVER="):
                        driver = line.split("=", 1)[1].strip()
                        if "usb" in driver:
                            return BusType.USB
                        if "nvme" in driver:
                            return BusType.NVME
                        if "sd" in driver or "ata" in driver:
                            return BusType.SATA
    except OSError:
        pass
    return BusType.UNKNOWN


def get_all_drives(include_internal: bool = False) -> List[Drive]:
    """Get drives suitable for flashing.

    ``include_internal`` is for direct-install targets; the system disk and
    mounted disks are still excluded by :func:`list_all_drives`.
    """
    raw_drives = list_all_drives(include_internal=include_internal)
    drives = []

    for dev, size, model, vendor, product, serial in raw_drives:
        info = get_drive_info(dev)
        if not info:
            continue

        bus_type = detect_bus_type(info["connection_bus"], dev)

        drive = Drive(
            device=dev,
            size=size,
            model=model,
            vendor=info["vendor"],
            product=info["product"],
            serial=info["serial"],
            wwn=info["wwn"],
            bus_type=bus_type,
            removable=info["removable"],
            ejectable=info["ejectable"],
            media_available=info["media_available"],
            media_removable=info["media_removable"],
        )
        drives.append(drive)

    return drives


def get_drive_by_device(device: str) -> Optional[Drive]:
    """Get a drive by its device path."""
    for drive in get_all_drives():
        if drive.device == device:
            return drive
    return None


def refresh_drives_callback(callback):
    """Run drive detection in background and call callback with results."""
    import threading

    def worker():
        try:
            drives = get_all_drives()
            callback(drives, None)
        except Exception as e:
            callback(None, e)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()