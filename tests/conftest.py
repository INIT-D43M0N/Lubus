"""Pytest configuration for Yaku tests."""
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Ensure GI is initialized early
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')

# Import after GI setup
from gi.repository import Gtk, Adw, GLib

# Initialize GTK for tests
Gtk.init()

# Set up Adw
Adw.init()