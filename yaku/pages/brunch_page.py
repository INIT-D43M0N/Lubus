"""ChromeOS options page (Step 3 of the wizard).

Shown when the selected image is a ChromeOS recovery image. It lets the
user decide between a plain copy of the image and a Brunch install, which
turns the drive into a ChromeOS that boots on an ordinary computer.

The Brunch files (its kernel and bootloaders) are about 900 MB. They are
downloaded once, on demand, and then kept in the user's data directory.
"""
import threading

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib

from yaku.utils.i18n import _
from yaku.core.brunch import (
    BRUNCH_URL, BrunchAssets, BrunchError, default_assets_dir,
)
from yaku.pages.base import UiPage

# The two ways a ChromeOS image can be written.
MODE_RECOVERY = 0
MODE_BRUNCH = 1

# The two boot modes Brunch supports.
BOOT_SECURE = 0
BOOT_LEGACY = 1


class BrunchPage(UiPage):
    """Page with the ChromeOS / Brunch options."""

    ui_name = "brunch_page.ui"
    root_id = "root"

    def __init__(self, on_options_changed=None, **kwargs):
        super().__init__()
        self.on_options_changed = on_options_changed

        self.mode_row = self.get("mode_row")
        self.boot_row = self.get("boot_row")
        self.brunch_group = self.get("brunch_group")
        self.assets_row = self.get("assets_row")
        self.download_button = self.get("download_button")
        self.project_row = self.get("project_row")
        self.download_group = self.get("download_group")
        self.progress_bar = self.get("progress_bar")
        self.cancel_button = self.get("cancel_button")

        self.mode_row.set_model(Gtk.StringList.new([
            _("ChromeOS recovery image (copy as-is)"),
            _("ChromeOS with Brunch (run on a regular PC)"),
        ]))
        self.boot_row.set_model(Gtk.StringList.new([
            _("Secure Boot"),
            _("Legacy boot (without Secure Boot)"),
        ]))

        self._assets = BrunchAssets(default_assets_dir())
        self._downloading = False
        self._cancel_requested = False
        self._assets_ready = self._assets.is_available()

        self.mode_row.connect("notify::selected", self._on_mode_changed)
        self.boot_row.connect("notify::selected", self._on_options_changed)
        self.download_button.connect("clicked", self._on_download_clicked)
        self.cancel_button.connect("clicked", self._on_cancel_clicked)
        self.project_row.connect("activated", self._on_project_clicked)

        self._refresh_assets_row()

    # ---------------------------------------------------------------- public

    def get_options(self) -> dict:
        """The ChromeOS options chosen by the user."""
        return {
            "use_brunch": self.mode_row.get_selected() == MODE_BRUNCH,
            "legacy_boot": self.boot_row.get_selected() == BOOT_LEGACY,
            "assets_dir": self._assets.directory,
        }

    def is_ready(self) -> bool:
        """Whether the chosen mode can be written right now."""
        if self.mode_row.get_selected() != MODE_BRUNCH:
            return True
        return self._assets_ready and not self._downloading

    def set_mode_brunch(self, enabled: bool):
        """Select the Brunch mode (or the plain recovery image)."""
        self.mode_row.set_selected(MODE_BRUNCH if enabled else MODE_RECOVERY)

    def refresh_assets(self):
        """Check whether the Brunch files are already on disk."""
        self._assets_ready = self._assets.is_available()
        self._refresh_assets_row()

    # ------------------------------------------------------------- internals

    def _on_mode_changed(self, *_args):
        use_brunch = self.mode_row.get_selected() == MODE_BRUNCH
        self.brunch_group.set_visible(use_brunch)
        self._on_options_changed()

    def _on_options_changed(self, *_args):
        if self.on_options_changed:
            self.on_options_changed(self.get_options())

    def _refresh_assets_row(self):
        """Show the state of the Brunch files."""
        if self._assets_ready:
            self.assets_row.set_subtitle(
                _("The Brunch files are ready on this computer.")
            )
            self.download_button.set_label(_("Download again…"))
            self.download_button.set_sensitive(not self._downloading)
        else:
            missing = ", ".join(self._assets.missing_files())
            self.assets_row.set_subtitle(
                _("Not downloaded yet ({files}). The download is about "
                  "900 MB and is kept for the next time.").format(files=missing)
            )
            self.download_button.set_label(_("Download…"))
            self.download_button.set_sensitive(not self._downloading)

    def _on_download_clicked(self, _btn):
        if self._downloading:
            return
        self._downloading = True
        self._cancel_requested = False
        self.download_group.set_visible(True)
        self.progress_bar.set_fraction(0)
        self.progress_bar.set_text(_("Starting…"))
        self._refresh_assets_row()
        threading.Thread(target=self._download_worker, daemon=True).start()

    def _on_cancel_clicked(self, _btn):
        self._cancel_requested = True

    def _download_worker(self):
        """Download the Brunch files (never touches widgets directly)."""
        def progress(done, total):
            GLib.idle_add(self._on_progress, done, total)

        def stopped():
            return self._cancel_requested

        try:
            self._assets.download(progress=progress, stopped=stopped)
        except BrunchError as exc:
            GLib.idle_add(self._on_download_failed, str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - shown in the page
            GLib.idle_add(self._on_download_failed, str(exc))
            return
        GLib.idle_add(self._on_download_done)

    def _on_progress(self, done, total):
        if total:
            self.progress_bar.set_fraction(min(1.0, done / total))
            self.progress_bar.set_text(f"{done / 1024**2:.0f} / "
                                       f"{total / 1024**2:.0f} MB")
        else:
            self.progress_bar.set_fraction(0)
            self.progress_bar.set_text(f"{done / 1024**2:.0f} MB")
        return False

    def _on_download_done(self):
        self._downloading = False
        self._assets_ready = True
        self.download_group.set_visible(False)
        self._refresh_assets_row()
        self._on_options_changed()

    def _on_download_failed(self, message: str):
        self._downloading = False
        self.download_group.set_visible(False)
        self._assets_ready = self._assets.is_available()
        self._refresh_assets_row()
        self.assets_row.set_subtitle(
            _("The download failed: {error}").format(error=message)
        )

    def _on_project_clicked(self, _row):
        """Open the Brunch project page in the browser."""
        from gi.repository import Gio
        launcher = Gio.AppInfo.launch_default_for_uri(BRUNCH_URL, None)
        if launcher is None:
            return
