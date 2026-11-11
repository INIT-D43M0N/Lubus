"""Flash/Progress page (Step 4 of the wizard).

Layout: ``data/ui/flash_page.ui`` — a centered column (Adw.Clamp, 8px grid)
with the standard progress layout:

* an animated open HDD (:mod:`yaku.pages.hdd`): the character artwork is
  the spinning platter, with an actuator arm writing over it
* title/subtitle, progress bar, stats and the technical details log

Progress bar, stats (speed · ETA · target drive) and the details accordion
follow the GNOME-style task layout.
"""
import math
import time

import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, GLib, Gdk, Pango

from yaku.utils.i18n import _
from yaku.pages.base import UiPage
from yaku.pages.hdd import HddAnimation

# Console severities — distinguished by weight only, so the log stays legible
# in both light and dark styles (HIG: don't fight the theme).
_TAGS = ("info", "warning", "error", "success", "progress")


class FlashPage(UiPage):
    """Page showing flash progress and the result screen."""

    ui_name = "flash_page.ui"
    root_id = "root"

    def __init__(self, on_done=None, on_cancel=None, on_try_again=None, **kwargs):
        super().__init__()
        self.on_done = on_done
        self.on_cancel = on_cancel
        self.on_try_again = on_try_again
        self._is_flashing = False
        self._last_fraction = 0.0

        # Widgets
        self.column_clamp = self.get("column_clamp")
        self.hdd_slot = self.get("hdd_slot")
        self.status_heading = self.get("status_heading")
        self.status_message = self.get("status_message")
        self.phase_label = self.get("phase_label")
        self.error_detail = self.get("error_detail")
        self.progress_bar = self.get("progress_bar")
        self.status_caption = self.get("status_caption")
        self.drive_info = self.get("drive_info")
        self.retry_btn = self.get("retry_btn")
        self.logs_btn = self.get("logs_btn")
        self.close_btn = self.get("close_btn")
        self.details_expander = self.get("details_expander")
        self.copy_log_btn = self.get("copy_log_btn")
        self.console_buffer = self.get("console_view").get_buffer()
        self._console_view = self.get("console_view")

        # Animated open-HDD graphic replacing the generic flash icon.
        self.hdd = HddAnimation()
        self.hdd_slot.append(self.hdd)

        # Signals
        self.retry_btn.connect("clicked", self._on_retry_clicked)
        self.close_btn.connect("clicked", self._on_close_clicked)
        self.logs_btn.connect("clicked", self._on_logs_clicked)
        self.copy_log_btn.connect("clicked", self._on_copy_log_clicked)

        self._build_tags()
        self._reset_state()

    # ---------------------------------------------------------------- public

    def set_drive_info(self, text: str):
        """Show the target drive under the stats line."""
        self.drive_info.set_label(text or "")
        self.drive_info.set_visible(bool(text))

    def start_flashing(self):
        """Switch to the 'writing' state."""
        self._is_flashing = True
        self._last_fraction = 0.0
        self.hdd.set_progress(0.0)
        self.hdd.set_state("writing")
        self.hdd.set_active(True)
        self.status_heading.set_label(_("Writing Image"))
        self.status_message.set_label(_("Do not unplug the target drive."))
        self.error_detail.set_label("")
        self.error_detail.set_visible(False)
        self.phase_label.set_label("")
        self.phase_label.set_visible(False)
        self.progress_bar.set_fraction(0.0)
        self.progress_bar.remove_css_class("failed")
        self.progress_bar.remove_css_class("success")
        self._set_caption(_("Starting…"))
        self.details_expander.set_expanded(False)
        self._configure_actions(writing=True)

    def set_status(self, message: str):
        """Update the current writer phase (kept below the safety warning)."""
        if message:
            self.phase_label.set_label(message)
            self.phase_label.set_visible(True)

    def update_progress(self, fraction: float, status: str = "",
                        speed: float = 0, eta: int = 0):
        """Update the progress bar and the single caption line.

        Accepts a 0.0–1.0 fraction (preferred) or a 0–100 percentage from a
        backend, normalising both to the GtkProgressBar scale. Progress
        callbacks never claim the final tick: 100% belongs exclusively to
        :meth:`stop_flashing`, which runs after the write, flush and
        verification have finished.
        """
        try:
            value = float(fraction)
        except (TypeError, ValueError):
            value = 0.0
        if not math.isfinite(value):
            value = 0.0
        if value > 1.0:
            # Backend sent a percentage (e.g. 42 or 100).
            value /= 100.0
        self._last_fraction = max(0.0, min(0.999, value))
        self.progress_bar.set_fraction(self._last_fraction)
        self.hdd.set_progress(self._last_fraction)

        if status:
            self.set_status(status)

        if self._last_fraction > 0:
            parts = [f"{int(self._last_fraction * 100)}%"]
            if speed > 0:
                parts.append(_format_speed(speed))
            if eta > 0:
                parts.append(_("ETA {time}").format(time=_format_time(eta)))
            self._set_caption(" · ".join(parts))
        else:
            self._set_caption(_("Starting…"))

    def log(self, message: str, level: str = "info"):
        """Append a timestamped line to the details log."""
        tag = level if level in _TAGS else "info"
        line = f"[{time.strftime('%H:%M:%S')}] {message}\n"
        end_iter = self.console_buffer.get_end_iter()
        self.console_buffer.insert_with_tags_by_name(end_iter, line, tag)

        mark = self.console_buffer.create_mark(
            None, self.console_buffer.get_end_iter(), False
        )
        self._console_view.scroll_to_mark(mark, 0.0, True, 0.0, 1.0)

    def stop_flashing(self, success: bool, message: str = ""):
        """Switch to the completed or failed state."""
        self._is_flashing = False
        self.phase_label.set_label("")
        self.phase_label.set_visible(False)

        if success:
            self.progress_bar.set_fraction(1.0)
            self.progress_bar.add_css_class("success")
            self._set_caption(_("Complete"))
            self.hdd.set_progress(1.0)
            self.hdd.set_state("success")
            self.hdd.set_active(False)
            self.status_heading.set_label(_("Flash Complete"))
            self.status_message.set_label(
                _("The image was written successfully. You can safely unplug the drive.")
            )
            self.error_detail.set_label("")
            self.error_detail.set_visible(False)
            self.log(_("Flash completed successfully"), "success")
            if message:
                self.log(message, "info")
        else:
            # The bar stays exactly where the write stopped, in red: that
            # position is the failure point.
            percent = int(self._last_fraction * 100)
            self.progress_bar.set_fraction(self._last_fraction)
            self.progress_bar.add_css_class("failed")
            self._set_caption(
                _("Failed at {percent}%").format(percent=percent)
                if percent > 0 else _("Failed")
            )
            self.hdd.set_state("failed")
            self.hdd.set_active(False)
            self.status_heading.set_label(_("Flash Failed"))
            self.status_message.set_label(
                _("The drive was left unchanged. You can safely remove it or try again.")
            )
            self.error_detail.set_label(message or _("Unknown error"))
            self.error_detail.set_visible(bool(message))
            self.log(_("Flash failed"), "error")
            if message:
                self.log(message, "error")

        self._configure_actions(writing=False, success=success)

    def is_flashing(self) -> bool:
        return self._is_flashing

    # ------------------------------------------------------------- internals

    def _set_caption(self, text: str):
        """Show the caption line only when it has something to say."""
        self.status_caption.set_label(text or "")
        self.status_caption.set_visible(bool(text))

    def _reset_state(self):
        """Neutral starting point before the first flash."""
        self.hdd.set_active(False)
        self.hdd.set_state("neutral")
        self.hdd.set_progress(0.0)
        self.status_heading.set_label(_("Ready to Flash"))
        self.status_message.set_label(_("Waiting to start…"))
        self.error_detail.set_label("")
        self.error_detail.set_visible(False)
        self.phase_label.set_label("")
        self.phase_label.set_visible(False)
        self.progress_bar.set_fraction(0.0)
        self._set_caption("")
        self.drive_info.set_label("")
        self.drive_info.set_visible(False)
        self._configure_actions(writing=False, ready=True)

    def _configure_actions(self, writing: bool, success: bool = False, ready: bool = False):
        """Show only the buttons that make sense for the current state.

        Result actions appear only once the flash finished.
        """
        if writing or ready:
            self.retry_btn.set_visible(False)
            self.logs_btn.set_visible(False)
            self.close_btn.set_visible(False)
            return

        if success:
            self.retry_btn.set_visible(False)
            self.logs_btn.set_visible(False)
            self.close_btn.set_visible(True)
            self._set_primary(self.close_btn, True)
            self.close_btn.set_label(_("Done"))
        else:
            self.retry_btn.set_visible(True)
            self._set_primary(self.retry_btn, True)
            self.logs_btn.set_visible(True)
            self._set_primary(self.logs_btn, False)
            self.close_btn.set_visible(True)
            self._set_primary(self.close_btn, False)
            self.close_btn.set_label(_("Close"))

    @staticmethod
    def _set_primary(button, primary: bool):
        """Move a button between the suggested (primary) and flat styles."""
        if primary:
            button.add_css_class("suggested-action")
            button.remove_css_class("flat")
        else:
            button.remove_css_class("suggested-action")
            button.add_css_class("flat")

    def _on_retry_clicked(self, _btn):
        if self.on_try_again:
            self.on_try_again()

    def _on_close_clicked(self, _btn):
        if self.on_done:
            self.on_done()

    def _on_logs_clicked(self, _btn):
        """Reveal the technical log (also acts as the details toggle)."""
        expanded = self.details_expander.get_expanded()
        self.details_expander.set_expanded(not expanded)
        self.logs_btn.set_label(
            _("Hide Error Logs") if not expanded else _("View Error Logs")
        )

    def _on_copy_log_clicked(self, _btn):
        """Copy the whole log to the clipboard."""
        start, end = self.console_buffer.get_bounds()
        text = self.console_buffer.get_text(start, end, False)
        Gdk.Display.get_default().get_clipboard().set_text(text or "")
        self.copy_log_btn.set_label(_("Copied"))
        GLib.timeout_add(2000, self._reset_copy_label)

    def _reset_copy_label(self) -> bool:
        """Restore the copy button label (runs once via a 2s timeout)."""
        self.copy_log_btn.set_label(_("Copy Log"))
        return False

    def _build_tags(self):
        """Console tags differentiate lines by weight only (theme-safe)."""
        table = self.console_buffer.get_tag_table()
        for name in _TAGS:
            if table.lookup(name) is None:
                tag = Gtk.TextTag(name=name)
                if name in ("warning", "error", "success"):
                    tag.set_property("weight", Pango.Weight.BOLD)
                table.add(tag)

    def cleanup(self):
        """Clear the log and reset the page for the next run."""
        self.console_buffer.set_text("")
        self._reset_state()


def _format_speed(bytes_per_sec: float) -> str:
    if bytes_per_sec >= 1024**3:
        return f"{bytes_per_sec / 1024**3:.1f} GiB/s"
    if bytes_per_sec >= 1024**2:
        return f"{bytes_per_sec / 1024**2:.1f} MiB/s"
    if bytes_per_sec >= 1024:
        return f"{bytes_per_sec / 1024:.1f} KiB/s"
    return f"{bytes_per_sec:.0f} B/s"


def _format_time(seconds: int) -> str:
    if seconds < 60:
        return _("{seconds}s").format(seconds=int(seconds))
    if seconds < 3600:
        return _("{minutes}m {seconds}s").format(
            minutes=int(seconds // 60), seconds=int(seconds % 60)
        )
    return _("{hours}h {minutes}m").format(
        hours=int(seconds // 3600), minutes=int((seconds % 3600) // 60)
    )
