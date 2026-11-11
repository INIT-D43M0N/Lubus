"""Main application window for Yaku (GTK4 + Libadwaita, GNOME HIG).

Structure
---------
``Adw.ApplicationWindow`` (client-side decorations, follows the system
light/dark style automatically) containing:

* ``Adw.HeaderBar`` with the wizard navigation: a symbolic back button, the
  current step title and the primary Next/Start Flashing action — HIG keeps
  flow navigation in the header bar instead of custom footer widgets.
* ``Adw.ViewStack`` with one page per wizard step, driven by ``advance()``.
* ``Adw.Breakpoint`` rules that adapt the layout on phone-sized windows
  (Phosh / mobile) without a separate UI.

Business logic (drive enumeration, image analysis, writing) lives in
``yaku.core`` and only touches this class through small callbacks.
"""
import contextlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib, Gio, Gdk

from yaku.utils.i18n import _
from yaku.config import APP_CONFIG, save_config
from yaku.utils.logging import get_logger
from yaku.utils.resources import ui_file
from yaku.core.devices import Drive, get_drive_identity, format_size
from yaku.core.images import ImageInfo, PartitionScheme, TargetSystem, FileSystem, ImageType
from yaku.core.writer import (
    copy_image_dd, write_windows_installer, write_windows_togo,
    extract_iso_files, release_stale_iso_mounts, write_macos_installer,
    write_brunch_installer,
    StoppedException, WriteError, VerifyMismatchError,
)
from yaku.core.direct_install import (
    InstallConfig, install_to_drive, validate_install_config,
)
from yaku.core.multi_flash import AggregateProgress
from yaku.core.verify import verify_device_matches_image
from yaku.udisks import (
    is_canceled_error, NotAuthorizedError, PortalPathError,
    mount_iso_readonly, get_device_size,
)
from yaku.pages import DrivePage, ImagePage, BrunchPage, SummaryPage, FlashPage

log = get_logger(__name__)

STEP_NAMES = ("drive", "image", "chromeos", "summary", "flash")

STEP_TITLES = {
    "drive": _("Select Drive"),
    "image": _("Select Image"),
    "chromeos": _("ChromeOS Options"),
    "summary": _("Summary"),
    "flash": _("Flash"),
}

# Below this width the window behaves like a phone layout (Phosh, PinePhone).
NARROW_BREAKPOINT = "max-width: 550sp"


class YakuWindow(Adw.ApplicationWindow):
    """Main application window."""

    def __init__(self, app, iso_path=None, drive_device=None,
                 windows_options=None, **kwargs):
        super().__init__(application=app, **kwargs)
        self.set_title(_("Yaku"))
        self.set_default_size(820, 760)

        # ------------------------------------------------------------ state
        self._iso_path = iso_path
        self._drive_device = drive_device
        self._selected_drive: Drive | None = None
        self._selected_drives: list[Drive] = []
        self._image_info: ImageInfo | None = None
        self._settings: dict = {}
        self._is_flashing = False
        self._worker_thread = None
        self._cancel_requested = False
        self._start_time = 0
        self._narrow = False
        self._cli_windows_options = dict(windows_options or {})

        # --------------------------------------------------------------- UI
        self._build_ui()
        self._setup_actions()
        self._apply_theme()
        self._setup_breakpoints()

        # Wire pages to their callbacks (after construction, so the widgets
        # and window references exist)
        self.drive_page.on_drive_selected = self._on_drive_selected
        self.image_page.on_image_selected = self._on_image_selected
        self.image_page.on_analysis_complete = self._on_analysis_complete
        self.image_page._window = self
        self.summary_page.on_verify_toggled = self._on_verify_toggled
        self.summary_page.on_windows_options_changed = self._on_windows_options_changed
        self.brunch_page.on_options_changed = self._on_brunch_options_changed

        # Initial data
        self.drive_page.refresh_drives()
        if drive_device:
            self.drive_page.set_selected_drive(drive_device)
        if iso_path:
            self.image_page.set_image(iso_path)

        # Auto-refresh the drive list while nothing is happening
        self._refresh_id = GLib.timeout_add_seconds(3, self._auto_refresh_drives)

    # --------------------------------------------------------------- chrome

    def _build_ui(self):
        """Build the window: toolbar, header bar, view stack and pages."""
        toolbar_view = Adw.ToolbarView()
        self.set_content(toolbar_view)

        header = Adw.HeaderBar()
        toolbar_view.add_top_bar(header)

        # Back button (header-bar navigation, GNOME HIG)
        self.back_btn = Gtk.Button(
            icon_name="go-previous-symbolic",
            tooltip_text=_("Back"),
            sensitive=False,
            visible=False,
        )
        self.back_btn.connect("clicked", self._on_back_clicked)
        header.pack_start(self.back_btn)

        # Title widget shows the current step
        self.title_widget = Adw.WindowTitle(title=STEP_TITLES["drive"])
        header.set_title_widget(self.title_widget)

        # Primary flow action
        self.next_btn = Gtk.Button(label=_("Next"))
        self.next_btn.add_css_class("suggested-action")
        self.next_btn.connect("clicked", self._on_next_clicked)
        header.pack_end(self.next_btn)

        # Primary menu (HIG: hamburger is a fallback, but a wizard fits it)
        self._menu_button = self._build_menu_button()
        header.pack_end(self._menu_button)

        # Cancel action for the flash step (shown instead of Back)
        self.cancel_btn = Gtk.Button(
            label=_("Cancel"),
            tooltip_text=_("Cancel flashing"),
            css_classes=["destructive-action"],
            visible=False,
        )
        self.cancel_btn.connect("clicked", self._on_flash_cancel)
        header.pack_start(self.cancel_btn)

        # View stack with one page per step
        self.view_stack = Adw.ViewStack()
        toolbar_view.set_content(self.view_stack)

        self.drive_page = DrivePage(on_drive_selected=self._on_drive_selected)
        self.image_page = ImagePage(
            on_image_selected=self._on_image_selected,
            on_analysis_complete=self._on_analysis_complete,
        )
        self.brunch_page = BrunchPage(
            on_options_changed=self._on_brunch_options_changed,
        )
        self.summary_page = SummaryPage(on_verify_toggled=self._on_verify_toggled)
        self.flash_page = FlashPage(
            on_done=self._on_flash_done,
            on_cancel=self._on_flash_cancel,
            on_try_again=self._on_flash_retry,
        )

        self._add_step(self.drive_page, "drive")
        self._add_step(self.image_page, "image")
        self._add_step(self.brunch_page, "chromeos")
        self._add_step(self.summary_page, "summary")
        self._add_step(self.flash_page, "flash")

        self.view_stack.connect("notify::visible-child", self._on_step_changed)

        self._current_step = 0
        self._update_step_ui()

        # Drop ISO files anywhere on the window (steps 1–2)
        drop_target = Gtk.DropTarget.new(
            Gdk.FileList, Gdk.DragAction.COPY | Gdk.DragAction.MOVE
        )
        drop_target.connect("drop", self._on_drop)
        drop_target.connect("enter", self._on_drag_enter)
        drop_target.connect("leave", self._on_drag_leave)
        self.add_controller(drop_target)

        self.connect("close-request", self._on_close_request)

    def _add_step(self, page, name: str):
        """Register a page in the view stack with its step title."""
        self.view_stack.add_titled_with_icon(
            page.get_widget(), name, STEP_TITLES[name], self._step_icon(name)
        )

    @staticmethod
    def _step_icon(name: str) -> str:
        return {
            "drive": "drive-removable-media-symbolic",
            "image": "media-optical-symbolic",
            "chromeos": "computer-symbolic",
            "summary": "view-list-symbolic",
            "flash": "media-flash-symbolic",
        }[name]

    def _build_menu_button(self) -> Gtk.MenuButton:
        """Primary menu: Preferences, Shortcuts, About."""
        menu = Gio.Menu.new()
        menu.append(_("Preferences"), "win.preferences")
        menu.append(_("Keyboard Shortcuts"), "win.shortcuts")
        menu.append(_("About Yaku"), "win.about")
        return Gtk.MenuButton(
            icon_name="open-menu-symbolic",
            menu_model=menu,
            tooltip_text=_("Main Menu"),
        )

    def _setup_breakpoints(self):
        """Adapt the layout on phone-sized windows."""
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(NARROW_BREAKPOINT))
        narrow.connect("apply", self._on_narrow_apply)
        narrow.connect("unapply", self._on_narrow_unapply)
        self.add_breakpoint(narrow)

    def _on_narrow_apply(self, _breakpoint):
        """Phone layout: tighten the column so content stays readable."""
        self._narrow = True
        self.flash_page.column_clamp.set_maximum_size(320)

    def _on_narrow_unapply(self, _breakpoint):
        self._narrow = False
        self.flash_page.column_clamp.set_maximum_size(420)

    # -------------------------------------------------------------- actions

    def _setup_actions(self):
        """Window-scoped actions (menu entries and accelerators)."""
        specs = [
            ("about", self._show_about),
            ("preferences", self._show_preferences),
            ("shortcuts", self._show_shortcuts),
            ("refresh", self._on_refresh_action),
            ("choose-image", self._on_choose_image_action),
        ]
        for name, callback in specs:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)

    def _apply_theme(self):
        """Apply the saved style preference (system = automatic)."""
        theme_idx = APP_CONFIG.get("theme", 0)
        style_mgr = Adw.StyleManager.get_default()
        if theme_idx == 1:
            style_mgr.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
        elif theme_idx == 2:
            style_mgr.set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        else:
            style_mgr.set_color_scheme(Adw.ColorScheme.DEFAULT)

    # ----------------------------------------------------------- navigation

    def _on_step_changed(self, _stack, _pspec):
        """Sync the header bar when the stack's visible page changes."""
        visible = self.view_stack.get_visible_child_name()
        if visible in STEP_NAMES:
            self._current_step = STEP_NAMES.index(visible)
        self._update_step_ui()

    def _is_chromeos(self) -> bool:
        """Whether the selected image is a ChromeOS recovery image."""
        return bool(
            self._image_info is not None
            and self._image_info.image_type == ImageType.CHROMEOS
        )

    def _visible_steps(self) -> tuple:
        """The steps shown for the current image.

        The ChromeOS step only exists when a ChromeOS recovery image is
        selected, so the step counter does not skip a number.
        """
        steps = ["drive", "image"]
        if self._is_chromeos():
            steps.append("chromeos")
        steps.extend(["summary", "flash"])
        return tuple(steps)

    def _update_step_ui(self):
        """Refresh header bar + page state for the current step."""
        name = STEP_NAMES[self._current_step]
        self.view_stack.set_visible_child_name(name)
        self.title_widget.set_title(STEP_TITLES[name])
        steps = self._visible_steps()
        position = steps.index(name) + 1 if name in steps else self._current_step + 1
        self.title_widget.set_subtitle(
            _("Step {n} of {total}").format(n=position, total=len(steps))
        )

        flashing = name == "flash"
        self.back_btn.set_visible(not flashing and self._current_step > 0)
        self.back_btn.set_sensitive(not flashing and self._current_step > 0)
        self.cancel_btn.set_visible(flashing and self._is_flashing)

        if flashing:
            self.next_btn.set_visible(not self._is_flashing)
            self.next_btn.set_label(_("Done"))
            self.next_btn.set_sensitive(not self._is_flashing)
            return

        if name == "drive":
            self.drive_page.set_image_type(
                self._image_info.image_type if self._image_info else None
            )
            self.next_btn.set_visible(True)
            self.next_btn.set_label(_("Next"))
            self.next_btn.set_sensitive(
                bool(self.drive_page.get_selected_drives())
            )

        elif name == "image":
            self.next_btn.set_visible(True)
            self.next_btn.set_label(_("Next"))
            self.next_btn.set_sensitive(
                self._iso_path is not None
                and not self.image_page.is_analyzing()
                and self.image_page.is_ready_for_next_step()
            )

        elif name == "chromeos":
            self.next_btn.set_visible(True)
            self.next_btn.set_label(_("Next"))
            self.next_btn.set_sensitive(self.brunch_page.is_ready())

        elif name == "summary":
            self.next_btn.set_visible(True)
            self.next_btn.set_label(_("Flash Now") if self._narrow else _("Start Flashing"))
            self.next_btn.set_sensitive(True)
            self._update_summary()

    def _on_back_clicked(self, _btn):
        if self._current_step > 0:
            self._current_step -= 1
            # The ChromeOS step only exists for ChromeOS images.
            if (STEP_NAMES[self._current_step] == "chromeos"
                    and not self._is_chromeos()):
                self._current_step -= 1
            self._update_step_ui()

    def _on_next_clicked(self, _btn):
        name = STEP_NAMES[self._current_step]
        if name == "flash":
            # Done after flashing: start a new round
            self._on_flash_done()
            return

        if name == "drive":
            self._selected_drives = self.drive_page.get_selected_drives()
            self._selected_drive = self.drive_page.get_selected_drive()
            if not self._selected_drives:
                return
            self._settings["drives"] = self._selected_drives
            self._settings["drive"] = self._selected_drive
            self._settings["install_config"] = self.drive_page.get_install_config()
            self._current_step = 1
            self._update_step_ui()

        elif name == "image":
            if self._iso_path and self.image_page.is_ready_for_next_step():
                self._current_step = 2 if self._is_chromeos() else 3
                self._update_step_ui()

        elif name == "chromeos":
            self._current_step = 3
            self._update_step_ui()

        elif name == "summary":
            self._start_flashing()

    def _on_drive_selected(self, drive: Drive):
        self._selected_drive = drive
        self._selected_drives = self.drive_page.get_selected_drives()
        if not self._selected_drives and drive is not None:
            self._selected_drives = [drive]
        self._update_step_ui()

    def _on_image_selected(self, path: str):
        self._iso_path = path
        self._update_step_ui()

    def _on_analysis_complete(self, info: ImageInfo):
        if info is None:
            return
        self._image_info = info
        self.drive_page.set_image_type(info.image_type)
        self._settings["image_path"] = info.path
        self._settings["image_info"] = info
        if info.image_type == ImageType.CHROMEOS:
            # The Brunch files may already be on disk from an earlier run.
            self.brunch_page.refresh_assets()
        self._update_step_ui()

    def _on_brunch_options_changed(self, options: dict):
        """The ChromeOS / Brunch options changed."""
        self._settings["brunch"] = dict(options)
        self._update_step_ui()

    def _on_verify_toggled(self, enabled: bool):
        self._settings["verify"] = enabled

    def _on_windows_options_changed(self, options: dict):
        self._settings["windows_options"] = dict(options)

    def _update_summary(self):
        """Push the current settings into the summary page."""
        drives = self.drive_page.get_selected_drives()
        if drives:
            self._selected_drives = drives
            self._selected_drive = self._selected_drive or drives[0]
            if self._selected_drive.device not in {d.device for d in drives}:
                self._selected_drive = drives[0]
        if not self._selected_drives or not self._image_info:
            return
        self._settings.update(self.image_page.get_settings())
        self._settings["drives"] = self._selected_drives
        self._settings["drive"] = self._selected_drive
        _install = self.drive_page.get_install_config()
        _install.edition_index = self._settings.get("edition_index", 1)
        self._settings["install_config"] = _install
        self._settings.setdefault("verify", self.summary_page.get_verify_enabled())
        if self._image_info.image_type == ImageType.WINDOWS_INSTALLER:
            install = self._settings["install_config"]
            opts = self._settings.setdefault("windows_options", {})
            for key, value in self._cli_windows_options.items():
                opts.setdefault(key, value)
            opts.setdefault("skip_oobe", True)
            opts.setdefault("bypass_win11", install.bypass_win11)
            opts.setdefault("remove_defender", False)
            opts.setdefault("username", install.username)
            opts.setdefault("password", install.password)
            opts.setdefault("full_name", install.full_name)
        self.summary_page.update_summary(self._settings)
        self.summary_page.set_verify_enabled(self._settings.get("verify", True))
        brunch = self._settings.get("brunch") or {}
        raw_verify = (
            self._image_info.image_type in (
                ImageType.LINUX_ISHYBRID, ImageType.CHROMEOS,
                ImageType.BSD_UNIX,
            )
            and self._settings.get("write_mode", 1) == 0
            and not self._settings["install_config"].is_direct
            # A Brunch install is not a copy of the image, so there is
            # nothing to compare the drive with.
            and not brunch.get("use_brunch")
        )
        self.summary_page.set_verify_supported(raw_verify)

    # -------------------------------------------------------------- flashing

    def _start_flashing(self):
        """Validate and ask for confirmation before writing."""
        drives = self.drive_page.get_selected_drives()
        self._selected_drives = drives
        self._selected_drive = self.drive_page.get_selected_drive()
        if not drives or not self._iso_path or not self._image_info:
            return

        image_size = self._image_info.size
        too_small = [d for d in drives if image_size > d.size]
        if too_small:
            self._show_error(
                _("Image Too Large"),
                _("The image ({image}) does not fit on: {drives}.").format(
                    image=format_size(image_size),
                    drives=", ".join(d.device for d in too_small),
                ),
            )
            return

        config = self.drive_page.get_install_config()
        if config.is_direct:
            errors = []
            for drive in drives:
                errors.extend(
                    f"{drive.device}: {e}"
                    for e in validate_install_config(
                        config, drive.size, self._image_info.image_type
                    )
                )
            if errors:
                self._show_error(
                    _("Invalid Installation Settings"),
                    "\n".join(errors),
                )
                return
            self._settings["install_config"] = config

        self._show_confirmation_dialog()

    def _show_confirmation_dialog(self):
        """Destructive confirmation for one or more target drives."""
        drives = self._selected_drives or [self._selected_drive]
        config = self.drive_page.get_install_config()
        drive_list = "\n".join(
            f"• <b>{d.device}</b> — {d.model} ({d.size_human})"
            for d in drives
        )
        mode = (_("Direct install (portable)") if config.is_direct
                else _("Live USB"))
        extra = f"\n\nMode: <b>{mode}</b>"
        if config.is_direct:
            extra += _("\nUser: <b>{user}@{host}</b>").format(
                user=config.username, host=config.hostname
            )
            if config.is_custom:
                extra += _("\nPartitioning: <b>Custom</b>")
            else:
                extra += _("\nPartitioning: <b>Automatic</b>")

        dialog = Adw.AlertDialog(
            heading=_("Erase and Write?"),
            body=_(
                "Everything on the following drive(s) will be permanently "
                "erased:\n\n{drives}\n\n"
                "This will write <b>{image}</b> to {count} drive(s).{extra}"
            ).format(
                drives=drive_list,
                image=os.path.basename(self._iso_path),
                count=len(drives),
                extra=extra,
            ),
        )
        dialog.set_body_use_markup(True)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("confirm", _("Erase and Write"))
        dialog.set_response_appearance("confirm", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.choose(self, None, self._on_confirm_response)

    def _on_confirm_response(self, dialog, result):
        try:
            response = dialog.choose_finish(result)
        except GLib.Error:
            return
        if response == "confirm":
            self._current_step = STEP_NAMES.index("flash")
            self._update_step_ui()
            self.flash_page.start_flashing()
            drives = self._selected_drives or [self._selected_drive]
            self.flash_page.set_drive_info(
                " · ".join(
                    f"{d.model} ({d.device}, {d.size_human})" for d in drives
                )
            )
            self._is_flashing = True
            self._update_step_ui()
            self._run_flash_worker()

    def _run_flash_worker(self):
        self._cancel_requested = False
        self._worker_thread = threading.Thread(target=self._flash_worker, daemon=True)
        self._worker_thread.start()

    def _flash_worker(self):
        """Background flash worker: never touches widgets directly."""
        try:
            success = self._do_flash()
            GLib.idle_add(self._flash_complete, success, "")
        except StoppedException:
            GLib.idle_add(self._flash_complete, False, _("Cancelled"))
        except NotAuthorizedError:
            GLib.idle_add(self._flash_complete, False, _("Authentication was cancelled"))
        except PortalPathError as e:
            GLib.idle_add(self._flash_complete, False, str(e))
        except WriteError as e:
            GLib.idle_add(self._flash_complete, False, str(e))
        except VerifyMismatchError as e:
            GLib.idle_add(self._flash_complete, False, str(e))
        except Exception as e:  # noqa: BLE001 - reported in the UI
            log.exception("Flash failed")
            GLib.idle_add(self._flash_complete, False, f"{_('Unexpected error')}: {e}")

    def _do_flash(self) -> bool:
        """Write the image to one or several drives in parallel.

        Both Live USB mode and Direct install / portable mode are supported.
        File-based writes share a single read-only ISO mount so concurrent
        drives do not race each other's loop devices.
        """
        drives = list(self._selected_drives or [])
        if not drives and self._selected_drive is not None:
            drives = [self._selected_drive]
        if not drives or self._iso_path is None or self._image_info is None:
            raise WriteError(_("Nothing to flash — start a new operation."))

        iso_path = self._iso_path
        settings = self._settings
        image_type = self._image_info.image_type
        write_mode = settings.get("write_mode", 1)  # 0=DD, 1=Extract
        install_config = self.drive_page.get_install_config()
        install_config.edition_index = settings.get("edition_index", 1)

        # ChromeOS recovery images and macOS disk images have only one
        # sensible way to be written, so the user's choice does not apply.
        brunch = settings.get("brunch") or {}
        use_brunch = bool(brunch.get("use_brunch"))
        if image_type == ImageType.CHROMEOS:
            write_mode = 0        # raw block copy
        elif image_type == ImageType.MACOS:
            write_mode = 2        # Apple's own partition layout
        elif image_type == ImageType.BSD_UNIX:
            write_mode = 0        # the image brings its own partition table
        windows_options = dict(settings.get("windows_options") or {})
        if windows_options:
            install_config.skip_oobe = windows_options.get(
                "skip_oobe", install_config.skip_oobe
            )
            install_config.remove_defender = windows_options.get(
                "remove_defender", install_config.remove_defender
            )
            install_config.bypass_win11 = windows_options.get(
                "bypass_win11", install_config.bypass_win11
            )
            install_config.username = windows_options.get(
                "username", install_config.username
            )
            install_config.password = windows_options.get(
                "password", install_config.password
            )
            install_config.full_name = windows_options.get(
                "full_name", install_config.full_name
            )
            install_config.drivers_path = windows_options.get(
                "drivers_path", install_config.drivers_path
            )
        windows_options.setdefault("hostname", install_config.hostname)
        windows_options.setdefault("timezone", install_config.timezone)
        windows_options.setdefault("language", install_config.language)
        windows_options.setdefault("keyboard", install_config.keyboard)
        settings["install_config"] = install_config

        self._start_time = GLib.get_monotonic_time()

        def status_cb(msg):
            GLib.idle_add(self.flash_page.log, msg, "info")
            GLib.idle_add(self.flash_page.set_status, msg)

        # Raw byte verification is only meaningful when the image was written
        # directly with dd. File-based installers need filesystem-level
        # verification (adapter-specific), so do not pretend to verify them.
        raw_verify = (
            image_type in (ImageType.LINUX_ISHYBRID, ImageType.CHROMEOS,
                           ImageType.BSD_UNIX)
            and write_mode == 0
            and not install_config.is_direct
            and not use_brunch
        )
        verify_enabled = bool(settings.get("verify", True)) and raw_verify
        if settings.get("verify", True) and not raw_verify:
            status_cb(_("Verification is not available for this write mode; skipping."))

        verify_share = 0.1 if verify_enabled else 0.0
        write_share = 1.0 - verify_share

        def _emit_progress(fraction, done, total):
            elapsed = (GLib.get_monotonic_time() - self._start_time) / 1_000_000
            speed = done / elapsed if elapsed > 0 else 0.0
            eta = int((total - done) / speed) if speed > 0 and total > done else 0
            GLib.idle_add(
                self.flash_page.update_progress, fraction, "", speed, eta
            )

        def stopped_cb():
            return self._cancel_requested

        write_mode_share = write_share

        def make_status(device):
            prefix = f"{device}: " if len(drives) > 1 else ""

            def _status(msg):
                GLib.idle_add(self.flash_page.log, prefix + msg, "info")
                GLib.idle_add(self.flash_page.set_status, msg)

            return _status

        # A shared ISO mount is safe and avoids each writer mounting/remounting
        # the same ISO concurrently. Raw writes and the macOS layout do not
        # need the image mounted at all.
        needs_iso = not (
            image_type in (ImageType.LINUX_ISHYBRID, ImageType.CHROMEOS,
                           ImageType.BSD_UNIX)
            and write_mode == 0
            and not install_config.is_direct
        ) and image_type != ImageType.MACOS
        if needs_iso:
            release_stale_iso_mounts(iso_path)
            iso_cm = mount_iso_readonly(iso_path)
        else:
            release_stale_iso_mounts(iso_path)
            iso_cm = contextlib.nullcontext(None)

        def write_one(index, drive, progress_channel, shared_root, verify_channel=None):
            device = drive.device
            device_status = make_status(device)

            current_identity = get_drive_identity(device)
            saved_identity = getattr(drive, "_identity", None)
            if saved_identity and current_identity != saved_identity:
                raise WriteError(
                    _("Drive identity changed — aborted for safety: {device}").format(
                        device=device
                    )
                )
            drive._identity = current_identity

            if install_config.is_direct:
                return install_to_drive(
                    iso_path, device, self._image_info, install_config,
                    progress_channel, stopped_cb, device_status,
                    scheme=settings.get("scheme", PartitionScheme.GPT),
                    target=settings.get("target", TargetSystem.UEFI),
                    label=settings.get("volume_label", "YAKU"),
                    iso_root=shared_root,
                    release_stale=False,
                )

            if image_type == ImageType.WINDOWS_INSTALLER:
                return write_windows_installer(
                    iso_path, device,
                    settings.get("scheme", PartitionScheme.GPT),
                    settings.get("volume_label", "WINUSB"),
                    progress_channel, stopped_cb,
                    status_cb=device_status,
                    release_stale=False,
                    iso_root=shared_root,
                    windows_options=windows_options,
                )

            if settings.get("windows_togo", False):
                return write_windows_togo(
                    iso_path, device,
                    settings.get("edition_index", 1),
                    progress_channel, stopped_cb,
                    lambda _seq, _rand: True,
                    iso_root=shared_root,
                    release_stale=False,
                )

            if (image_type in (ImageType.LINUX_ISHYBRID, ImageType.BSD_UNIX)
                    and write_mode == 0):
                return copy_image_dd(
                    iso_path, device, progress_channel, stopped_cb,
                    verify=internal_verify,
                    release_stale=False,
                    verify_progress=verify_channel,
                    status_cb=device_status,
                )

            if image_type == ImageType.CHROMEOS:
                if use_brunch:
                    # Brunch builds its own ChromeOS partition table and
                    # copies the recovery image into it.
                    return write_brunch_installer(
                        iso_path, device,
                        brunch.get("assets_dir", ""),
                        progress_channel, stopped_cb,
                        status_cb=device_status,
                        legacy_boot=bool(brunch.get("legacy_boot")),
                    )
                # A ChromeOS recovery image is a raw disk image: Google
                # writes it to the drive byte for byte as well.
                return copy_image_dd(
                    iso_path, device, progress_channel, stopped_cb,
                    verify=internal_verify,
                    release_stale=False,
                    verify_progress=verify_channel,
                    status_cb=device_status,
                )

            if image_type == ImageType.MACOS:
                return write_macos_installer(
                    iso_path, device, progress_channel, stopped_cb,
                    status_cb=device_status,
                    release_stale=False,
                )

            from yaku.core.partitions import get_generic_layout
            layout = get_generic_layout(
                settings.get("scheme", PartitionScheme.GPT),
                settings.get("target", TargetSystem.UEFI),
                settings.get("filesystem", FileSystem.FAT32),
                settings.get("volume_label", "YAKU"),
                drive.size,
            )
            return extract_iso_files(
                iso_path, device, layout,
                progress_channel, stopped_cb,
                release_stale=False,
                iso_root=shared_root,
            )

        write_agg = AggregateProgress(
            len(drives),
            lambda frac, done, total: _emit_progress(
                frac * write_mode_share, done, total
            ),
        )
        verify_agg = AggregateProgress(
            len(drives),
            lambda frac, done, total: _emit_progress(
                write_mode_share + frac * verify_share, done, total
            ),
        )
        # With a single drive the dd writer verifies with the descriptor it
        # already holds, which avoids a second privileged open of the device
        # (and the polkit authorization that comes with it). With several
        # drives the verification runs afterwards, aggregated over all of
        # them, so the progress bar does not jump between the two phases.
        internal_verify = verify_enabled and len(drives) == 1

        failures = []
        with iso_cm as shared_root:
            if len(drives) == 1:
                try:
                    ok = write_one(0, drives[0], write_agg.channel(0), shared_root,
                                   verify_agg.channel(0))
                    if not ok:
                        failures.append((drives[0], _("Cancelled")))
                except Exception as exc:  # noqa: BLE001 - reported below
                    failures.append((drives[0], exc))
            else:
                with ThreadPoolExecutor(
                    max_workers=min(4, len(drives))
                ) as executor:
                    futures = {
                        executor.submit(
                            write_one, i, drive, write_agg.channel(i),
                            shared_root, verify_agg.channel(i),
                        ): drive
                        for i, drive in enumerate(drives)
                    }
                    for future in as_completed(futures):
                        drive = futures[future]
                        try:
                            ok = future.result()
                            if not ok:
                                failures.append((drive, _("Cancelled")))
                        except StoppedException:
                            failures.append((drive, _("Cancelled")))
                        except Exception as exc:  # noqa: BLE001
                            failures.append((drive, exc))
                            # Stop the other drives as soon as one fails.
                            self._cancel_requested = True

        if failures:
            # Prefer reporting the real error over cascaded cancellations.
            real = [(d, e) for d, e in failures
                    if not isinstance(e, StoppedException)]
            to_report = real or failures
            details = "; ".join(
                f"{d.device}: {e}" for d, e in to_report
            )
            raise WriteError(
                _("Flashing failed on {count} drive(s): {details}").format(
                    count=len(to_report), details=details
                )
            )

        if verify_enabled and not internal_verify:
            status_cb(_("Verifying written data…"))
            verify_failures = []

            def verify_one(index, drive):
                return verify_device_matches_image(
                    drive.device, iso_path,
                    verify_agg.channel(index), stopped_cb,
                )

            if len(drives) == 1:
                try:
                    if not verify_one(0, drives[0]):
                        verify_failures.append((drives[0], _("Cancelled")))
                except Exception as exc:  # noqa: BLE001
                    verify_failures.append((drives[0], exc))
            else:
                with ThreadPoolExecutor(
                    max_workers=min(4, len(drives))
                ) as executor:
                    futures = {
                        executor.submit(verify_one, i, drive): drive
                        for i, drive in enumerate(drives)
                    }
                    for future in as_completed(futures):
                        drive = futures[future]
                        try:
                            if not future.result():
                                verify_failures.append((drive, _("Cancelled")))
                        except Exception as exc:  # noqa: BLE001
                            verify_failures.append((drive, exc))
                            self._cancel_requested = True

            if verify_failures:
                d, exc = verify_failures[0]
                if isinstance(exc, VerifyMismatchError):
                    raise exc
                raise VerifyMismatchError(
                    _("Verification failed on {device}: {error}").format(
                        device=d.device, error=exc
                    )
                )

        return True

    def _flash_complete(self, success: bool, message: str):
        """Handle flash completion (runs on the main thread)."""
        self._is_flashing = False
        self.flash_page.stop_flashing(success, message)
        self.next_btn.set_label(_("Done"))
        self._update_step_ui()

        if success:
            drives = self._selected_drives or [self._selected_drive]
            devices = ", ".join(d.device for d in drives if d is not None)
            self._show_notification(
                _("Flash Complete"),
                _("Wrote {image} to {count} drive(s): {devices}").format(
                    image=os.path.basename(self._iso_path),
                    count=len(drives),
                    devices=devices,
                ),
            )
        else:
            self._show_notification(_("Flash Failed"), message, priority="critical")

    def _on_flash_done(self):
        self._reset_wizard()

    def _on_flash_retry(self):
        """Retry the same write without re-walking the wizard."""
        if self._is_flashing:
            return
        if not (self._selected_drives and self._iso_path and self._image_info):
            # Nothing retained (e.g. wizard was reset) — start clean
            self._reset_wizard()
            return
        log.info("Retrying flash after failure")
        self._current_step = STEP_NAMES.index("flash")
        self._update_step_ui()
        self.flash_page.start_flashing()
        self._is_flashing = True
        self._update_step_ui()
        self._run_flash_worker()

    def _on_flash_cancel(self, _btn=None):
        if self._is_flashing:
            self._cancel_requested = True
            self.flash_page.log(_("Cancelling…"), "warning")
        else:
            self._reset_wizard()

    def _reset_wizard(self):
        """Return to step 1 with a clean state."""
        self._current_step = 0
        self._iso_path = None
        self._selected_drive = None
        self._selected_drives = []
        self._image_info = None
        self.drive_page.set_image_type(None)
        self._settings = {}
        self._is_flashing = False
        self._cancel_requested = False

        self.image_page.cleanup()
        self.flash_page.cleanup()
        self.drive_page.refresh_drives()
        self._update_step_ui()

    def _auto_refresh_drives(self):
        """Refresh the drive list on step 1 unless the user is busy."""
        if self._current_step == 0 and not self._is_flashing:
            if not self.drive_page.is_user_interacting():
                self.drive_page.refresh_drives()
        return True

    # ------------------------------------------------------------ drag/drop

    def _on_drop(self, _target, value, _x, _y):
        """Handle an ISO/image dropped on the window."""
        if self._current_step > 1:
            return False
        files = value.get_files() if value is not None else []
        if len(files) == 1:
            path = files[0].get_path()
            if path and path.lower().endswith((".iso", ".img", ".dd", ".raw", ".bin")):
                if self._current_step == 0 and self.drive_page.get_selected_drive():
                    self._selected_drive = self.drive_page.get_selected_drive()
                    self._settings["drive"] = self._selected_drive
                    self._current_step = 1
                    self._update_step_ui()
                self.image_page.set_image(path)
                return True
        return False

    def _on_drag_enter(self, _target, _x, _y):
        if self._current_step <= 1:
            self.add_css_class("drop-active")
        return Gdk.DragAction.COPY

    def _on_drag_leave(self, _target):
        self.remove_css_class("drop-active")

    # --------------------------------------------------------------- dialogs

    def _on_close_request(self, _window):
        """Warn before quitting while a flash is running."""
        if self._is_flashing or (self._worker_thread and self._worker_thread.is_alive()):
            dialog = Adw.AlertDialog(
                heading=_("Flashing in Progress"),
                body=_("Closing now will leave the drive unbootable. Stop flashing first?"),
            )
            dialog.add_response("stay", _("Keep Flashing"))
            dialog.add_response("quit", _("Force Quit"))
            dialog.set_response_appearance("quit", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.choose(self, None, self._on_close_response)
            return True
        return False

    def _on_close_response(self, dialog, result):
        try:
            response = dialog.choose_finish(result)
        except GLib.Error:
            return
        if response == "quit":
            self._cancel_requested = True
            self.destroy()

    def _show_about(self, _action, _param):
        dialog = Adw.AboutDialog(
            application_name=_("Yaku"),
            application_icon="com.github.INIT-D43M0N.yaku",
            version="6.0.0",
            developer_name="Yaku Contributors",
            license_type=Gtk.License.GPL_3_0,
            website="https://github.com/INIT-D43M0N/Yaku",
            issue_url="https://github.com/INIT-D43M0N/Yaku/issues",
            comments=_("Create bootable USB drives on Linux (a Rufus alternative)"),
        )
        dialog.present(self)

    def _show_preferences(self, _action, _param):
        """Native Adw.PreferencesDialog (collapses to pages on mobile)."""
        dialog = Adw.PreferencesDialog(title=_("Preferences"))
        page = Adw.PreferencesPage(title=_("General"))
        dialog.add(page)

        appearance = Adw.PreferencesGroup(title=_("Appearance"))
        page.add(appearance)

        theme_row = Adw.ComboRow(title=_("Theme"))
        theme_row.set_model(Gtk.StringList.new([_("System"), _("Light"), _("Dark")]))
        theme_row.set_selected(APP_CONFIG.get("theme", 0))
        theme_row.connect("notify::selected", self._on_theme_changed)
        appearance.add(theme_row)

        language = Adw.PreferencesGroup(title=_("Language"))
        page.add(language)

        lang_row = Adw.ComboRow(title=_("Language"))
        lang_row.set_model(Gtk.StringList.new([
            _("System Default"), "English", "Português", "Русский",
        ]))
        current_lang = APP_CONFIG.get("lang", "")
        lang_row.set_selected(2 if current_lang.startswith("pt") else 3 if current_lang.startswith("ru") else 0)
        lang_row.connect("notify::selected", self._on_lang_changed)
        language.add(lang_row)

        dialog.present(self)

    def _show_shortcuts(self, _action, _param):
        builder = Gtk.Builder()
        builder.set_translation_domain("yaku")
        builder.add_from_file(str(ui_file("shortcuts.ui")))
        shortcuts = builder.get_object("shortcuts_window")
        shortcuts.set_transient_for(self)
        shortcuts.present(self)

    def _on_refresh_action(self, _action, _param):
        if self._current_step == 0:
            self.drive_page.refresh_drives()

    def _on_choose_image_action(self, _action, _param):
        if self._current_step <= 1:
            self._current_step = 1
            self._update_step_ui()
            self.image_page._on_browse_clicked(None)

    def _on_theme_changed(self, combo, _pspec):
        idx = combo.get_selected()
        APP_CONFIG["theme"] = idx
        save_config(APP_CONFIG)
        self._apply_theme()

    def _on_lang_changed(self, combo, _pspec):
        idx = combo.get_selected()
        langs = ["", "en_US.UTF-8", "pt_BR.UTF-8", "ru_RU.UTF-8"]
        new_lang = langs[idx] if idx < len(langs) else ""
        if APP_CONFIG.get("lang") == new_lang:
            return
        APP_CONFIG["lang"] = new_lang
        save_config(APP_CONFIG)

        dialog = Adw.AlertDialog(
            heading=_("Language Changed"),
            body=_("The new language will be applied the next time Yaku starts."),
        )
        dialog.add_response("ok", _("OK"))
        dialog.choose(self, None, lambda *_: None)

    def _show_error(self, title: str, message: str):
        dialog = Adw.AlertDialog(heading=title, body=message)
        dialog.add_response("ok", _("OK"))
        dialog.set_default_response("ok")
        dialog.choose(self, None, lambda *_: None)

    def _show_notification(self, title: str, body: str, priority: str = "normal"):
        """Best-effort desktop notification (never fatal)."""
        try:
            notification = Gio.Notification.new(title)
            notification.set_body(body)
            notification.set_priority(
                Gio.NotificationPriority.HIGH if priority == "critical"
                else Gio.NotificationPriority.NORMAL
            )
            self.get_application().send_notification(None, notification)
        except Exception:  # noqa: BLE001 - notifications are optional
            log.debug("Could not send notification", exc_info=True)

    # --------------------------------------------------------------- cleanup

    def cleanup(self):
        """Stop timers before the window goes away."""
        if getattr(self, "_refresh_id", None):
            GLib.source_remove(self._refresh_id)
            self._refresh_id = None
        self.drive_page.cleanup()
        self.image_page.cleanup()
        self.flash_page.cleanup()
