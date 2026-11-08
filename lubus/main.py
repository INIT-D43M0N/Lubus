"""Main entry point for Lubus."""
import sys
import os
import argparse

# Handle --version and --help BEFORE importing GTK (which requires a display)
def _handle_early_args():
    parser = argparse.ArgumentParser(prog="lubus", add_help=False)
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--help", action="store_true")
    try:
        parsed, _ = parser.parse_known_args()
    except SystemExit:
        return None
    if parsed.version:
        print("Lubus 5.0.0")
        return 0
    if parsed.help:
        _print_help()
        return 0
    return None

def _print_help():
    print("""
Lubus - Bootable USB Creator for Linux

Usage:
  lubus [OPTIONS] [ISO_FILE]

Options:
  --iso PATH         Path to ISO/IMG file to flash
  --drive DEVICE     Target drive device (e.g., /dev/sdb)
  --debug            Enable debug logging
  --windows-skip-oobe
                     Skip Windows OOBE/privacy screens
  --windows-bypass-requirements
                     Apply Windows 11 requirement bypasses
  --windows-remove-defender
                     Remove Windows Defender during image preparation
  --version          Show version and exit
  --help             Show this help and exit

Examples:
  lubus --iso ubuntu.iso --drive /dev/sdb
  lubus ubuntu.iso

Supported image formats: .iso, .img, .dd, .raw, .bin
""".strip())

# Check early args before GTK initialization
early_result = _handle_early_args()
if early_result is not None:
    sys.exit(early_result)

# Now import GTK (requires display)
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, Gio, GLib

from .utils.logging import setup_logging, get_logger
from .config import load_config, APP_CONFIG
from .utils.i18n import set_language
from .window import LubusWindow

log = get_logger(__name__)


class LubusApplication(Adw.Application):
    """Main application class."""

    def __init__(self, **kwargs):
        super().__init__(
            application_id="com.github.yourusername.lubus",
            flags=Gio.ApplicationFlags.HANDLES_OPEN | Gio.ApplicationFlags.HANDLES_COMMAND_LINE | Gio.ApplicationFlags.NON_UNIQUE,
            **kwargs
        )
        self._window = None
        self._iso_path = None
        self._drive_device = None
        self._windows_options = {}

        # Connect signals
        self.connect("activate", self.on_activate)
        self.connect("open", self.on_open)
        self.connect("command-line", self.on_command_line)

        # Global quit action (Ctrl+Q)
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_args: self.quit())
        self.add_action(quit_action)

        # Accelerators are application-global in GTK4, including win.* ones
        self.set_accels_for_action("app.quit", ["<Primary>q"])
        self.set_accels_for_action("win.refresh", ["<Primary>r"])
        self.set_accels_for_action("win.choose-image", ["<Primary>o"])
        self.set_accels_for_action("win.preferences", ["<Primary>comma"])
        self.set_accels_for_action("win.shortcuts", ["<Primary>question"])

        # Light/dark style follows the system automatically through
        # Adw.StyleManager; no explicit handling needed here.

    def on_activate(self, app):
        """Handle application activation."""
        if not self._window:
            self._window = LubusWindow(
                app,
                iso_path=self._iso_path,
                drive_device=self._drive_device,
                windows_options=self._windows_options,
            )
        self._window.present()

    def do_startup(self):
        """GTK is initialized here: apply the app icon and stylesheet."""
        Adw.Application.do_startup(self)
        # Match the installed desktop icon (X11 window icon / About dialog).
        Gtk.Window.set_default_icon_name("com.github.yourusername.lubus")
        self._load_custom_css()

    def _load_custom_css(self):
        """Load the minimal stylesheet (Libadwaita provides the rest)."""
        try:
            from lubus.utils.resources import data_dir
            css_path = data_dir() / "styles.css"
            if not css_path.is_file():
                return
            provider = Gtk.CssProvider()
            provider.load_from_path(str(css_path))
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(),
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )
        except GLib.Error as e:
            log.debug("Custom stylesheet not loaded: %s", e.message)

    def on_open(self, app, files, hint):
        """Handle files opened via D-Bus (e.g., double-click ISO)."""
        for file in files:
            path = file.get_path()
            if path and path.lower().endswith((".iso", ".img", ".dd", ".raw", ".bin")):
                self._iso_path = path
                break
        self.activate()

    def on_command_line(self, app, command_line):
        """Handle command line arguments (for --iso, --drive, --debug)."""
        options = command_line.get_options_dict()
        args_dict = options.end().unpack()
        # GLib.VariantDict unpack returns a dict, get the command line args
        args = sys.argv[1:]  # Use sys.argv directly for command line

        parser = argparse.ArgumentParser(prog="lubus", add_help=False)
        parser.add_argument("--iso", type=str, help="Path to ISO image")
        parser.add_argument("--drive", type=str, help="Target drive device (e.g., /dev/sdb)")
        parser.add_argument("--debug", action="store_true", help="Enable debug logging")
        parser.add_argument("--windows-skip-oobe", action="store_true", default=None,
                            help="Skip Windows OOBE/privacy screens")
        parser.add_argument("--windows-bypass-requirements", action="store_true",
                            default=None,
                            help="Apply Windows 11 TPM/Secure Boot/RAM/CPU bypasses")
        parser.add_argument("--windows-remove-defender", action="store_true",
                            default=None,
                            help="Remove Windows Defender when preparing the image")

        try:
            parsed = parser.parse_args(args)
        except SystemExit:
            return 1

        if parsed.debug:
            os.environ["LUBUS_DEBUG"] = "1"

        if parsed.iso:
            self._iso_path = parsed.iso
        if parsed.drive:
            self._drive_device = parsed.drive
        if parsed.windows_skip_oobe is not None:
            self._windows_options["skip_oobe"] = True
        if parsed.windows_bypass_requirements is not None:
            self._windows_options["bypass_win11"] = True
        if parsed.windows_remove_defender is not None:
            self._windows_options["remove_defender"] = True

        self.activate()
        return 0


def main():
    """Main entry point."""
    # Parse early args for debug
    debug = "--debug" in sys.argv or os.environ.get("LUBUS_DEBUG") == "1"

    # Setup logging
    from .utils.logging import setup_logging
    setup_logging(debug=debug)

    # Load config and set language
    from .config import load_config, APP_CONFIG
    from .utils.i18n import set_language
    load_config()
    if APP_CONFIG.get("lang"):
        from .utils.i18n import set_language
        set_language(APP_CONFIG["lang"])

    # Create and run application
    app = LubusApplication()
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())