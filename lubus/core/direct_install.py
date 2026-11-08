"""Direct drive installation / portable mode for Lubus 5.0.

This module turns a bootable image into an installed, portable system on a
target disk instead of a read-only live USB.

Supported adapters
------------------
* Windows: delegates to the Windows To Go writer (WIM apply to NTFS, UEFI
  bootloader, BCD) and additionally writes ``unattend.xml`` so the first
  boot creates the requested local account.
* Linux: deploys the ISO payload to a root partition, installs/uses the
  image's EFI or GRUB bootloader, and writes a cloud-init seed plus
  ``lubus-install.json`` for first-boot account provisioning.  This is a
  portable deployment path that works with live/portable Linux images; full
  distro installers can plug in a dedicated adapter through
  :func:`install_to_drive`.

Automatic and custom partitioning
---------------------------------
* Automatic: ESP (UEFI) + root/Windows partition using the rest of the disk.
* Custom: the user chooses the root/Windows size, swap-file size and
  filesystem.  Sizes are validated against the target disk before writing.

The actual block-device work is intentionally kept in this module so the
wizard and the writer backends stay independent.
"""
import contextlib
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from typing import Optional

from .images import (
    FileSystem, ImageType, PartitionScheme, TargetSystem,
)
from .partitions import (
    PartitionLayout, PartitionSpec, PartitionType,
    calculate_partition_offsets, format_size, get_partition_type_guid,
)
from .provisioning import write_linux_provisioning
from .writer import (
    ProgressCallback, StatusCallback, StoppedCallback,
    StoppedException, WriteError, _ProgressMeter, _copy_tree_with_progress,
    _measure_iso_tree, install_grub_bios, release_stale_iso_mounts,
)
from ..udisks import (
    bus, resolve, unmount_all, mount_filesystem, unmount_filesystem,
    mount_iso_readonly, create_partition, wipe_partition_table,
    set_partition_active, get_device_size,
)

GiB = 1024 ** 3
ESP_SIZE = 512 * 1024 * 1024  # 512 MiB

_FS_MAP = {
    "ext4": FileSystem.EXT4,
    "btrfs": FileSystem.BTRFS,
}


@dataclass
class InstallConfig:
    """User choices for direct installation (also used by the summary UI)."""

    mode: str = "live"                 # "live" | "direct"
    partition_mode: str = "auto"       # "auto" | "custom"
    username: str = "lubus"
    password: str = ""
    full_name: str = ""
    hostname: str = "lubus-pc"
    timezone: str = "UTC"
    filesystem: str = "ext4"
    root_gib: int = 32
    swap_gib: int = 4
    edition_index: int = 1
    boot_mode: str = "uefi"          # uefi | bios (Windows direct)
    esp_mib: int = 512               # Windows UEFI ESP size
    language: str = "en-US"
    keyboard: str = "0409:00000409"
    bypass_win11: bool = False
    skip_oobe: bool = True
    remove_defender: bool = False
    accept_eula: bool = True

    @property
    def is_direct(self) -> bool:
        return self.mode == "direct"

    @property
    def is_custom(self) -> bool:
        return self.partition_mode == "custom"

    def to_dict(self) -> dict:
        return asdict(self)


def _fs_enum(config: InstallConfig) -> FileSystem:
    return _FS_MAP.get(config.filesystem, FileSystem.EXT4)


def validate_install_config(
    config: InstallConfig,
    drive_size: int,
    image_type: ImageType | None = None,
) -> list[str]:
    """Return human-readable validation errors for a direct install."""
    if not config.is_direct:
        return []

    errors = []
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", config.username or ""):
        errors.append("Username must start with a letter/underscore and contain only a-z, 0-9, _ or -.")
    if not config.password:
        errors.append("A password is required for direct installation.")
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", config.hostname or ""):
        errors.append("Hostname is not valid.")
    if not config.timezone:
        errors.append("Timezone is required.")
    if image_type in (ImageType.WINDOWS_INSTALLER, ImageType.WINDOWS_TO_GO):
        if config.boot_mode not in ("uefi", "bios"):
            errors.append("Windows boot type must be UEFI or Legacy BIOS.")
        if config.boot_mode == "uefi" and not (100 <= config.esp_mib <= 2048):
            errors.append("EFI System Partition size must be 100-2048 MiB.")
    else:
        if config.filesystem not in _FS_MAP:
            errors.append("Filesystem must be ext4 or btrfs.")
        if config.swap_gib < 0:
            errors.append("Swap size cannot be negative.")

    if config.is_custom:
        minimum = 20 if image_type == ImageType.WINDOWS_INSTALLER else 8
        if config.root_gib < minimum:
            errors.append(f"Custom root/Windows size must be at least {minimum} GiB.")
        if image_type in (ImageType.WINDOWS_INSTALLER, ImageType.WINDOWS_TO_GO):
            needed = (config.root_gib + 1) * GiB
            if config.boot_mode == "uefi":
                needed += config.esp_mib * 1024**2 + 16 * 1024**2
        else:
            if config.swap_gib > max(0, config.root_gib - 4):
                errors.append("Swap file must be smaller than the root partition.")
            needed = (config.root_gib + 1) * GiB
        if needed > drive_size:
            errors.append(
                f"Custom layout needs {format_size(needed)} but the drive has "
                f"{format_size(drive_size)}."
            )
    return errors


def build_linux_layout(
    config: InstallConfig,
    scheme: PartitionScheme,
    target: TargetSystem,
    label: str,
    drive_size: int,
) -> PartitionLayout:
    """Build the partition layout for a Linux portable install."""
    partitions = []

    if scheme == PartitionScheme.GPT and target == TargetSystem.UEFI:
        partitions.append(PartitionSpec(
            type=PartitionType.EFI_SYSTEM,
            size=ESP_SIZE,
            fs=FileSystem.FAT32,
            label="ESP",
            name="EFI System Partition",
            flags=["boot", "esp"],
        ))

    root_size = config.root_gib * GiB if config.is_custom else 0
    if root_size:
        reserved = ESP_SIZE if scheme == PartitionScheme.GPT and target == TargetSystem.UEFI else 0
        if root_size + reserved + 256 * 1024**2 > drive_size:
            raise WriteError(
                f"Root partition ({format_size(root_size)}) does not fit on "
                f"the {format_size(drive_size)} drive."
            )

    partitions.append(PartitionSpec(
        type=PartitionType.LINUX_FILESYSTEM,
        size=root_size,
        fs=_fs_enum(config),
        label=label,
        name="Root",
        flags=["boot"] if target == TargetSystem.BIOS else [],
    ))

    return PartitionLayout(
        scheme=scheme,
        target=target,
        partitions=partitions,
    )


def _create_layout_partitions(conn, device: str, layout: PartitionLayout):
    partitions = calculate_partition_offsets(layout, get_device_size(device))
    created = []
    for spec in partitions:
        part = create_partition(
            conn, device,
            spec.offset, spec.size,
            get_partition_type_guid(spec.type, layout.scheme),
            spec.name, spec.fs.value, spec.label,
            layout.scheme.value,
        )
        created.append(part)
        if layout.scheme == PartitionScheme.MBR:
            set_partition_active(conn, part)
    return created


def _find_case_insensitive(root: str, name: str) -> Optional[str]:
    try:
        for entry in os.listdir(root):
            if entry.lower() == name.lower():
                return os.path.join(root, entry)
    except OSError:
        return None
    return None


def _create_swap_file(root_mount: str, swap_gib: int):
    """Create a swap file inside the installed root (best effort)."""
    if swap_gib <= 0:
        return
    path = os.path.join(root_mount, "swapfile")
    size = f"{swap_gib}G"
    try:
        subprocess.run(["fallocate", "-l", size, path], check=True,
                       capture_output=True)
        os.chmod(path, 0o600)
        subprocess.run(["mkswap", path], check=True, capture_output=True)
        fstab = os.path.join(root_mount, "etc", "fstab")
        try:
            with open(fstab, "a", encoding="utf-8") as f:
                f.write("/swapfile none swap sw 0 0\n")
        except OSError:
            pass
    except (OSError, subprocess.CalledProcessError):
        # Swap is an optimization; never fail the install over it.
        pass


def write_linux_portable(
    iso_path: str,
    device: str,
    config: InstallConfig,
    scheme: PartitionScheme,
    target: TargetSystem,
    label: str,
    progress: ProgressCallback,
    stopped: StoppedCallback,
    status_cb: Optional[StatusCallback] = None,
    iso_root: Optional[str] = None,
    release_stale: bool = True,
) -> bool:
    """Deploy a Linux ISO payload to a partitioned target as a portable OS."""
    emit = status_cb or (lambda _m: None)
    conn = bus()
    dev_path = resolve(conn, device)

    emit("Preparing drive...")
    if release_stale and iso_root is None:
        release_stale_iso_mounts(iso_path)
    unmount_all(conn, dev_path)

    drive_size = get_device_size(device)
    layout = build_linux_layout(config, scheme, target, label, drive_size)
    partitions = calculate_partition_offsets(layout, drive_size)

    wipe_partition_table(conn, device, scheme.value)
    created = _create_layout_partitions(conn, device, layout)

    iso_cm = (contextlib.nullcontext(iso_root) if iso_root
              else mount_iso_readonly(iso_path))
    root_mount = esp_mount = None
    meter = None

    try:
        with iso_cm as mounted_root:
            iso_tree = mounted_root
            is_uefi = (scheme == PartitionScheme.GPT
                       and target == TargetSystem.UEFI)
            if is_uefi:
                esp_mount = mount_filesystem(conn, created[0])
                root_mount = mount_filesystem(conn, created[1])
            else:
                root_mount = mount_filesystem(conn, created[0])

            tree_bytes, _skipped, _oversized = _measure_iso_tree(iso_tree, [])
            efi_src = (_find_case_insensitive(iso_tree, "EFI")
                       if is_uefi else None)
            if is_uefi and not efi_src:
                raise WriteError(
                    "This image has no EFI boot directory; choose BIOS/MBR "
                    "or use a UEFI-bootable image."
                )
            efi_bytes = 0
            if efi_src:
                efi_bytes = _measure_iso_tree(efi_src, [])[0]
            meter = _ProgressMeter(tree_bytes + efi_bytes, progress)

            # The ISO payload lives on the root partition.
            _copy_tree_with_progress(
                iso_tree, root_mount, [], progress, stopped,
                on_bytes=meter.add,
            )
            # UEFI boot files also need to be on the ESP.
            if efi_src and esp_mount:
                _copy_tree_with_progress(
                    efi_src, os.path.join(esp_mount, "EFI"),
                    [], progress, stopped, on_bytes=meter.add,
                )

            write_linux_provisioning(root_mount, config.to_dict())
            _create_swap_file(root_mount, config.swap_gib)

        if esp_mount:
            unmount_filesystem(conn, created[0])
        unmount_filesystem(conn, created[-1])

        if target == TargetSystem.BIOS:
            emit("Installing BIOS bootloader (GRUB)...")
            install_grub_bios(conn, device)

        emit("Syncing...")
        os.sync()
        if meter:
            meter.finish()
        return True

    except StoppedException:
        return False
    finally:
        # Only close mounts that are still open; the success path unmounts
        # explicitly before the bootloader step.
        for part in (created[0] if esp_mount else None, created[-1] if root_mount else None):
            if part:
                try:
                    unmount_filesystem(conn, part)
                except Exception:  # noqa: BLE001 - tolerant cleanup
                    pass


def install_to_drive(
    iso_path: str,
    device: str,
    image_info,
    config: InstallConfig,
    progress: ProgressCallback,
    stopped: StoppedCallback,
    status_cb: Optional[StatusCallback] = None,
    scheme: Optional[PartitionScheme] = None,
    target: Optional[TargetSystem] = None,
    label: str = "LUBUS",
    iso_root: Optional[str] = None,
    release_stale: bool = True,
) -> bool:
    """Install the selected image directly to a drive (portable mode)."""
    drive_size = get_device_size(device)
    errors = validate_install_config(config, drive_size, image_info.image_type)
    if errors:
        raise WriteError("\n".join(errors))

    if image_info.image_type in (ImageType.WINDOWS_INSTALLER, ImageType.WINDOWS_TO_GO):
        from .windows_install import write_windows_direct

        return write_windows_direct(
            iso_path, device, config, progress, stopped,
            status_cb=status_cb,
            iso_root=iso_root,
            release_stale=release_stale,
        )

    if image_info.image_type in (ImageType.LINUX_ISHYBRID, ImageType.LINUX_INSTALLER):
        scheme = scheme or image_info.suggested_scheme
        target = target or image_info.suggested_target
        if target == TargetSystem.UEFI:
            scheme = PartitionScheme.GPT
        return write_linux_portable(
            iso_path, device, config, scheme, target,
            label=label, progress=progress, stopped=stopped,
            status_cb=status_cb, iso_root=iso_root,
            release_stale=release_stale,
        )

    raise WriteError("Direct installation is not supported for this image type yet.")
