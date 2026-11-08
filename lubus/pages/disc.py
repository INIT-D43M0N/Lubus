"""Interactive 3D disc easter egg for the image-selection page.

Step 2 used to show the symbolic ``media-optical`` icon. It now shows the
holographic back of a disc; clicking it flips the disc around its vertical
axis to reveal the character artwork on the front (and back again on the
next click).

GTK4 has no CSS 3D transforms, so the flip is drawn with GSK's 3D transform
nodes (``Gsk.Transform`` with ``perspective`` + ``rotate_3d``) inside a small
custom widget. This keeps the effect dependency-free (no WebKit view) and
hardware accelerated through the normal GTK renderer, while still giving a
real perspective flip rather than a 2D "flatten" fake.
"""
import logging
import math

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Gsk', '4.0')
from gi.repository import Gtk, Gdk, Gio, Gsk, Graphene

from lubus.utils.i18n import _
from lubus.utils.resources import asset_file

log = logging.getLogger(__name__)

# Outer widget footprint. The artwork disc itself is slightly smaller
# because the widget keeps padding for the drop shadow.
DEFAULT_SIZE = 148
_FLIP_MS = 520


def _load_texture(name: str):
    """Load a shipped PNG as a ``Gdk.Texture`` (``None`` if unavailable)."""
    try:
        path = asset_file(name)
        return Gdk.Texture.new_from_file(Gio.File.new_for_path(str(path)))
    except Exception:  # noqa: BLE001 - a missing easter-egg asset is not fatal
        log.warning("Disc asset %r could not be loaded", name, exc_info=True)
        return None


def _ease_in_out(t: float) -> float:
    """Cubic ease-in-out: a gentle wind-up and settle for the flip."""
    if t < 0.5:
        return 4.0 * t * t * t
    return 1.0 - pow(-2.0 * t + 2.0, 3) / 2.0


class DiscFlip(Gtk.Widget):
    """A circular disc that flips 180° about the Y axis when clicked.

    ``angle == 0`` shows the holographic back; ``angle == 180`` shows the
    artwork front. Half-way through the animation the disc is edge-on, and
    the rendered face switches from back to front so the reveal is seamless.
    """

    def __init__(self, size: int = DEFAULT_SIZE, **kwargs):
        super().__init__(**kwargs)
        self._size = size
        self._back = _load_texture("discback.png")
        self._front = _load_texture("discfront.png")

        self._angle = 0.0          # current rotation in degrees
        self._target = 0.0         # 0 = back, 180 = front
        self._anim_from = 0.0
        self._anim_start = None     # frame time (µs) of the current segment
        self._tick_id = 0
        self._face = 0              # 0 = back, 1 = front (requested face)

        self.set_size_request(size, size)
        self.set_hexpand(False)
        self.set_vexpand(False)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)
        self.set_focusable(True)
        self.set_tooltip_text(_("Click to flip the disc"))
        self.add_css_class("disc-flip")
        try:
            self.set_cursor_from_name("pointer")
        except Exception:  # pragma: no cover - no display in some test setups
            pass

        click = Gtk.GestureClick()
        click.connect("released", self._on_clicked)
        self.add_controller(click)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key_pressed)
        self.add_controller(keys)

    # ---------------------------------------------------------------- public

    def flip(self):
        """Toggle the visible face (the easter egg action)."""
        self._face = 0 if self._face else 1
        self._target = 180.0 * self._face
        self._anim_from = self._angle
        self._anim_start = None
        if not self._tick_id:
            self._tick_id = self.add_tick_callback(self._on_tick)

    def reset(self):
        """Return to the holographic back without animating."""
        self._face = 0
        self._target = 0.0
        self._angle = 0.0
        self._anim_start = None
        if self._tick_id:
            self.remove_tick_callback(self._tick_id)
            self._tick_id = 0
        self.queue_draw()

    # ------------------------------------------------------------ internals

    def _on_clicked(self, _gesture, _n_press, _x, _y):
        self.flip()

    def _on_key_pressed(self, _ctrl, keyval, _keycode, _state):
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter,
                      Gdk.KEY_space, Gdk.KEY_KP_Space):
            self.flip()
            return True
        return False

    def _on_tick(self, _widget, clock):
        now = clock.get_frame_time()
        if self._anim_start is None:
            self._anim_start = now
        elapsed = (now - self._anim_start) / (_FLIP_MS * 1000.0)
        t = min(1.0, max(0.0, elapsed))
        self._angle = self._anim_from + (self._target - self._anim_from) * _ease_in_out(t)
        self.queue_draw()

        if t >= 1.0:
            self._angle = self._target
            self._tick_id = 0
            return False
        return True

    # --------------------------------------------------------------- drawing

    def do_measure(self, _orientation, _for_size):
        return self._size, self._size, -1, -1

    def do_snapshot(self, snapshot):
        texture = self._back if self._angle <= 90.0 else self._front
        if texture is None:
            return

        width = self.get_width()
        height = self.get_height()
        if width <= 0 or height <= 0:
            return

        size = min(width, height)
        pad = size * 0.07
        disc = size - 2 * pad                       # square holding the artwork
        cx, cy = width / 2.0, height / 2.0
        angle = self._angle

        # Face selection: 0..90 shows the back, 90..180 the front. The front
        # face is drawn with angle-180 so it is never mirrored.
        rotation = angle if angle <= 90.0 else angle - 180.0

        # Soft drop shadow, squashed with the disc so it reads as a physical
        # object rotating above the page rather than a flat sticker.
        flat = abs(math.cos(math.radians(angle)))
        shadow_w = max(disc * flat, disc * 0.14)
        shadow_h = disc * 0.96
        shadow_rect = Graphene.Rect().init(
            cx - shadow_w / 2.0, cy - shadow_h / 2.0, shadow_w, shadow_h
        )
        outline = Gsk.RoundedRect()
        outline.init_from_rect(shadow_rect, min(shadow_w, shadow_h) / 2.0)
        shadow_color = Gdk.RGBA(
            red=0.0, green=0.0, blue=0.0, alpha=0.34 + 0.16 * flat
        )
        snapshot.append_outset_shadow(
            outline,
            shadow_color,
            0.0,
            disc * 0.05,        # dy
            0.0,                # spread
            disc * 0.06,        # blur
        )

        # Perspective flip around the vertical axis, pivoting on the centre.
        transform = Gsk.Transform.new()
        centre = Graphene.Point().init(cx, cy)
        offset = Graphene.Point().init(-cx, -cy)
        transform = transform.translate(centre)
        transform = transform.perspective(max(size * 2.2, 240.0))
        transform = transform.rotate_3d(rotation, Graphene.Vec3().init(0.0, 1.0, 0.0))
        transform = transform.translate(offset)
        snapshot.transform(transform)

        snapshot.append_texture(
            texture, Graphene.Rect().init(cx - disc / 2.0, cy - disc / 2.0, disc, disc)
        )
