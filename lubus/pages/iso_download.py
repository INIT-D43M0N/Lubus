"""Download Linux ISO dialog for Step 2.

A catalog -> branch -> edition flow with mirror resolution, segmented
download, SHA-256 verification, pause/resume/cancel and a configurable cache
folder.  On success the downloaded ISO is handed back to the image page.
"""
import os
import threading

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib, Gio

from lubus.config import APP_CONFIG, save_config
from lubus.utils.i18n import _
from lubus.core.iso_catalog import IsoCatalog, CatalogError
from lubus.core.iso_downloader import IsoDownload
from lubus.utils.logging import get_logger

log = get_logger(__name__)

DEFAULT_CACHE = os.path.join(os.path.expanduser("~"), "Downloads", "Lubus ISOs")


class IsoDownloadDialog(Adw.Dialog):
    """Adw.Dialog with catalog, detail and download pages."""

    def __init__(self, on_iso_ready, catalog=None, **kwargs):
        super().__init__(**kwargs)
        self._on_iso_ready = on_iso_ready or (lambda _p: None)
        self._catalog = catalog or IsoCatalog.load()
        self._current_distro = None
        self._current_branch = None
        self._current_edition = None
        self._download = None
        self._paused = False
        self._cache_dir = APP_CONFIG.get("iso_download_dir") or DEFAULT_CACHE
        os.makedirs(self._cache_dir, exist_ok=True)

        self.set_title(_("Download Linux ISO"))
        self.set_content_width(760)
        self.set_content_height(640)
        self._build_ui()
        self.connect("closed", self._on_closed)

    # ---------------------------------------------------------------- UI

    def _build_ui(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_child(root)

        header = Adw.HeaderBar()
        self._header = header
        self._title = Adw.WindowTitle(title=_("Download Linux ISO"))
        header.set_title_widget(self._title)

        self._back_btn = Gtk.Button(icon_name="go-previous-symbolic")
        self._back_btn.set_tooltip_text(_("Back"))
        self._back_btn.set_visible(False)
        self._back_btn.connect("clicked", self._on_back_clicked)
        header.pack_start(self._back_btn)

        root.append(header)

        self._stack = Gtk.Stack(
            transition_type=Gtk.StackTransitionType.CROSSFADE,
            vexpand=True,
        )
        root.append(self._stack)
        self._stack.add_named(self._build_catalog_page(), "catalog")
        self._stack.add_named(self._build_detail_page(), "detail")
        self._stack.add_named(self._build_download_page(), "download")
        self._populate_catalog("")

    def _build_catalog_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)

        search_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._search = Gtk.SearchEntry(
            placeholder_text=_("Search distributions, editions or categories…")
        )
        self._search.set_hexpand(True)
        self._search.connect("search-changed", lambda *_a: self._populate_catalog(
            self._search.get_text()
        ))
        search_row.append(self._search)
        self._refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic")
        self._refresh_btn.set_tooltip_text(_("Refresh catalog from the remote source"))
        self._refresh_btn.connect("clicked", self._on_refresh_catalog)
        search_row.append(self._refresh_btn)
        box.append(search_row)

        self._catalog_status = Gtk.Label(halign=Gtk.Align.START)
        self._catalog_status.add_css_class("caption")
        self._catalog_status.add_css_class("dim-label")
        box.append(self._catalog_status)

        self._distro_list = Gtk.ListBox()
        self._distro_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._distro_list.add_css_class("boxed-list")
        self._distro_list.connect("row-activated", self._on_distro_activated)
        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        scroller.set_has_frame(False)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self._distro_list)
        box.append(scroller)
        return box

    def _build_detail_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(20)
        box.set_margin_end(20)

        self._detail_title = Gtk.Label(halign=Gtk.Align.START, wrap=True)
        self._detail_title.add_css_class("title-1")
        box.append(self._detail_title)

        self._detail_desc = Gtk.Label(halign=Gtk.Align.START, wrap=True)
        self._detail_desc.add_css_class("dim-label")
        box.append(self._detail_desc)

        rows = Gtk.ListBox()
        rows.add_css_class("boxed-list")
        self._branch_row = Adw.ComboRow(title=_("Branch / Release"))
        self._branch_row.connect("notify::selected", self._on_branch_changed)
        rows.append(self._branch_row)
        self._edition_row = Adw.ComboRow(title=_("Edition / Desktop"))
        self._edition_row.connect("notify::selected", self._on_edition_changed)
        rows.append(self._edition_row)
        box.append(rows)

        self._meta_label = Gtk.Label(halign=Gtk.Align.START, wrap=True)
        self._meta_label.add_css_class("caption")
        self._meta_label.add_css_class("dim-label")
        box.append(self._meta_label)

        self._url_label = Gtk.Label(halign=Gtk.Align.START, wrap=True)
        self._url_label.add_css_class("caption")
        self._url_label.add_css_class("dim-label")
        self._url_label.set_selectable(True)
        box.append(self._url_label)

        self._cache_row = Adw.ActionRow(
            title=_("Download Folder"), subtitle=self._cache_dir
        )
        choose = Gtk.Button(label=_("Choose…"), valign=Gtk.Align.CENTER)
        choose.add_css_class("flat")
        choose.connect("clicked", self._on_choose_folder)
        self._cache_row.add_suffix(choose)
        cache_box = Gtk.ListBox()
        cache_box.add_css_class("boxed-list")
        cache_box.append(self._cache_row)
        box.append(cache_box)

        self._detail_status = Gtk.Label(halign=Gtk.Align.START, wrap=True)
        self._detail_status.add_css_class("caption")
        self._detail_status.add_css_class("dim-label")
        box.append(self._detail_status)

        self._download_btn = Gtk.Button(label=_("Download ISO"))
        self._download_btn.add_css_class("pill")
        self._download_btn.add_css_class("suggested-action")
        self._download_btn.set_halign(Gtk.Align.END)
        self._download_btn.connect("clicked", self._on_download_clicked)
        box.append(self._download_btn)
        return box

    def _build_download_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.set_margin_top(24)
        box.set_margin_bottom(24)
        box.set_margin_start(28)
        box.set_margin_end(28)

        self._download_title = Gtk.Label(halign=Gtk.Align.START, wrap=True)
        self._download_title.add_css_class("title-2")
        box.append(self._download_title)

        self._download_subtitle = Gtk.Label(halign=Gtk.Align.START, wrap=True)
        self._download_subtitle.add_css_class("dim-label")
        box.append(self._download_subtitle)

        self._progress = Gtk.ProgressBar(show_text=False)
        box.append(self._progress)

        self._progress_stats = Gtk.Label(halign=Gtk.Align.START)
        self._progress_stats.add_css_class("caption")
        self._progress_stats.add_css_class("dim-label")
        box.append(self._progress_stats)

        self._download_status = Gtk.Label(halign=Gtk.Align.START, wrap=True)
        self._download_status.add_css_class("caption")
        self._download_status.add_css_class("dim-label")
        box.append(self._download_status)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        actions.set_halign(Gtk.Align.END)
        self._pause_btn = Gtk.Button(label=_("Pause"))
        self._pause_btn.add_css_class("pill")
        self._pause_btn.connect("clicked", self._on_pause_resume)
        actions.append(self._pause_btn)
        self._cancel_btn = Gtk.Button(label=_("Cancel"))
        self._cancel_btn.add_css_class("pill")
        self._cancel_btn.add_css_class("destructive-action")
        self._cancel_btn.connect("clicked", self._on_cancel)
        actions.append(self._cancel_btn)
        box.append(actions)
        return box

    # -------------------------------------------------------------- catalog

    def _populate_catalog(self, query: str):
        child = self._distro_list.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._distro_list.remove(child)
            child = nxt

        for distro in self._catalog.search(query):
            row = Adw.ActionRow(
                title=distro.get("name", distro.get("id", "")),
                subtitle=self._distro_subtitle(distro),
                activatable=True,
            )
            row.distro = distro
            badge = Gtk.Box()
            badge.add_css_class("distro-badge")
            badge.set_size_request(38, 38)
            badge.set_valign(Gtk.Align.CENTER)
            label = Gtk.Label(label=distro.get("name", "?")[:1].upper())
            badge.append(label)
            row.add_prefix(badge)
            self._distro_list.append(row)

    @staticmethod
    def _distro_subtitle(distro):
        tags = distro.get("tags") or [distro.get("category", "")]
        branch_count = len(distro.get("branches", []))
        return f"{' · '.join(tags[:3])}  —  {branch_count} release(s)"

    def _on_refresh_catalog(self, _btn):
        self._refresh_btn.set_sensitive(False)
        self._catalog_status.set_label(_("Refreshing catalog…"))
        threading.Thread(target=self._refresh_catalog_worker, daemon=True).start()

    def _refresh_catalog_worker(self):
        try:
            catalog = IsoCatalog.load(force=True)
            GLib.idle_add(self._catalog_refreshed, catalog, None)
        except Exception as exc:  # noqa: BLE001
            GLib.idle_add(self._catalog_refreshed, None, str(exc))

    def _catalog_refreshed(self, catalog, error):
        self._refresh_btn.set_sensitive(True)
        if error:
            self._catalog_status.set_label(
                _("Remote catalog unavailable; using local fallback.")
            )
            return False
        self._catalog = catalog
        self._catalog_status.set_label(
            _("Catalog updated from {source}").format(source=catalog.source)
        )
        self._populate_catalog(self._search.get_text())
        return False

    def _on_distro_activated(self, _listbox, row):
        self._current_distro = getattr(row, "distro", None)
        if not self._current_distro:
            return
        self._detail_title.set_label(self._current_distro.get("name", ""))
        self._detail_desc.set_label(self._current_distro.get("description", ""))
        branches = self._current_distro.get("branches", [])
        self._branch_row.set_model(Gtk.StringList.new([
            b.get("name", "") for b in branches
        ]))
        self._branch_row.set_selected(0)
        self._populate_editions(0)
        self._detail_status.set_label("")
        self._back_btn.set_visible(True)
        self._stack.set_visible_child_name("detail")

    def _populate_editions(self, branch_index):
        if not self._current_distro:
            return
        branches = self._current_distro.get("branches", [])
        if not (0 <= branch_index < len(branches)):
            return
        self._current_branch = branches[branch_index]
        editions = self._current_branch.get("editions", [])
        self._edition_row.set_model(Gtk.StringList.new([
            e.get("name", "") for e in editions
        ]))
        self._edition_row.set_selected(0)
        self._populate_meta(0)

    def _on_branch_changed(self, row, _pspec):
        self._populate_editions(row.get_selected())

    def _on_edition_changed(self, row, _pspec):
        self._populate_meta(row.get_selected())

    def _populate_meta(self, edition_index):
        if not self._current_branch:
            return
        editions = self._current_branch.get("editions", [])
        if not (0 <= edition_index < len(editions)):
            return
        self._current_edition = editions[edition_index]
        e = self._current_edition
        parts = [
            e.get("status", self._current_branch.get("status", "")),
            e.get("arch", "x86_64"),
        ]
        if e.get("desktop"):
            parts.append(e["desktop"])
        if e.get("firmware"):
            parts.append(f"firmware: {e['firmware']}")
        self._meta_label.set_label(" · ".join(p for p in parts if p))
        self._url_label.set_label(e.get("base_url") or e.get("listing_url")
                                  or e.get("url") or "")

    # ------------------------------------------------------------- download

    def _on_download_clicked(self, _btn):
        if not self._current_distro or not self._current_branch or not self._current_edition:
            return
        self._download_btn.set_sensitive(False)
        self._detail_status.set_label(_("Resolving official mirror…"))
        threading.Thread(
            target=self._resolve_worker,
            args=(self._current_distro, self._current_branch, self._current_edition),
            daemon=True,
        ).start()

    def _resolve_worker(self, distro, branch, edition):
        try:
            resolved = self._catalog.resolve(distro, branch, edition)
            GLib.idle_add(self._start_download, resolved)
        except Exception as exc:  # noqa: BLE001
            log.warning("ISO mirror resolution failed: %s", exc)
            GLib.idle_add(self._resolve_failed, str(exc))

    def _resolve_failed(self, message):
        self._detail_status.set_label(
            _("Could not resolve a mirror: {error}").format(error=message)
        )
        self._download_btn.set_sensitive(True)
        return False

    def _start_download(self, resolved):
        self._download_btn.set_sensitive(True)
        self._download_title.set_label(resolved.filename)
        self._download_subtitle.set_label(
            f"{resolved.distro} · {resolved.branch} · {resolved.edition}"
        )
        self._url_label.set_label(resolved.url)
        self._progress.set_fraction(0.0)
        self._progress_stats.set_label("")
        self._download_status.set_label(_("Starting download…"))
        self._pause_btn.set_label(_("Pause"))
        self._paused = False
        self._pause_btn.set_sensitive(True)
        self._cancel_btn.set_sensitive(True)
        self._download = IsoDownload(
            resolved.url,
            self._cache_dir,
            resolved.filename,
            resolved.sha256,
            checksum_url=resolved.checksum_url,
            on_progress=lambda d, t, s, e: GLib.idle_add(
                self._update_progress, d, t, s, e
            ),
            on_status=lambda msg: GLib.idle_add(self._set_download_status, msg),
            on_done=lambda path: GLib.idle_add(self._download_done, path),
            on_error=lambda msg: GLib.idle_add(self._download_error, msg),
            on_cancelled=lambda: GLib.idle_add(self._download_cancelled),
        )
        self._stack.set_visible_child_name("download")
        self._download.start()
        return False

    def _update_progress(self, done, total, speed, eta):
        if total <= 0:
            return False
        self._progress.set_fraction(min(1.0, done / total))
        self._progress_stats.set_label(
            f"{_format_size(done)} / {_format_size(total)}"
            f"   ·   {_format_size(speed)}/s"
            + (f"   ·   ETA {_format_eta(eta)}" if eta > 0 else "")
        )
        return False

    def _set_download_status(self, message):
        self._download_status.set_label(message)
        return False

    def _download_done(self, path):
        self._progress.set_fraction(1.0)
        self._download_status.set_label(_("Verified. Selecting image…"))
        self._pause_btn.set_sensitive(False)
        self._cancel_btn.set_sensitive(False)
        GLib.timeout_add(350, lambda: self._deliver(path))
        return False

    def _deliver(self, path):
        self._on_iso_ready(path)
        self.close()
        return False

    def _download_error(self, message):
        self._download_status.set_label(
            _("Download failed: {error}").format(error=message)
        )
        self._pause_btn.set_sensitive(False)
        self._cancel_btn.set_sensitive(True)
        return False

    def _download_cancelled(self):
        self._download_status.set_label(_("Download cancelled."))
        self._pause_btn.set_sensitive(False)
        self._cancel_btn.set_sensitive(False)
        return False

    def _on_closed(self, *_args):
        if self._download and self._download.running:
            self._download.cancel()

    def _on_pause_resume(self, _btn):
        if not self._download:
            return
        if self._paused:
            self._download.resume()
            self._paused = False
            self._pause_btn.set_label(_("Pause"))
        else:
            self._download.pause()
            self._paused = True
            self._pause_btn.set_label(_("Resume"))

    def _on_cancel(self, _btn):
        if self._download:
            self._download.cancel()

    def _on_back_clicked(self, _btn):
        if self._stack.get_visible_child_name() == "download":
            # Do not let navigation abandon an active download silently.
            if self._download and self._download.running:
                return
        self._back_btn.set_visible(False)
        self._stack.set_visible_child_name("catalog")

    def _on_choose_folder(self, _btn):
        dialog = Gtk.FileDialog(title=_("Select ISO download folder"))
        parent = self.get_root()
        try:
            dialog.select_folder(parent, None, self._on_folder_selected)
        except Exception as exc:  # noqa: BLE001
            self._detail_status.set_label(str(exc))

    def _on_folder_selected(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
            path = folder.get_path()
        except GLib.Error:
            return
        if path:
            self._cache_dir = path
            self._cache_row.set_subtitle(path)
            APP_CONFIG["iso_download_dir"] = path
            try:
                save_config(APP_CONFIG)
            except OSError:
                pass


def _format_size(value: float) -> str:
    if value >= 1024**3:
        return f"{value / 1024**3:.2f} GiB"
    if value >= 1024**2:
        return f"{value / 1024**2:.1f} MiB"
    if value >= 1024:
        return f"{value / 1024:.0f} KiB"
    return f"{value:.0f} B"


def _format_eta(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
