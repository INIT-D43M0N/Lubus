"""
Lubus - A modern bootable USB creator for Linux.

Functionally identical to Rufus, built with GTK4, Libadwaita, and Python.
"""

__version__ = "6.0.0"
__author__ = "Lubus Contributors"
__license__ = "GPL-3.0-or-later"
__description__ = "Create bootable USB drives on Linux (Rufus alternative)"

from .config import APP_CONFIG, CONFIG_DIR, DEFAULT_CONFIG, load_config, save_config
from .utils import _, set_language

__all__ = [
    "__version__",
    "__author__",
    "__license__",
    "__description__",
    "APP_CONFIG",
    "CONFIG_DIR",
    "DEFAULT_CONFIG",
    "load_config",
    "save_config",
    "_",
    "set_language",
]