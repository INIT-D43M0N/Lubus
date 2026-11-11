"""Image selection page (Step 2 of the wizard).

Layout: ``data/ui/image_page.ui`` — a centered empty state with the
interactive disc easter egg (:class:`yaku.pages.disc.DiscFlip`) and an
Adw.PreferencesPage with native Combo/Entry rows for the write options,
all rebuilt from the analysis result.

Business logic (analysis, edition listing) stays in ``yaku.core`` and runs
in worker threads; this class only mirrors results into the widgets.
"""
import logging
import os
import threading

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib, Gio, Gdk

from yaku.utils.i18n import _
from yaku.core.images import (
    ImageType, analyze_image, list_windows_editions,
    ImageInfo, FileSystem, PartitionScheme, TargetSystem,
)
from yaku.core.devices import format_size
from yaku.udisks import mount_iso_readonly, PortalPathError  # noqa: F401 (kept for API parity)
from yaku.pages.base import UiPage
from yaku.pages.disc import DiscFlip
from yaku.pages.iso_download import IsoDownloadDialog

log = logging.getLogger(__name__)

IMAGE_EXTENSIONS = (".iso", ".img", ".dd", ".raw", ".bin", ".dmg")

# Cluster size options per filesystem (Rufus-compatible set)
CLUSTER_OPTIONS = {
    FileSystem.FAT32: ["512 bytes", "1 KiB", "2 KiB", "4 KiB", "8 KiB", "16 KiB", "32 KiB", "64 KiB"],
    FileSystem.NTFS: ["512 bytes", "1 KiB", "2 KiB", "4 KiB", "8 KiB", "16 KiB", "32 KiB", "64 KiB"],
    FileSystem.EXFAT: [
        "512 bytes", "1 KiB", "2 KiB", "4 KiB", "8 KiB", "16 KiB", "32 KiB",
        "64 KiB", "128 KiB", "256 KiB", "512 KiB", "1 MiB", "2 MiB", "4 MiB",
        "8 MiB", "16 MiB", "32 MiB",
    ],
    FileSystem.EXT4: ["1 KiB", "2 KiB", "4 KiB"],
    FileSystem.BTRFS: ["4 KiB (default)"],
}


class ImagePage(UiPage):
    """Page for selecting and analyzing the image to write."""

    ui_name = "image_page.ui"
    root_id = "root"

    def __init__(self, on_image_selected=None, on_analysis_complete=None, **kwargs):
        super().__init__()
        self.on_image_selected = on_image_selected
        self.on_analysis_complete = on_analysis_complete
        self._image_path: str | None = None
        self._image_info: ImageInfo | None = None
        self._analyzing = False
        self._editions: list[tuple] = []
        self._window = None

        # Widgets
        self.state_stack = self.get("state_stack")
        self.empty_state = self.get("empty_state")
        self.disc_slot = self.get("disc_slot")
        self.browse_btn = self.get("browse_btn")
        self.download_btn = self.get("download_btn")
        self.change_btn = self.get("change_btn")
        self.error_banner = self.get("error_banner")
        self.warning_banner = self.get("warning_banner")
        self.image_row = self.get("image_row")
        self.detected_row = self.get("detected_row")
        self.options_group = self.get("options_group")
        self.edition_row = self.get("edition_row")
        self.scheme_row = self.get("scheme_row")
        self.target_row = self.get("target_row")
        self.fs_row = self.get("fs_row")
        self.label_row = self.get("label_row")
        self.cluster_row = self.get("cluster_row")
        self.mode_row = self.get("mode_row")

        # Drop target for ISO files anywhere on the page
        drop_target = Gtk.DropTarget.new(
            Gdk.FileList, Gdk.DragAction.COPY | Gdk.DragAction.MOVE
        )
        drop_target.connect("drop", self._on_drop)
        drop_target.connect("enter", self._on_drag_enter)
        drop_target.connect("leave", self._on_drag_leave)
        self.widget.add_controller(drop_target)

        # Interactive disc easter egg: holographic back by default, flips to
        # the artwork on click (see yaku.pages.disc).
        self.disc_flip = DiscFlip()
        self.disc_slot.append(self.disc_flip)

        self._fs_names: list[str] = []
        self._init_dropdowns()
        self.browse_btn.connect("clicked", self._on_browse_clicked)
        self.download_btn.connect("clicked", self._on_download_clicked)
        self.change_btn.connect("clicked", self._on_browse_clicked)
        self.error_banner.connect("button-clicked", lambda _b: self._on_browse_clicked(None))
        self.scheme_row.connect("notify::selected", self._on_scheme_changed)
        self.label_row.connect("changed", self._on_label_changed)

    # ---------------------------------------------------------------- public

    def set_image(self, path: str):
        """Select an image and start background analysis."""
        if not path or not os.path.exists(path):
            return

        self._image_path = path
        self._analyzing = True
        self.error_banner.set_revealed(False)

        if self.on_image_selected:
            self.on_image_selected(path)

        # Show analysis state
        self.image_row.set_title(os.path.basename(path))
        self.image_row.set_subtitle(_("Analyzing image…"))
        self.detected_row.set_subtitle(_("Analyzing image…"))
        self.state_stack.set_visible_child_name("image")

        threading.Thread(target=self._analyze_worker, args=(path,), daemon=True).start()

    def get_image_info(self) -> ImageInfo | None:
        return self._image_info

    def get_image_path(self) -> str | None:
        return self._image_path

    def is_analyzing(self) -> bool:
        return self._analyzing

    def get_selected_edition_index(self) -> int:
        """Selected Windows edition (1-based index inside the WIM)."""
        if self._editions:
            pos = self.edition_row.get_selected()
            if 0 <= pos < len(self._editions):
                return self._editions[pos][0]
        return 1

    def is_ready_for_next_step(self) -> bool:
        """True when a fully analyzed image is present."""
        if not self._image_path or self._analyzing:
            return False
        if self._image_info and self._image_info.image_type == ImageType.WINDOWS_INSTALLER:
            if self._editions:
                pos = self.edition_row.get_selected()
                if pos < 0 or pos >= len(self._editions):
                    return False
        return True

    def get_settings(self) -> dict:
        """Current write settings (consumed by the summary and the writer)."""
        scheme_idx = self.scheme_row.get_selected()
        target_idx = self.target_row.get_selected()
        fs_idx = self.fs_row.get_selected()
        return {
            "image_path": self._image_path,
            "image_info": self._image_info,
            "os_type": self._os_type_index(),
            "edition_index": self.get_selected_edition_index(),
            "scheme": PartitionScheme.GPT if scheme_idx == 0 else PartitionScheme.MBR,
            "target": TargetSystem.UEFI if target_idx == 0 else TargetSystem.BIOS,
            "filesystem": (
                FileSystem[self._fs_names[fs_idx]]
                if 0 <= fs_idx < len(self._fs_names) else FileSystem.FAT32
            ),
            "volume_label": (self.label_row.get_text() or "YAKU")[:11],
            "cluster_size": self.cluster_row.get_selected(),
            "write_mode": self.mode_row.get_selected(),  # 0=DD, 1=Extract
        }

    # ------------------------------------------------------------- internals

    def _os_type_index(self) -> int:
        """Map the detected image type onto the summary's list order."""
        if not self._image_info:
            return 3
        mapping = {
            ImageType.WINDOWS_INSTALLER: 0,
            ImageType.LINUX_ISHYBRID: 1,
            ImageType.LINUX_INSTALLER: 2,
            ImageType.CHROMEOS: 4,
            ImageType.MACOS: 5,
            ImageType.BSD_UNIX: 6,
        }
        return mapping.get(self._image_info.image_type, 3)

    def _init_dropdowns(self):
        """Populate the ComboRows (models are set in code, not .ui)."""
        from yaku.core.images import get_supported_filesystems

        self.scheme_row.set_model(Gtk.StringList.new([
            _("GPT (UEFI)"), _("MBR (BIOS/CSM)"),
        ]))
        self.target_row.set_model(Gtk.StringList.new([
            _("UEFI (non-CSM)"), _("BIOS (CSM/Legacy)"),
        ]))

        supported = get_supported_filesystems()
        self._fs_names = [fs.name for fs in supported]
        self.fs_row.set_model(Gtk.StringList.new(self._fs_names))

        self._update_cluster_row(FileSystem.FAT32)

        self.mode_row.set_model(Gtk.StringList.new([
            _("DD Image Mode (block copy)"),
            _("ISO Image Mode (file extraction)"),
        ]))

    def _update_cluster_row(self, fs: FileSystem):
        """Refresh the cluster size row for the current filesystem."""
        options = CLUSTER_OPTIONS.get(fs, [_("Default")])
        self.cluster_row.set_model(Gtk.StringList.new(options))
        self.cluster_row.set_selected(0)

    # ------------------------------------------------------- file choosing

    def _on_download_clicked(self, _btn):
        dialog = IsoDownloadDialog(on_iso_ready=self._on_downloaded_iso)
        dialog.present(self.widget)

    def _on_downloaded_iso(self, path: str):
        if path and os.path.exists(path):
            self.set_image(path)

    def _on_browse_clicked(self, _btn):
        """Open the native GTK4 file dialog."""
        dialog = Gtk.FileDialog.new()
        dialog.set_title(_("Select Bootable Image"))

        filters = Gio.ListStore.new(Gtk.FileFilter)

        iso_filter = Gtk.FileFilter()
        iso_filter.set_name(_("ISO Images (*.iso)"))
        iso_filter.add_suffix("iso")
        filters.append(iso_filter)

        img_filter = Gtk.FileFilter()
        img_filter.set_name(_("Disk Images (*.img, *.dd, *.raw)"))
        for ext in ("img", "dd", "raw", "bin"):
            img_filter.add_suffix(ext)
        filters.append(img_filter)

        dmg_filter = Gtk.FileFilter()
        dmg_filter.set_name(_("Apple Disk Images (*.dmg)"))
        dmg_filter.add_suffix("dmg")
        filters.append(dmg_filter)

        all_filter = Gtk.FileFilter()
        all_filter.set_name(_("All Files"))
        all_filter.add_pattern("*")
        filters.append(all_filter)

        dialog.set_filters(filters)
        dialog.set_default_filter(iso_filter)

        parent = self._window if self._window is not None else self.widget.get_root()
        dialog.open(parent, None, self._on_file_selected)

    def _on_file_selected(self, dialog, result):
        try:
            file = dialog.open_finish(result)
        except GLib.Error:
            return
        if file is not None:
            self.set_image(file.get_path())

    def _on_drop(self, _target, value, _x, _y):
        """Accept a dropped ISO/image file."""
        files = value.get_files() if value is not None else []
        if len(files) == 1:
            path = files[0].get_path()
            if path and path.lower().endswith(IMAGE_EXTENSIONS):
                self.set_image(path)
                return True
        return False

    def _on_drag_enter(self, _target, _x, _y):
        self.widget.add_css_class("drop-active")
        return Gdk.DragAction.COPY

    def _on_drag_leave(self, _target):
        self.widget.remove_css_class("drop-active")

    def _on_label_changed(self, _entry):
        """FAT32 labels are limited to 11 characters (uppercased like Rufus)."""
        text = self.label_row.get_text()
        if len(text) > 11:
            self.label_row.set_text(text[:11])

    # ---------------------------------------------------------- analysis

    def _analyze_worker(self, path: str):
        try:
            info = analyze_image(path)
            GLib.idle_add(self._analysis_complete, info)
        except Exception as e:  # noqa: BLE001 - reported in the banner
            log.exception("Image analysis failed for %s", path)
            GLib.idle_add(self._analysis_error, str(e))

    def _analysis_complete(self, info: ImageInfo):
        """Populate the page from a finished analysis."""
        self._analyzing = False
        self._image_info = info

        self.image_row.set_title(os.path.basename(info.path))
        self.image_row.set_subtitle(f"{info.image_type.value} · {format_size(info.size)}")

        self.detected_row.set_title(_("Detected Image"))
        detected = [_(info.image_type.value)]
        if info.os_name:
            detected.insert(0, info.os_name)
        if info.label:
            detected.append(_("Label: {label}").format(label=info.label))
        self.detected_row.set_subtitle(" · ".join(detected))

        if info.boot_warning:
            self.warning_banner.set_title(info.boot_warning)
            self.warning_banner.set_revealed(True)
        else:
            self.warning_banner.set_revealed(False)

        self._apply_suggestions(info)
        self._apply_write_mode(info)

        # Windows ISOs: list editions for the Windows To Go path
        if info.image_type == ImageType.WINDOWS_INSTALLER:
            self.edition_row.set_visible(True)
            self._load_windows_editions()
        else:
            self.edition_row.set_visible(False)
            self._editions = []
            self._notify_ready()

    def _apply_write_mode(self, info: ImageInfo):
        """Lock the writing mode for the formats that only have one."""
        if info.image_type == ImageType.CHROMEOS:
            # A recovery image is a raw disk image, it is copied as a whole.
            self.mode_row.set_selected(0)
            self.mode_row.set_sensitive(False)
            self.mode_row.set_subtitle(
                _("Raw block copy (the only mode for ChromeOS)")
            )
        elif info.image_type == ImageType.MACOS:
            self.mode_row.set_sensitive(False)
            self.mode_row.set_subtitle(
                _("Apple partition layout (EFI + HFS+)")
            )
        elif info.image_type == ImageType.BSD_UNIX:
            # The BSD images carry their own partition table and are copied
            # as a whole, exactly like an isohybrid ISO.
            self.mode_row.set_selected(0)
            self.mode_row.set_sensitive(False)
            self.mode_row.set_subtitle(
                _("Raw block copy (the only mode for BSD and Unix images)")
            )
        else:
            self.mode_row.set_sensitive(True)
            self.mode_row.set_subtitle("")

    def _analysis_error(self, error: str):
        """Show a banner and fall back to the chooser state."""
        self._analyzing = False
        self._image_info = None
        self.error_banner.set_title(_("This image could not be analyzed: {error}").format(error=error))
        self.error_banner.set_revealed(True)
        self.state_stack.set_visible_child_name("empty")

    def _load_windows_editions(self):
        if not self._image_path:
            return

        def worker():
            try:
                editions = list_windows_editions(self._image_path)
                GLib.idle_add(self._editions_loaded, editions)
            except Exception:  # noqa: BLE001 - empty list is a valid result
                GLib.idle_add(self._editions_loaded, [])

        threading.Thread(target=worker, daemon=True).start()

    def _editions_loaded(self, editions: list):
        self._editions = editions
        if editions:
            names = [name for _, name, _ in editions]
            self.edition_row.set_model(Gtk.StringList.new(names))
            self.edition_row.set_selected(0)
            self.edition_row.set_visible(True)
        else:
            self.edition_row.set_visible(False)
        self._notify_ready()

    def _notify_ready(self):
        """Let the window re-evaluate the 'Next' button."""
        if self.on_analysis_complete and self._image_info:
            self.on_analysis_complete(self._image_info)

    def _apply_suggestions(self, info: ImageInfo):
        """Apply the analysis suggestions to the option rows."""
        if info is None:
            return

        self.scheme_row.set_selected(0 if info.suggested_scheme == PartitionScheme.GPT else 1)
        self.target_row.set_selected(0 if info.suggested_target == TargetSystem.UEFI else 1)

        fs_index = self._fs_names.index(info.suggested_fs.name) if info.suggested_fs.name in self._fs_names else 0
        self.fs_row.set_selected(fs_index)
        self._update_cluster_row(info.suggested_fs)

        self.mode_row.set_selected(0 if info.image_type == ImageType.LINUX_ISHYBRID else 1)

        if info.label:
            self.label_row.set_text(info.label[:11].upper())

    def _on_scheme_changed(self, combo, _pspec):
        """Keep target/filesystem consistent with the partition scheme."""
        is_gpt = combo.get_selected() == 0
        if is_gpt:
            self.target_row.set_selected(0)
            self.fs_row.set_selected(0)
        else:
            self.target_row.set_selected(1)
            if self._image_info and self._image_info.image_type == ImageType.WINDOWS_INSTALLER:
                self.fs_row.set_selected(1 if len(self._fs_names) > 1 else 0)

        fs_idx = self.fs_row.get_selected()
        fs = (
            FileSystem[self._fs_names[fs_idx]]
            if 0 <= fs_idx < len(self._fs_names) else FileSystem.FAT32
        )
        self._update_cluster_row(fs)
