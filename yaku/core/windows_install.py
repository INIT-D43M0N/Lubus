"""Windows direct install / Windows To Go deployment pipeline.

This module is the Windows-specific adapter for Yaku 5.0 direct installs:

1. build a GPT/UEFI layout (ESP + MSR + NTFS) or an MBR/BIOS layout
   (single active NTFS partition);
2. locate ``install.wim``/``install.esd`` and apply the selected edition
   index to the system partition with wimlib's NTFS-3G backend;
3. inject ``autounattend.xml``/``unattend.xml`` for first-boot setup;
4. install the UEFI boot manager or BIOS boot chain;
5. flush writeback before reporting completion.

Boot configuration is delegated to :mod:`yaku.core.windows_boot`, which
supports a bcdboot helper or a known-good BCD template.
"""
import contextlib
import os
import shutil
import tempfile

from .direct_install import InstallConfig
from .images import FileSystem, PartitionScheme, TargetSystem
from .partitions import (
    PartitionLayout, PartitionSpec, PartitionType,
    calculate_partition_offsets, format_size, get_partition_type_guid,
)
from .provisioning import write_windows_provisioning
from .windows_debloat import get_wim_indexes, remove_defender_from_wim
from .windows_boot import configure_boot
from .windows_drivers import stage_drivers
from .windows_togo import (
    _apply_wim, _extract_boot_files, fix_ntfs_start_sector,
)
from .writer import (
    ProgressCallback, StatusCallback, StoppedCallback,
    StoppedException, WriteError, _find_install_image,
    install_grub_bios, release_stale_iso_mounts,
)
from ..udisks import (
    bus, resolve, unmount_all, mount_filesystem, unmount_filesystem,
    mount_iso_readonly, create_partition, create_partition_unformatted,
    wipe_partition_table, set_partition_active, get_device_size, objects,
    open_device_exclusive, PARTITION,
)

GiB = 1024 ** 3
MiB = 1024 ** 2
MSR_SIZE = 16 * MiB


def build_windows_layout(
    config: InstallConfig, drive_size: int
) -> PartitionLayout:
    """Build the GPT/UEFI or MBR/BIOS layout for a direct Windows install."""
    win_size = config.root_gib * GiB if config.is_custom else 0

    if config.boot_mode == "uefi":
        esp_size = config.esp_mib * MiB
        reserved = esp_size + MSR_SIZE
        if win_size:
            if win_size + reserved + MiB > drive_size:
                raise WriteError(
                    f"Windows partition ({format_size(win_size)}) plus ESP/MSR "
                    f"does not fit on the {format_size(drive_size)} drive."
                )
        return PartitionLayout(
            scheme=PartitionScheme.GPT,
            target=TargetSystem.UEFI,
            partitions=[
                PartitionSpec(
                    type=PartitionType.EFI_SYSTEM,
                    size=esp_size,
                    fs=FileSystem.FAT32,
                    label="ESP",
                    name="EFI System Partition",
                    flags=["boot", "esp"],
                ),
                PartitionSpec(
                    type=PartitionType.MICROSOFT_RESERVED,
                    size=MSR_SIZE,
                    fs=FileSystem.NTFS,  # not formatted; only used for validation
                    label="MSR",
                    name="Microsoft Reserved Partition",
                    flags=["msftres"],
                ),
                PartitionSpec(
                    type=PartitionType.MICROSOFT_BASIC_DATA,
                    size=win_size,
                    fs=FileSystem.NTFS,
                    label="Windows",
                    name="Windows",
                    flags=["msftdata"],
                ),
            ],
        )

    # MBR/BIOS: one active NTFS partition containing bootmgr + Windows.
    if win_size and win_size + MiB > drive_size:
        raise WriteError(
            f"Windows partition ({format_size(win_size)}) does not fit on "
            f"the {format_size(drive_size)} drive."
        )
    return PartitionLayout(
        scheme=PartitionScheme.MBR,
        target=TargetSystem.BIOS,
        partitions=[
            PartitionSpec(
                type=PartitionType.MICROSOFT_BASIC_DATA,
                size=win_size,
                fs=FileSystem.NTFS,
                label="Windows",
                name="Windows",
                flags=["boot"],
            ),
        ],
    )


def _create_windows_partitions(conn, device: str, layout: PartitionLayout):
    specs = calculate_partition_offsets(layout, get_device_size(device))
    created = []
    for spec in specs:
        type_id = get_partition_type_guid(spec.type, layout.scheme)
        if spec.type == PartitionType.MICROSOFT_RESERVED:
            part = create_partition_unformatted(
                conn, device, spec.offset, spec.size,
                type_id, spec.name, layout.scheme.value,
            )
        else:
            part = create_partition(
                conn, device, spec.offset, spec.size,
                type_id, spec.name, spec.fs.value, spec.label,
                layout.scheme.value,
            )
        if layout.scheme == PartitionScheme.MBR:
            set_partition_active(conn, part)
        created.append(part)
    return created


def write_windows_direct(
    iso_path: str,
    device: str,
    config: InstallConfig,
    progress: ProgressCallback,
    stopped: StoppedCallback,
    status_cb: StatusCallback | None = None,
    iso_root: str | None = None,
    release_stale: bool = True,
) -> bool:
    """Deploy Windows directly to ``device`` as a bootable system."""
    emit = status_cb or (lambda _m: None)
    conn = bus()
    dev_path = resolve(conn, device)

    emit("Preparing drive...")
    if release_stale and iso_root is None:
        release_stale_iso_mounts(iso_path)
    unmount_all(conn, dev_path)

    drive_size = get_device_size(device)
    layout = build_windows_layout(config, drive_size)
    specs = calculate_partition_offsets(layout, drive_size)

    wipe_partition_table(conn, device, layout.scheme.value)
    created = _create_windows_partitions(conn, device, layout)
    if not created:
        raise WriteError("No Windows partition was created")

    win_part = created[-1]
    esp_part = created[0] if config.boot_mode == "uefi" else None

    iso_cm = (contextlib.nullcontext(iso_root) if iso_root
              else mount_iso_readonly(iso_path))
    try:
        with iso_cm as iso_tree, tempfile.TemporaryDirectory(
            prefix="yaku-win-"
        ) as tmp:
            wim_path = _find_install_image(iso_tree)
            if not wim_path:
                raise WriteError(
                    "No install.wim or install.esd found in the Windows image"
                )

            if config.remove_defender:
                size = os.path.getsize(wim_path)
                free = shutil.disk_usage(tmp).free
                if free < size + 256 * 1024**2:
                    raise WriteError(
                        "Not enough temporary space to debloat install.wim "
                        f"({format_size(size)} needed, "
                        f"{format_size(free)} free)."
                    )
                local_wim = os.path.join(tmp, os.path.basename(wim_path))
                emit("Copying Windows image for debloat...")
                shutil.copyfile(wim_path, local_wim)
                remove_defender_from_wim(
                    local_wim, [config.edition_index],
                    status_cb=emit,
                )
                wim_path = local_wim

            objs = objects(conn)
            part_info = objs[win_part][PARTITION]

            emit("Extracting Windows boot files...")
            boot_subdir = ("Windows/Boot/EFI" if config.boot_mode == "uefi"
                           else "Windows/Boot/PCAT")
            boot_src = _extract_boot_files(
                wim_path, config.edition_index, tmp, boot_subdir
            )

            emit("Deploying Windows image...")
            unmount_filesystem(conn, win_part)
            ntfs_fd = open_device_exclusive(conn, win_part, "rw")
            try:
                fix_ntfs_start_sector(ntfs_fd, part_info["Offset"])
                wim_total = _apply_wim(
                    wim_path, config.edition_index, ntfs_fd,
                    progress, stopped,
                )
                os.fsync(ntfs_fd)
            finally:
                os.close(ntfs_fd)

            emit("Writing first-boot configuration...")
            try:
                win_mount = mount_filesystem(conn, win_part)
                try:
                    write_windows_provisioning(win_mount, config.to_dict())
                    if config.drivers_path:
                        stage_drivers(
                            config.drivers_path, win_mount, status_cb=emit
                        )
                finally:
                    unmount_filesystem(conn, win_part)
            except Exception as exc:  # noqa: BLE001
                # Provisioning is valuable but must not destroy an otherwise
                # valid deployment if the NTFS mount is unavailable.
                emit(f"Provisioning skipped: {exc}")

            if config.boot_mode == "uefi":
                emit("Installing UEFI bootloader...")
                esp_mount = mount_filesystem(conn, esp_part)
                win_mount = mount_filesystem(conn, win_part)
                try:
                    configure_boot(
                        "uefi", win_mount, esp_mount, boot_src
                    )
                finally:
                    unmount_filesystem(conn, win_part)
                    unmount_filesystem(conn, esp_part)
            else:
                emit("Installing BIOS bootloader...")
                win_mount = mount_filesystem(conn, win_part)
                try:
                    configure_boot("bios", win_mount, None, boot_src)
                finally:
                    unmount_filesystem(conn, win_part)
                install_grub_bios(conn, device)

            # Only now is the disk both written and boot-configured.
            emit("Syncing...")
            os.sync()
            progress(wim_total, wim_total)
            return True

    except StoppedException:
        return False
