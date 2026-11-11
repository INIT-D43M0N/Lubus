"""Smoke test for the rewritten GTK4/Libadwaita interface.

Run:  python3 tests/ui_smoke.py

Verifies that every .ui file loads, all page classes instantiate, the window
builds with its header-bar navigation, and the phone breakpoint applies —
without needing a real USB drive (drive lists and image analysis are faked).
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.argv = ["yaku"]

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Gdk

failures = []


def check(name, cond, detail=""):
    print(("  PASS: " if cond else "  FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# Instantiating an Adw.Application registers the libadwaita widget types
# that the .ui files reference (AdwBanner etc.).
app = Adw.Application(application_id="org.yaku.ui_smoke")
app.connect("activate", lambda _a: None)

# ---------------------------------------------------------------- .ui files
print("=== UI FILES ===")
from yaku.utils.resources import ui_file, data_dir
from yaku.pages.base import UiPage

for name in ("drive_page.ui", "image_page.ui", "summary_page.ui", "flash_page.ui", "shortcuts.ui"):
    try:
        builder = Gtk.Builder()
        builder.set_translation_domain("yaku")
        builder.add_from_file(str(ui_file(name)))
        check(f"{name} loads", builder.get_objects() is not None)
    except GLib.Error as e:
        check(f"{name} loads", False, e.message)

check("data dir resolved", data_dir().is_dir(), str(data_dir()))
check("app icon PNG shipped",
      (data_dir() / "icons" / "hicolor" / "1024x1024" / "apps" / "yaku.png").is_file())
check("app icon SVG shipped",
      (data_dir() / "icons" / "hicolor" / "scalable" / "apps" / "yaku.svg").is_file())

# ------------------------------------------------------------------- pages
print("\n=== PAGES ===")
from yaku.pages import DrivePage, ImagePage, SummaryPage, FlashPage

drive_page = DrivePage(on_drive_selected=None)
check("DrivePage instantiates", drive_page.widget is not None)
check("Step 1 uses native preferences layout",
      drive_page.get("drives_group") is not None)

image_page = ImagePage(on_image_selected=None, on_analysis_complete=None)
check("ImagePage instantiates", image_page.widget is not None)
check("image page exposes the ISO download button",
      image_page.download_btn.get_label() == "Download Linux ISO")
from yaku.pages.iso_download import IsoDownloadDialog
_dl = IsoDownloadDialog(lambda _p: None)
check("ISO downloader dialog builds",
      _dl.get_child() is not None
      and _dl._distro_list.get_first_child() is not None)

summary_page = SummaryPage(on_verify_toggled=None)
check("SummaryPage instantiates", summary_page.widget is not None)
check("Windows driver row is present",
      summary_page.get("win_drivers_row") is not None)
check("Windows options expose drivers_path",
      "drivers_path" in summary_page.get_windows_options())
check("Windows driver hint is present",
      summary_page.get("win_driver_hint_row") is not None)

flash_page = FlashPage(on_done=None, on_cancel=None)
check("FlashPage instantiates", flash_page.widget is not None)

# Drive list behaviour with fake hardware
from yaku.core.devices import Drive, BusType
fake_drives = [
    Drive(device="/dev/sdb", size=32 * 1024**3, model="Ultra Fit", vendor="SanDisk",
          product="Ultra Fit", serial="S1", wwn="W1", bus_type=BusType.USB,
          removable=True, ejectable=True, media_available=True, media_removable=True),
    Drive(device="/dev/sdc", size=64 * 1024**3, model="Extreme", vendor="SanDisk",
          product="Extreme", serial="S2", wwn="W2", bus_type=BusType.USB,
          removable=True, ejectable=True, media_available=True, media_removable=True),
]
drive_page._update_drive_list(fake_drives)
check("two drive rows created", drive_page.drive_list.get_first_child() is not None)
check("first drive auto-selected", drive_page.get_selected_drive() is not None
      and drive_page.get_selected_drive().device == "/dev/sdb")
check("erase warning revealed", drive_page.erase_banner.get_revealed())
check("drive row carries path, bus and capacity",
      "/dev/sdb · USB · 32 GiB" in drive_page.drive_list.get_first_child().get_subtitle())
check("Screen 1 uses a native preferences page",
      isinstance(drive_page.get("preferences_page"), Adw.PreferencesPage))
check("Screen 1 groups installation and target drives",
      isinstance(drive_page.get("mode_group"), Adw.PreferencesGroup)
      and isinstance(drive_page.get("drives_group"), Adw.PreferencesGroup))
check("Screen 1 has no custom dashboard widget",
      not hasattr(drive_page, "tv_slot"))
check("more-drives button hidden with two drives",
      not drive_page.more_btn.get_visible())
drive_page.set_selected_drive("/dev/sdc")
check("selection restored by device path",
      drive_page.get_selected_drive().device == "/dev/sdc")

# Multiple targets: inclusion checkboxes are independent of the primary row.
second = drive_page._row_for_device("/dev/sdc")
second._include_check.set_active(True)
check("multiple drives can be included",
      {d.device for d in drive_page.get_selected_drives()}
      == {"/dev/sdb", "/dev/sdc"})
second._include_check.set_active(False)

# 5.0 installation mode / provisioning controls.
check("install mode offers live and direct options",
      drive_page.install_mode_row.get_model().get_n_items() == 2)
drive_page.install_mode_row.set_selected(1)
check("direct mode reveals user fields",
      drive_page.user_group.get_visible()
      and drive_page.partition_mode_row.get_visible())
check("direct mode exposes internal-drive opt-in",
      drive_page.show_internal_row.get_visible())
drive_page.partition_mode_row.set_selected(1)
check("custom mode reveals layout controls",
      drive_page.partition_layout_row.get_visible())
drive_page.username_row.set_text("alice")
drive_page.password_row.set_text("secret")
drive_page.fullname_row.set_text("Alice Example")
drive_page.hostname_row.set_text("alice-pc")
_config = drive_page.get_install_config()
check("install config round-trips",
      _config.is_direct and _config.is_custom
      and _config.username == "alice" and _config.hostname == "alice-pc")
from yaku.core.direct_install import validate_install_config
check("valid direct config accepted",
      validate_install_config(_config, 128 * 1024**3, None) == [])

from yaku.core.images import ImageType
drive_page.set_image_type(ImageType.WINDOWS_INSTALLER)
check("Windows direct install exposes boot type",
      drive_page.boot_mode_row.get_visible()
      and drive_page.boot_mode_row.get_model().get_n_items() == 2)
check("Windows direct install fixes filesystem to NTFS",
      drive_page.direct_fs_row.get_model().get_string(0) == "NTFS")
drive_page.boot_mode_row.set_selected(1)
drive_page.language_row.set_text("pt-BR")
drive_page.keyboard_row.set_text("0416:00000416")
drive_page.bypass_win11_row.set_active(True)
_win_cfg = drive_page.get_install_config()
check("Windows install config carries boot/language/bypass",
      _win_cfg.boot_mode == "bios"
      and _win_cfg.language == "pt-BR"
      and _win_cfg.keyboard == "0416:00000416"
      and _win_cfg.bypass_win11
      and _win_cfg.filesystem == "ntfs")
drive_page.set_image_type(None)

drive_page.install_mode_row.set_selected(0)
check("live mode hides provisioning fields",
      not drive_page.user_group.get_visible())
check("live mode hides the internal-drive opt-in",
      not drive_page.show_internal_row.get_visible())

# More than two drives: top two stay prominent, the rest are expandable.
many_drives = fake_drives + [
    Drive(device="/dev/sdd", size=128 * 1024**3, model="Portable SSD", vendor="SanDisk",
          product="Portable SSD", serial="S3", wwn="W3", bus_type=BusType.USB,
          removable=True, ejectable=True, media_available=True, media_removable=True),
    Drive(device="/dev/sde", size=16 * 1024**3, model="Cruzer Blade", vendor="SanDisk",
          product="Cruzer Blade", serial="S4", wwn="W4", bus_type=BusType.USB,
          removable=True, ejectable=True, media_available=True, media_removable=True),
]
drive_page._update_drive_list(many_drives)
check("top two drives shown prominently", sum(1 for _ in drive_page.drive_list) == 2)
check("more-drives popup button visible", drive_page.more_btn.get_visible())
check("more-drives button advertises the total",
      "4" in drive_page.more_btn.get_label())

from yaku.pages.drive_dialog import DriveListDialog
_dialog = DriveListDialog(
    many_drives, {d.device for d in many_drives},
    lambda _d, _a: None, lambda _a: None,
)
check("all-drives popup lists every drive", len(_dialog._rows) == 4)
check("select-all starts checked",
      _dialog._select_all.get_active()
      and not _dialog._select_all.get_inconsistent())
_dialog.set_checked({"/dev/sdb"})
check("partial selection makes select-all inconsistent",
      _dialog._select_all.get_inconsistent())
_dialog.set_checked(set())
check("empty selection clears select-all",
      not _dialog._select_all.get_active())

drive_page._set_device_checked("/dev/sdd", True)
check("extra drive can be included through the popup logic",
      "/dev/sdd" in {d.device for d in drive_page.get_selected_drives()})
drive_page._set_all_devices_checked(True)
check("select all includes every dashboard drive",
      len(drive_page.get_selected_drives()) == 4)
drive_page._set_all_devices_checked(False)
check("deselect all clears the drives and warning",
      drive_page.get_selected_drives() == []
      and not drive_page.erase_banner.get_revealed())
drive_page.set_selected_drive("/dev/sdb")
check("primary selection is restored after select-all",
      drive_page.get_selected_drive().device == "/dev/sdb"
      and drive_page.erase_banner.get_revealed())

drive_page._update_drive_list([])
check("empty state shown for no drives",
      drive_page.state_stack.get_visible_child_name() == "empty")

# Image page options
check("scheme options present", image_page.scheme_row.get_model() is not None)
check("fs options present", image_page.fs_row.get_model() is not None)
check("label limited to 11 chars (FAT32)", image_page.label_row.get_text() == "YAKU")
image_page.label_row.set_text("VERYLONGLABEL")
image_page._on_label_changed(None)
check("label truncated to 11 chars", len(image_page.label_row.get_text()) == 11,
      image_page.label_row.get_text())
settings = image_page.get_settings()
check("get_settings returns scheme/target/fs",
      settings["scheme"] is not None and settings["target"] is not None
      and settings["filesystem"] is not None)

# Interactive disc easter egg (Step 2)
from yaku.pages.disc import DiscFlip
check("disc easter egg lives in the empty state",
      isinstance(image_page.disc_flip, DiscFlip)
      and image_page.disc_flip.get_parent() is image_page.disc_slot)
check("disc ships both faces (back + front)",
      image_page.disc_flip._back is not None and image_page.disc_flip._front is not None)
check("disc starts on the holographic back", image_page.disc_flip._angle == 0.0)
image_page.disc_flip.flip()
check("clicking the disc flips to the artwork", image_page.disc_flip._target == 180.0)
image_page.disc_flip.flip()
check("clicking again flips back to the holographic side",
      image_page.disc_flip._target == 0.0)
image_page.disc_flip.reset()

# ------------------------------------------------------------------ window
print("\n=== WINDOW ===")
from yaku.window import YakuWindow

window = YakuWindow(app)
window.present()
check("window has 4 steps", window.view_stack.get_pages().get_n_items() == 4)
check("back hidden on step 1", not window.back_btn.get_visible())
check("cancel hidden outside flash", not window.cancel_btn.get_visible())
check("next insensitive without drives", not window.next_btn.get_sensitive())

# Navigate with faked state: repopulate the drive list first (this also
# selects the first drive, mirroring what auto-refresh does with hardware).
window.drive_page._update_drive_list(fake_drives)
check("next enabled once a drive is picked", window.next_btn.get_sensitive())
check("window tracks the initial target",
      len(window._selected_drives) == 1
      and window._selected_drives[0].device == "/dev/sdb")
_wsecond = window.drive_page._row_for_device("/dev/sdc")
_wsecond._include_check.set_active(True)
check("window tracks multiple targets",
      {d.device for d in window._selected_drives} == {"/dev/sdb", "/dev/sdc"})
_wsecond._include_check.set_active(False)

window._on_next_clicked(None)
check("advanced to image step", window._current_step == 1)
check("back visible on step 2", window.back_btn.get_visible())

window._current_step = 2
window._update_step_ui()
check("advanced to summary step", window._current_step == 2)
check("next shows flash label", "Flash" in window.next_btn.get_label())

window._is_flashing = True
window._current_step = 3
window._update_step_ui()
check("flash step: cancel visible", window.cancel_btn.get_visible())
check("flash step: back hidden", not window.back_btn.get_visible())

# Flash page transitions
window.flash_page.start_flashing()
check("flash page resets on start", window.flash_page.progress_bar.get_fraction() == 0.0)
check("heading is 'Writing Image' while flashing",
      window.flash_page.status_heading.get_label() == "Writing Image")
check("no placeholder dashes while starting",
      window.flash_page.status_caption.get_label() == "Starting…")
window.flash_page.update_progress(0.42, "Copying image…", speed=5 * 1024**2, eta=61)
check("progress fraction applied", abs(window.flash_page.progress_bar.get_fraction() - 0.42) < 1e-6)
check("failure point remembered", abs(window.flash_page._last_fraction - 0.42) < 1e-6)
caption = window.flash_page.status_caption.get_label()
check("caption carries percent, speed and ETA",
      "42%" in caption and "MiB/s" in caption and "ETA" in caption, caption)

# Progress normalisation: percentages are accepted, but progress callbacks
# can never claim the final tick (stop_flashing owns 100%).
window.flash_page.update_progress(42)
check("percentage input normalised to a 0..1 fraction",
      abs(window.flash_page.progress_bar.get_fraction() - 0.42) < 1e-6)
window.flash_page.update_progress(100)
check("progress updates never display 100% early",
      window.flash_page.progress_bar.get_fraction() < 1.0)
window.flash_page.update_progress(0)
check("progress can be reset to the initial 0%",
      window.flash_page.progress_bar.get_fraction() == 0.0
      and window.flash_page.status_caption.get_label() == "Starting…")
window.flash_page.update_progress(0.42, "Copying image…", speed=5 * 1024**2, eta=61)

check("safety subtitle stays visible while writing",
      window.flash_page.status_message.get_label() == "Do not unplug the target drive.")
check("writer phase shown on its own line",
      window.flash_page.phase_label.get_visible()
      and window.flash_page.phase_label.get_label() == "Copying image…")
window.flash_page.log("test line", "info")
check("console log appended", window.flash_page.console_buffer.get_char_count() > 0)

# drive info line under the stats
window.flash_page.set_drive_info("SanDisk Ultra · /dev/sdb · 32 GiB")
check("target drive info shown", window.flash_page.drive_info.get_visible()
      and "/dev/sdb" in window.flash_page.drive_info.get_label())

# no action buttons while writing (Cancel lives in the header bar)
check("no result buttons while writing",
      not window.flash_page.retry_btn.get_visible()
      and not window.flash_page.logs_btn.get_visible()
      and not window.flash_page.close_btn.get_visible())

# --- animated HDD replaces the old generic flash icon ---
from yaku.pages.hdd import HddAnimation
hdd = window.flash_page.hdd
check("HDD animation lives in the progress view",
      isinstance(hdd, HddAnimation) and hdd.get_parent() is window.flash_page.hdd_slot)
check("HDD widget is centered on both axes",
      hdd.get_halign() == Gtk.Align.CENTER and hdd.get_valign() == Gtk.Align.CENTER)
check("HDD platter artwork loaded", hdd._texture is not None)
check("HDD spins while writing", hdd._active and hdd._state == "writing")
# no action buttons while writing (Cancel lives in the header bar)
check("no page buttons while writing",
      not window.flash_page.retry_btn.get_visible()
      and not window.flash_page.logs_btn.get_visible()
      and not window.flash_page.close_btn.get_visible())

# --- failure state (the redesigned screen) ---
window.flash_page.stop_flashing(False, "[Errno 28] No space left on device")
check("failure fraction kept at failure point",
      abs(window.flash_page.progress_bar.get_fraction() - 0.42) < 1e-6)
check("failure point labelled in caption",
      "Failed at 42%" in window.flash_page.status_caption.get_label())
check("failure heading", window.flash_page.status_heading.get_label() == "Flash Failed")
check("helper text explains unchanged drive",
      "unchanged" in window.flash_page.status_message.get_label().lower())
check("error detail visible", window.flash_page.error_detail.get_visible())
check("HDD shows the failed state",
      window.flash_page.hdd._state == "failed"
      and not window.flash_page.hdd._active)
check("progress bar marked failed", window.flash_page.progress_bar.has_css_class("failed"))
check("retry visible on failure", window.flash_page.retry_btn.get_visible())
check("retry is the primary action", window.flash_page.retry_btn.has_css_class("suggested-action"))
check("error logs button visible", window.flash_page.logs_btn.get_visible())
check("close button visible", window.flash_page.close_btn.get_visible())

# View Error Logs reveals the accordion
window.flash_page._on_logs_clicked(None)
check("view logs opens details accordion", window.flash_page.details_expander.get_expanded())
check("view logs label flips", window.flash_page.logs_btn.get_label() == "Hide Error Logs")
window.flash_page._on_logs_clicked(None)
check("details can be collapsed again", not window.flash_page.details_expander.get_expanded())

# --- success state ---
window.flash_page.stop_flashing(True)
check("HDD shows the success state",
      window.flash_page.hdd._state == "success"
      and not window.flash_page.hdd._active)
check("success state shows a full bar",
      abs(window.flash_page.progress_bar.get_fraction() - 1.0) < 1e-6)
check("success heading", window.flash_page.status_heading.get_label() == "Flash Complete")
check("progress marked success", window.flash_page.progress_bar.has_css_class("success"))
check("no retry on success", not window.flash_page.retry_btn.get_visible())
check("close becomes primary Done", window.flash_page.close_btn.get_label() == "Done")
check("error detail hidden on success", not window.flash_page.error_detail.get_visible())

# Breakpoint behaviour (phone width): the column tightens
window._on_narrow_apply(None)
check("narrow breakpoint tightens the column",
      window.flash_page.column_clamp.get_maximum_size() == 320)
check("narrow breakpoint keeps caption visible",
      window.flash_page.status_caption.get_visible())
window._on_narrow_unapply(None)
check("breakpoint unapply restores the column",
      window.flash_page.column_clamp.get_maximum_size() == 420)
check("breakpoint unapply shows column content",
      window.flash_page.column_clamp.get_maximum_size() == 420)

# Actions exist
for name in ("about", "preferences", "shortcuts", "refresh", "choose-image"):
    check(f"win.{name} action present", window.lookup_action(name) is not None)

# Screen 4 has no embedded mini-game or alternate central view.
import re
from pathlib import Path

_fp_src = (Path(__file__).parent.parent / "yaku" / "pages" / "flash_page.py").read_text()
check("flash page has no mini-game",
      "minigame" not in _fp_src.lower()
      and "MiniGame" not in _fp_src)
check("flash page exposes only the progress view",
      not hasattr(window.flash_page, "game_toggle_btn")
      and not hasattr(window.flash_page, "is_game_visible"))
_defs = re.findall(r"^    def (\w+)", _fp_src, re.M)
_dupes = {d for d in _defs if _defs.count(d) > 1}
check("no duplicate method definitions in flash_page", not _dupes, str(_dupes))

window.cleanup()
window.destroy()

print()
if failures:
    print(f"RESULT: {len(failures)} UI FAILURE(S): {failures}")
    sys.exit(1)
print("RESULT: UI SMOKE TEST PASSED — interface loads on all targets")
