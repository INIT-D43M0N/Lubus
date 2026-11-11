"""Native all-drives dialog for Screen 1.

The dialog follows the GNOME HIG: an ``Adw.Dialog`` with an ``Adw.HeaderBar``,
an ``Adw.PreferencesPage`` of ``Adw.ActionRow`` entries and a suggested
"Done" action.  Each row has an inclusion checkbox and the first row is a
master "Select All" control with an inconsistent state for partial
selections.
"""
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw

from yaku.utils.i18n import _


class DriveListDialog(Adw.Dialog):
    """Dialog listing every detected drive with per-drive and Select All toggles."""

    def __init__(self, drives, checked_devices, on_toggle, on_select_all,
                 **kwargs):
        super().__init__(**kwargs)
        self._drives = list(drives)
        self._checked = set(checked_devices)
        self._on_toggle = on_toggle
        self._on_select_all = on_select_all
        self._updating = False
        self._rows = []

        self.set_title(_("All Drives"))
        self.set_content_width(560)
        self.set_content_height(600)
        self._build_ui()
        self.set_checked(self._checked)

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        toolbar = Adw.ToolbarView()

        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(
            title=_("All Drives"),
            subtitle=_("Choose the targets to include"),
        ))

        done = Gtk.Button(label=_("Done"))
        done.add_css_class("suggested-action")
        done.set_tooltip_text(_("Apply the drive selection"))
        done.connect("clicked", lambda *_a: self.close())
        header.pack_end(done)
        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()
        toolbar.set_content(page)

        self._select_all_row = Adw.ActionRow(
            title=_("Select All"),
            subtitle=_("0 of 0 selected"),
            activatable=True,
        )
        self._select_all = Gtk.CheckButton(valign=Gtk.Align.CENTER)
        self._select_all.set_tooltip_text(_("Include every listed drive"))
        self._select_all.connect("toggled", self._on_select_all_toggled)
        self._select_all_row.add_suffix(self._select_all)
        self._select_all_row.connect(
            "activated", self._on_select_all_row_activated
        )

        drives_group = Adw.PreferencesGroup(
            title=_("Available Drives"),
            description=_(
                "The system disk and mounted disks are never listed. "
                "Every included drive will be erased."
            ),
        )
        drives_group.add(self._select_all_row)
        page.add(drives_group)
        self._list = drives_group

        self.set_child(toolbar)
        self._populate()

    def _populate(self):
        for row in self._rows:
            self._list.remove(row)
        self._rows = []

        for drive in self._drives:
            row = self._create_row(drive)
            self._rows.append(row)
            self._list.add(row)

    def _create_row(self, drive):
        name = drive.model or drive.product or drive.device
        if drive.vendor and drive.vendor.lower() not in name.lower():
            name = f"{drive.vendor} {name}"

        bus = getattr(drive.bus_type, "value", "")
        subtitle = f"{drive.device} · {bus.upper()} · {drive.size_human}" \
            if bus else f"{drive.device} · {drive.size_human}"

        row = Adw.ActionRow(
            title=name,
            subtitle=subtitle,
            icon_name="drive-removable-media-symbolic",
            activatable=True,
        )
        row.drive = drive

        check = Gtk.CheckButton(valign=Gtk.Align.CENTER)
        check.set_tooltip_text(
            _("Include {name} in the write").format(name=name)
        )
        check.connect("toggled", self._on_row_toggled, drive.device)
        row.add_suffix(check)
        row.check = check
        row.connect("activated", lambda r: r.check.set_active(
            not r.check.get_active()
        ))
        return row

    # ------------------------------------------------------------ handlers

    def _on_row_toggled(self, check, device):
        if self._updating:
            return
        self._on_toggle(device, check.get_active())

    def _on_select_all_toggled(self, check):
        if self._updating:
            return
        target = True if check.get_inconsistent() else check.get_active()
        self._on_select_all(target)

    def _on_select_all_row_activated(self, _row):
        if self._updating:
            return
        if self._select_all.get_inconsistent():
            self._select_all.set_active(True)
        else:
            self._select_all.set_active(
                not self._select_all.get_active()
            )

    # ----------------------------------------------------------------- API

    def set_drives(self, drives):
        """Rebuild the list if the page refreshes while the dialog is open."""
        self._drives = list(drives)
        self._checked &= {d.device for d in self._drives}
        self._populate()
        self.set_checked(self._checked)

    def set_checked(self, devices):
        """Synchronise checkbox state and count from the page."""
        self._checked = set(devices)
        total = len(self._drives)
        selected = len(self._checked)

        self._updating = True
        try:
            for row in self._rows:
                row.check.set_active(row.drive.device in self._checked)

            self._select_all_row.set_subtitle(
                _("{selected} of {total} selected").format(
                    selected=selected, total=total
                )
            )

            if selected == 0:
                self._select_all.set_inconsistent(False)
                self._select_all.set_active(False)
            elif selected == total and total > 0:
                self._select_all.set_inconsistent(False)
                self._select_all.set_active(True)
            else:
                self._select_all.set_inconsistent(True)
                self._select_all.set_active(True)
        finally:
            self._updating = False
