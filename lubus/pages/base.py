"""Shared helpers for Lubus wizard pages.

Pages keep the GNOME-style separation used across the app: layout lives in
``data/ui/<page>.ui`` (clean GTK4/libadwaita XML, translatable via the app's
gettext domain), while this Python layer wires behaviour. GTK4 widget classes
are final in some cases, so composition is used instead of inheritance.
"""
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk

from ..utils.resources import ui_file


class UiPage:
    """Base class for pages whose layout is defined in a .ui file.

    Subclasses set ``ui_name`` (file under ``data/ui/``) and ``root_id``
    (the id of the root object inside that file). Named widgets are then
    reachable with :meth:`get` and the published widget is ``self.widget``.
    """

    ui_name: str = ""
    root_id: str = "root"

    def __init__(self):
        if not self.ui_name:
            raise NotImplementedError("Subclasses must define ui_name")
        self._builder = Gtk.Builder()
        # translatable="yes" strings resolve through the "lubus" gettext
        # domain, keeping .ui files compatible with the Python translations.
        self._builder.set_translation_domain("lubus")
        self._builder.add_from_file(str(ui_file(self.ui_name)))
        self.widget = self.get(self.root_id)

    def get(self, name: str):
        """Return a named object from the page's .ui file."""
        obj = self._builder.get_object(name)
        if obj is None:
            raise RuntimeError(
                f"{self.ui_name}: object '{name}' is missing from the UI file"
            )
        return obj

    def get_widget(self):
        """Widget to add to the wizard's AdwViewStack."""
        return self.widget

    def cleanup(self):
        """Hook for pages that own background work or temporary state."""


def set_row_subtitle(row, subtitle: str):
    """Set an AdwPreferencesRow subtitle, tolerating empty text."""
    if subtitle:
        row.set_subtitle(subtitle)
        row.set_visible(True)
    else:
        row.set_subtitle("—")
        row.set_visible(False)
