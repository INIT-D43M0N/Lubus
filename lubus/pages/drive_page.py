"""Screen 1 — installation mode and target drive selection.

The page uses a native libadwaita layout that follows the GNOME HIG:

* ``AdwPreferencesPage`` groups for installation, provisioning and drives;
* ``AdwComboRow`` / ``AdwSwitchRow`` / ``AdwEntryRow`` for settings;
* the first two drives as ``AdwActionRow`` targets, with a **More Drives…**
  action that opens a native ``Adw.Dialog`` listing every drive and a
  **Select All** control.

The inclusion state is centralised in ``_checked_devices`` so the two main
rows and the dialog stay in sync, including drives that are only visible in
the dialog.
"""
import logging
import threading

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib

from lubus.utils.i18n import _, ngettext
from lubus.core.devices import get_all_drives, Drive, BusType
from lubus.core.direct_install import InstallConfig
from lubus.core.images import ImageType
from lubus.pages.base import UiPage
from lubus.pages.drive_dialog import DriveListDialog

log = logging.getLogger(__name__)

MAX_PROMINENT_DRIVES = 2

_BUS_LABELS = {
    BusType.USB: _("USB"),
    BusType.SATA: _("SATA"),
    BusType.NVME: _("NVMe"),
    BusType.SD: _("SD card"),
    BusType.UNKNOWN: "",
}

_FS_NAMES = ("ext4", "btrfs")


class DrivePage(UiPage):
    """Page for selecting installation mode and one or more target drives."""

    ui_name = "drive_page.ui"
    root_id = "root"

    def __init__(self, on_drive_selected=None, **kwargs):
        super().__init__()
        self.on_drive_selected = on_drive_selected
        self._drives: list[Drive] = []
        self._selected_drive: Drive | None = None
        self._selected_device: str | None = None
        self._checked_devices: set[str] = set()
        self._updating = False
        self._user_interacting = False
        self._drive_dialog: DriveListDialog | None = None

        self._custom_root_gib = 32
        self._custom_swap_gib = 4
        self._custom_fs_index = 0
        self._custom_esp_mib = 512
        self._image_type = None
        self._include_internal = False

        # Widgets
        self.state_stack = self.get("state_stack")
        self.drives_group = self.get("drives_group")
        self.drive_list = self.get("drive_list")
        self.more_btn = self.get("more_btn")
        self.empty_state = self.get("empty_state")
        self.erase_banner = self.get("erase_banner")
        self.error_banner = self.get("error_banner")
        self.refresh_btn = self.get("refresh_btn")

        # Installation mode / provisioning widgets
        self.install_mode_row = self.get("install_mode_row")
        self.boot_mode_row = self.get("boot_mode_row")
        self.partition_mode_row = self.get("partition_mode_row")
        self.direct_fs_row = self.get("direct_fs_row")
        self.partition_layout_row = self.get("partition_layout_row")
        self.show_internal_row = self.get("show_internal_row")
        self.configure_partitions_btn = self.get("configure_partitions_btn")
        self.user_group = self.get("user_group")
        self.username_row = self.get("username_row")
        self.password_row = self.get("password_row")
        self.fullname_row = self.get("fullname_row")
        self.hostname_row = self.get("hostname_row")
        self.timezone_row = self.get("timezone_row")
        self.language_row = self.get("language_row")
        self.keyboard_row = self.get("keyboard_row")
        self.bypass_win11_row = self.get("bypass_win11_row")

        self._init_install_controls()

        # Signals
        self.drive_list.connect("row-selected", self._on_row_selected)
        self.drive_list.connect("row-activated", self._on_row_activated)
        self.more_btn.connect("clicked", self._on_more_drives_clicked)
        self.refresh_btn.connect("clicked", lambda _b: self.refresh_drives())
        self.install_mode_row.connect("notify::selected", self._on_install_mode_changed)
        self.boot_mode_row.connect("notify::selected", self._on_boot_mode_changed)
        self.partition_mode_row.connect("notify::selected", self._on_partition_mode_changed)
        self.direct_fs_row.connect("notify::selected", self._on_direct_fs_changed)
        self.show_internal_row.connect("notify::active", self._on_show_internal)
        self.configure_partitions_btn.connect("clicked", self._on_configure_partitions)

    # ------------------------------------------------------------------ API

    def refresh_drives(self):
        """Refresh the drive list in a background thread."""
        self.state_stack.set_visible_child_name("list")

        def worker():
            try:
                drives = get_all_drives(
                    include_internal=self._include_internal
                )
                GLib.idle_add(self._update_drive_list, drives)
            except Exception as e:  # noqa: BLE001 - surfaced via the banner
                log.exception("Failed to enumerate drives")
                GLib.idle_add(self._show_error, str(e))

        threading.Thread(target=worker, daemon=True).start()

    def get_selected_drive(self) -> Drive | None:
        """Return the primary selected drive (compatibility API)."""
        selected = self.get_selected_drives()
        if not selected:
            return None
        if (self._selected_drive is not None
                and self._selected_drive.device in {d.device for d in selected}):
            return self._selected_drive
        return selected[0]

    def get_selected_drives(self) -> list[Drive]:
        """Return every included drive, in detection order."""
        return [d for d in self._drives if d.device in self._checked_devices]

    def set_selected_drive(self, device: str):
        """Pre-select a drive by device path (e.g. from the command line)."""
        self._selected_device = device
        self._checked_devices.add(device)
        self._selected_drive = self._drive_by_device(device)
        row = self._row_for_device(device)
        self._updating = True
        try:
            if row is not None:
                row._include_check.set_active(True)
                row.get_parent().select_row(row)
            else:
                self.drive_list.select_row(None)
        finally:
            self._updating = False
        self._update_main_checks()
        self._sync_dialog()
        self._notify_selection_changed()

    def is_user_interacting(self) -> bool:
        """True while the all-drives popup is open (pauses auto-refresh)."""
        return self._user_interacting

    # ------------------------------------------------------- install mode UI

    def _init_install_controls(self):
        self.install_mode_row.set_model(Gtk.StringList.new([
            _("Live USB"),
            _("Direct install (portable)"),
        ]))
        self.boot_mode_row.set_model(Gtk.StringList.new([
            _("UEFI (GPT)"),
            _("Legacy BIOS (MBR)"),
        ]))
        self.partition_mode_row.set_model(Gtk.StringList.new([
            _("Automatic"),
            _("Custom"),
        ]))
        self.direct_fs_row.set_model(Gtk.StringList.new(list(_FS_NAMES)))
        self.install_mode_row.set_selected(0)
        self.boot_mode_row.set_selected(0)
        self.partition_mode_row.set_selected(0)
        self.direct_fs_row.set_selected(0)
        self._custom_fs_index = 0
        self._custom_esp_mib = 512
        self._image_type = None
        self._update_partition_summary()

    def _is_direct(self) -> bool:
        return self.install_mode_row.get_selected() == 1

    def _is_custom(self) -> bool:
        return self.partition_mode_row.get_selected() == 1

    def _on_install_mode_changed(self, _row, _pspec):
        direct = self._is_direct()
        windows = self._image_type in (
            ImageType.WINDOWS_INSTALLER, ImageType.WINDOWS_TO_GO
        )
        self.user_group.set_visible(direct)
        self.boot_mode_row.set_visible(direct)
        self.partition_mode_row.set_visible(direct)
        self.direct_fs_row.set_visible(direct)
        self.bypass_win11_row.set_visible(direct and windows)
        self.show_internal_row.set_visible(direct)
        self.partition_layout_row.set_visible(direct and self._is_custom())
        if not direct and self.show_internal_row.get_active():
            self.show_internal_row.set_active(False)
        self._update_partition_summary()
        self._notify_selection_changed()

    def set_image_type(self, image_type):
        """Adapt the direct-install controls to Windows or Linux images."""
        if image_type == self._image_type:
            return
        self._image_type = image_type
        windows = image_type in (
            ImageType.WINDOWS_INSTALLER, ImageType.WINDOWS_TO_GO
        )
        if windows:
            self.direct_fs_row.set_model(Gtk.StringList.new(["NTFS"]))
            self.direct_fs_row.set_selected(0)
        else:
            self.direct_fs_row.set_model(Gtk.StringList.new(list(_FS_NAMES)))
            self.direct_fs_row.set_selected(
                min(self._custom_fs_index, len(_FS_NAMES) - 1)
            )
        self.bypass_win11_row.set_visible(self._is_direct() and windows)
        # ChromeOS and macOS media have their own layout, they cannot be
        # installed as a portable system on a partition of our choosing.
        own_layout = image_type in (ImageType.CHROMEOS, ImageType.MACOS)
        self.install_mode_row.set_sensitive(not own_layout)
        if own_layout:
            self.install_mode_row.set_selected(0)
        self._update_partition_summary()

    def _on_boot_mode_changed(self, _row, _pspec):
        self._update_partition_summary()
        self._notify_selection_changed()

    def _on_partition_mode_changed(self, _row, _pspec):
        self.partition_layout_row.set_visible(
            self._is_direct() and self._is_custom()
        )
        self._update_partition_summary()
        self._notify_selection_changed()

    def _on_direct_fs_changed(self, _row, _pspec):
        self._custom_fs_index = max(0, self.direct_fs_row.get_selected())
        self._update_partition_summary()
        self._notify_selection_changed()

    def _on_show_internal(self, row, _pspec):
        self._include_internal = bool(row.get_active())
        if self._include_internal:
            self.drives_group.set_description(
                _("Select one or more drives. Every included drive will be "
                  "erased. Internal disks are shown; the system disk and "
                  "mounted disks are always excluded.")
            )
        else:
            self.drives_group.set_description(
                _("Select one or more drives. Every included drive will be "
                  "erased. Internal disks are hidden for safety.")
            )
        self.refresh_drives()

    def get_install_config(self) -> InstallConfig:
        """Collect the installation mode and provisioning settings."""
        mode_idx = self.install_mode_row.get_selected()
        part_idx = self.partition_mode_row.get_selected()
        fs_idx = self.direct_fs_row.get_selected()
        filesystem = _FS_NAMES[fs_idx] if 0 <= fs_idx < len(_FS_NAMES) else "ext4"
        windows = self._image_type in (
            ImageType.WINDOWS_INSTALLER, ImageType.WINDOWS_TO_GO
        )
        return InstallConfig(
            mode="direct" if mode_idx == 1 else "live",
            partition_mode="custom" if part_idx == 1 else "auto",
            username=(self.username_row.get_text() or "").strip(),
            password=self.password_row.get_text() or "",
            full_name=(self.fullname_row.get_text() or "").strip(),
            hostname=(self.hostname_row.get_text() or "").strip(),
            timezone=(self.timezone_row.get_text() or "").strip(),
            filesystem="ntfs" if windows else filesystem,
            root_gib=self._custom_root_gib,
            swap_gib=self._custom_swap_gib,
            boot_mode="uefi" if self.boot_mode_row.get_selected() == 0 else "bios",
            esp_mib=self._custom_esp_mib,
            language=(self.language_row.get_text() or "en-US").strip(),
            keyboard=(self.keyboard_row.get_text() or "0409:00000409").strip(),
            bypass_win11=bool(self.bypass_win11_row.get_active()),
        )

    def _update_partition_summary(self):
        if not self._is_direct():
            subtitle = _("Automatic")
        elif self._is_custom():
            windows = self._image_type in (
                ImageType.WINDOWS_INSTALLER, ImageType.WINDOWS_TO_GO
            )
            if windows:
                boot = "UEFI" if self.boot_mode_row.get_selected() == 0 else "BIOS"
                subtitle = _(
                    "Windows {root} GiB · ESP {esp} MiB · {boot}"
                ).format(
                    root=self._custom_root_gib,
                    esp=self._custom_esp_mib,
                    boot=boot,
                )
            else:
                subtitle = _(
                    "Root {root} GiB · Swap {swap} GiB · {fs}"
                ).format(
                    root=self._custom_root_gib,
                    swap=self._custom_swap_gib,
                    fs=_FS_NAMES[self._custom_fs_index],
                )
        else:
            subtitle = _("Automatic (recommended layout for the selected image)")
        self.partition_layout_row.set_subtitle(subtitle)

    def _on_configure_partitions(self, _btn):
        windows = self._image_type in (
            ImageType.WINDOWS_INSTALLER, ImageType.WINDOWS_TO_GO
        )
        root_spin = Gtk.SpinButton.new_with_range(8, 4096, 1)
        root_spin.set_value(self._custom_root_gib)
        root_spin.set_numeric(True)
        swap_spin = Gtk.SpinButton.new_with_range(0, 512, 1)
        swap_spin.set_value(self._custom_swap_gib)
        swap_spin.set_numeric(True)
        esp_spin = Gtk.SpinButton.new_with_range(100, 2048, 16)
        esp_spin.set_value(self._custom_esp_mib)
        esp_spin.set_numeric(True)
        fs_drop = Gtk.DropDown.new_from_strings(list(_FS_NAMES))
        fs_drop.set_selected(self._custom_fs_index)

        box = Gtk.ListBox()
        box.add_css_class("boxed-list")
        rows = [(_("Root / Windows partition (GiB)"), root_spin)]
        if windows:
            rows.append((_("EFI System Partition (MiB)"), esp_spin))
        else:
            rows.extend([
                (_("Swap file (GiB)"), swap_spin),
                (_("Filesystem"), fs_drop),
            ])
        for title, widget in rows:
            row = Adw.ActionRow(title=title)
            widget.set_valign(Gtk.Align.CENTER)
            row.add_suffix(widget)
            box.append(row)

        dialog = Adw.AlertDialog(
            heading=_("Custom Partitioning"),
            body=_("Choose the size of the installed system and its swap file."),
        )
        dialog.set_extra_child(box)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("apply", _("Apply"))
        dialog.set_response_appearance("apply", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("apply")
        dialog.set_close_response("cancel")

        def on_response(dlg, result):
            try:
                response = dlg.choose_finish(result)
            except GLib.Error:
                return
            if response != "apply":
                return
            self._custom_root_gib = int(root_spin.get_value())
            if windows:
                self._custom_esp_mib = int(esp_spin.get_value())
            else:
                self._custom_swap_gib = int(swap_spin.get_value())
                self._custom_fs_index = max(0, fs_drop.get_selected())
                self.direct_fs_row.set_selected(self._custom_fs_index)
            self._update_partition_summary()
            self._notify_selection_changed()

        dialog.choose(self.widget, None, on_response)

    # -------------------------------------------------------- drive selection

    def _drive_by_device(self, device: str):
        return next((d for d in self._drives if d.device == device), None)

    def _rows(self):
        row = self.drive_list.get_first_child()
        while row is not None:
            yield row
            row = row.get_next_sibling()

    def _row_for_device(self, device: str):
        for row in self._rows():
            drive = getattr(row, "drive", None)
            if drive is not None and drive.device == device:
                return row
        return None

    @staticmethod
    def _clear_list(listbox):
        row = listbox.get_first_child()
        while row is not None:
            nxt = row.get_next_sibling()
            listbox.remove(row)
            row = nxt

    def _create_drive_row(self, drive: Drive) -> Adw.ActionRow:
        name = drive.model or drive.product or drive.device
        if drive.vendor and drive.vendor.lower() not in name.lower():
            name = f"{drive.vendor} {name}"

        bus = _BUS_LABELS.get(drive.bus_type, "")
        subtitle = (f"{drive.device} · {bus} · {drive.size_human}"
                    if bus else f"{drive.device} · {drive.size_human}")

        row = Adw.ActionRow(
            title=name,
            subtitle=subtitle,
            icon_name="drive-removable-media-symbolic",
            activatable=True,
        )
        row.drive = drive

        check = Gtk.CheckButton()
        check.set_valign(Gtk.Align.CENTER)
        check.set_tooltip_text(_("Include this drive in the write"))
        check.set_active(drive.device in self._checked_devices)
        check.connect("toggled", self._on_drive_checked, drive.device)
        row.add_suffix(check)
        row._include_check = check
        return row

    def _on_row_selected(self, _listbox, row):
        if self._updating or row is None:
            return
        drive = getattr(row, "drive", None)
        if drive is None:
            return
        self._checked_devices.add(drive.device)
        self._selected_drive = drive
        self._selected_device = drive.device
        self._update_main_checks()
        self._sync_dialog()
        self._notify_selection_changed()

    def _on_row_activated(self, _listbox, row):
        if row is not None:
            row.get_parent().select_row(row)

    def _on_drive_checked(self, check, device: str):
        if self._updating:
            return
        self._set_device_checked(device, check.get_active())

    def _set_device_checked(self, device: str, active: bool):
        if self._updating:
            return
        if active:
            self._checked_devices.add(device)
        else:
            self._checked_devices.discard(device)

        selected = self.get_selected_drives()
        if active and self._selected_device is None:
            self._selected_drive = self._drive_by_device(device)
            self._selected_device = device
        elif not active and self._selected_device == device:
            if selected:
                self._selected_drive = selected[0]
                self._selected_device = selected[0].device
            else:
                self._selected_drive = None
                self._selected_device = None

        self._updating = True
        try:
            row = self._row_for_device(self._selected_device) if self._selected_device else None
            if row is not None:
                row.get_parent().select_row(row)
            else:
                self.drive_list.select_row(None)
        finally:
            self._updating = False
        self._update_main_checks()
        self._sync_dialog()
        self._notify_selection_changed()

    def _set_all_devices_checked(self, active: bool):
        if active:
            self._checked_devices = {d.device for d in self._drives}
            if self._drives:
                self._selected_drive = self._drives[0]
                self._selected_device = self._drives[0].device
        else:
            self._checked_devices.clear()
            self._selected_drive = None
            self._selected_device = None

        self._updating = True
        try:
            row = self._row_for_device(self._selected_device) if self._selected_device else None
            if row is not None:
                row.get_parent().select_row(row)
            else:
                self.drive_list.select_row(None)
        finally:
            self._updating = False
        self._update_main_checks()
        self._sync_dialog()
        self._notify_selection_changed()

    def _update_main_checks(self):
        self._updating = True
        try:
            for row in self._rows():
                drive = getattr(row, "drive", None)
                check = getattr(row, "_include_check", None)
                if drive is not None and check is not None:
                    check.set_active(drive.device in self._checked_devices)
        finally:
            self._updating = False

    def _notify_selection_changed(self):
        self.erase_banner.set_revealed(bool(self._checked_devices))
        if self.on_drive_selected:
            self.on_drive_selected(self.get_selected_drive())

    # ---------------------------------------------------------- all drives

    def _on_more_drives_clicked(self, _btn):
        if self._drive_dialog is not None:
            self._drive_dialog.present(self.widget)
            return
        self._user_interacting = True
        dialog = DriveListDialog(
            self._drives,
            self._checked_devices,
            self._on_dialog_toggle,
            self._on_dialog_select_all,
        )
        dialog.connect("closed", self._on_dialog_closed)
        self._drive_dialog = dialog
        dialog.present(self.widget)

    def _on_dialog_toggle(self, device, active):
        self._set_device_checked(device, active)

    def _on_dialog_select_all(self, active):
        self._set_all_devices_checked(active)

    def _on_dialog_closed(self, *_args):
        self._drive_dialog = None
        self._user_interacting = False

    def _sync_dialog(self):
        if self._drive_dialog is not None:
            self._drive_dialog.set_checked(self._checked_devices)

    def _update_drive_list(self, drives: list[Drive]):
        """Rebuild the two prominent rows and the all-drives modal data."""
        self._updating = True
        try:
            self._drives = list(drives)
            self.error_banner.set_revealed(False)
            self._clear_list(self.drive_list)

            if not drives:
                self._selected_drive = None
                self._selected_device = None
                self._checked_devices.clear()
                self.erase_banner.set_revealed(False)
                self.more_btn.set_visible(False)
                if self._drive_dialog is not None:
                    self._drive_dialog.close()
                self.state_stack.set_visible_child_name("empty")
                return

            present = {d.device for d in drives}
            self._checked_devices &= present
            target = next(
                (d for d in drives if d.device == self._selected_device),
                drives[0],
            )
            if not self._checked_devices:
                self._checked_devices.add(target.device)
            if target.device not in self._checked_devices:
                self._checked_devices.add(target.device)

            for drive in drives[:MAX_PROMINENT_DRIVES]:
                self.drive_list.append(self._create_drive_row(drive))

            extra = len(drives) - MAX_PROMINENT_DRIVES
            self.more_btn.set_visible(extra > 0)
            if extra > 0:
                self.more_btn.set_label(
                    _("More Drives… ({count})").format(count=len(drives))
                )

            self._selected_drive = target
            self._selected_device = target.device
            row = self._row_for_device(target.device)
            if row is not None:
                row.get_parent().select_row(row)
            self.erase_banner.set_revealed(bool(self._checked_devices))
            self.state_stack.set_visible_child_name("list")
        finally:
            self._updating = False

        self._update_main_checks()
        if self._drive_dialog is not None:
            self._drive_dialog.set_drives(self._drives)
            self._sync_dialog()
        self._notify_selection_changed()

    # --------------------------------------------------------------- errors

    def _show_error(self, error: str):
        self.error_banner.set_title(
            _("Could not read the drive list: {error}").format(error=error)
        )
        self.error_banner.set_revealed(True)
        self._selected_drive = None
        self._selected_device = None
        self._checked_devices.clear()
        if self._drive_dialog is not None:
            self._drive_dialog.close()
        self.erase_banner.set_revealed(False)
        self._clear_list(self.drive_list)
        self.more_btn.set_visible(False)
        self.state_stack.set_visible_child_name("empty")
