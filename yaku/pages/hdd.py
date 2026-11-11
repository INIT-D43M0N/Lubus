"""Animated open-HDD writing graphic for the flash page (Step 4).

The flash screen used to show a generic flash-chip icon. It now shows an open
hard-disk casing with the character artwork (:data:`discfront.png`) as the
spinning platter and a moving actuator arm that reads as the head writing the
image.

Everything is drawn inside a small custom widget:

* the casing, recess, platter well, spindle hub, actuator arm, screws and
  status LED are recorded as Cairo nodes (``Gtk.Snapshot.append_cairo``);
* the platter artwork is a real ``Gdk.Texture``, clipped to a circle with
  ``push_rounded_clip`` and rotated with a 2D ``Gsk.Transform``.

That keeps the effect dependency-free: no WebKit view, no external image
animation, and the normal GTK renderer draws it. A tick callback only runs
while the drive is spinning (or slowing down), so an idle page costs no CPU.
"""
import logging
import math

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Gsk', '4.0')
from gi.repository import Gtk, Gdk, Gio, Gsk, Graphene

import cairo

from yaku.utils.resources import asset_file

log = logging.getLogger(__name__)

# Widget footprint (logical pixels). All drawing below is expressed in this
# coordinate space; the widget is not allowed to expand, so allocation matches.
WIDTH = 220
HEIGHT = 150

_BODY_MARGIN = 6.0
_RECESS_MARGIN = 20.0
_PLATTER_R = 52.0
_PLATTER_CX = 88.0
_PLATTER_CY = 76.0

# The shipped artwork fills ~97.6% of its square (the 1.2% anti-alias trim),
# so the texture square is slightly larger than the platter to make the
# artwork circle match the platter radius exactly — same convention as
# yaku.pages.disc.
_TEXTURE_FILL = 0.976

_PIVOT = (188.0, 120.0)
_ARM_LEN = 62.0
_ARM_REST = math.radians(204.0)
_ARM_SWEEP = math.radians(7.5)

_SPIN_DEG_PER_S = 235.0
_SPIN_UP = 3.0          # velocity easing per second
_SPIN_DOWN = 2.2
_SETTLE_BELOW = 90.0    # start easing upright once this slow

_STATE_COLORS = {
    "neutral": (0.60, 0.64, 0.70),
    "writing": (0.38, 0.67, 1.00),
    "success": (0.36, 0.84, 0.47),
    "failed": (0.96, 0.37, 0.37),
}

_TAU = math.pi * 2.0


def _load_texture(name: str):
    """Load a shipped PNG as a ``Gdk.Texture`` (``None`` if unavailable)."""
    try:
        path = asset_file(name)
        return Gdk.Texture.new_from_file(Gio.File.new_for_path(str(path)))
    except Exception:  # noqa: BLE001 - a missing asset must not break flashing
        log.warning("HDD platter asset %r could not be loaded", name, exc_info=True)
        return None


def _rounded_rect(cr, x, y, w, h, r):
    """Append a rounded-rectangle sub-path to a Cairo context."""
    r = max(0.0, min(r, w / 2.0, h / 2.0))
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2.0, 0.0)
    cr.arc(x + w - r, y + h - r, r, 0.0, math.pi / 2.0)
    cr.arc(x + r, y + h - r, r, math.pi / 2.0, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3.0 * math.pi / 2.0)
    cr.close_path()


class HddAnimation(Gtk.Widget):
    """An open hard-disk drive whose platter is the character disc.

    Public API used by :class:`yaku.pages.flash_page.FlashPage`:

    ``set_active(active)``
        Start or stop the platter; stopping spins down smoothly.
    ``set_state(state)``
        One of ``"neutral"``, ``"writing"``, ``"success"``, ``"failed"``;
        controls the status LED colour (accent / green / red).
    ``set_progress(fraction)``
        Optional 0..1 hint used to position the actuator arm.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._texture = _load_texture("discfront.png")

        self._angle = 0.0             # platter rotation in degrees
        self._velocity = 0.0          # current spin speed (degrees / second)
        self._arm_angle = _ARM_REST
        self._pulse = 0.0             # animation clock for the head glow
        self._progress = 0.0

        self._active = False
        self._state = "neutral"
        self._tick_id = 0
        self._last_time = None

        self.set_size_request(WIDTH, HEIGHT)
        self.set_hexpand(False)
        self.set_vexpand(False)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)
        self.set_can_target(False)

    # ---------------------------------------------------------------- public

    def set_active(self, active: bool):
        """Start or stop the spin (stopping eases out, it never snaps)."""
        active = bool(active)
        if active == self._active:
            return
        self._active = active
        if active:
            self._ensure_tick()
        elif not self._tick_id:
            # Nothing is moving and there is no tick: just redraw the arm.
            self.queue_draw()

    def set_state(self, state: str):
        """Set the status colour: neutral, writing, success or failed."""
        if state not in _STATE_COLORS:
            state = "neutral"
        if state == self._state:
            return
        self._state = state
        self.queue_draw()

    def set_progress(self, fraction: float):
        """Remember the fraction so the arm sits at the write position."""
        self._progress = max(0.0, min(1.0, float(fraction)))
        if not self._active:
            self.queue_draw()

    # ------------------------------------------------------------ animation

    def _ensure_tick(self):
        if not self._tick_id:
            self._last_time = None
            self._tick_id = self.add_tick_callback(self._on_tick)

    def _on_tick(self, _widget, clock):
        now = clock.get_frame_time()
        if self._last_time is None:
            self._last_time = now
            return True
        dt = min(0.05, max(0.0, (now - self._last_time) / 1_000_000.0))
        self._last_time = now

        target = _SPIN_DEG_PER_S if self._active else 0.0
        easing = _SPIN_UP if self._active else _SPIN_DOWN
        self._velocity += (target - self._velocity) * min(1.0, dt * easing)
        self._angle = (self._angle + self._velocity * dt) % 360.0

        # Once it is slow enough, park the platter with the artwork upright
        # (shortest way round) so a finished flash does not freeze the
        # character sideways or upside-down.
        delta = 0.0
        if not self._active and self._velocity < _SETTLE_BELOW:
            delta = -((self._angle + 180.0) % 360.0 - 180.0)
            self._angle = (self._angle + delta * min(1.0, dt * 7.0)) % 360.0

        # The arm creeps across the platter as the write advances, and
        # sweeps back and forth around that position while writing.
        base = _ARM_REST + (self._progress - 0.5) * math.radians(5.0)
        if self._active:
            self._pulse = (self._pulse + dt * 2.6) % _TAU
            arm_target = base + _ARM_SWEEP * math.sin(self._pulse)
        else:
            arm_target = base
        self._arm_angle += (arm_target - self._arm_angle) * min(1.0, dt * 6.0)

        self.queue_draw()

        if (not self._active and self._velocity < 8.0
                and abs(delta) < 1.0):
            # Fully spun down and parked: stop burning frames.
            self._velocity = 0.0
            self._angle = 0.0
            self._arm_angle = base
            self._tick_id = 0
            self._last_time = None
            self.queue_draw()
            return False
        return True

    # --------------------------------------------------------------- drawing

    def do_measure(self, orientation, _for_size):
        size = WIDTH if orientation == Gtk.Orientation.HORIZONTAL else HEIGHT
        return size, size, -1, -1

    def do_snapshot(self, snapshot):
        if self.get_width() <= 0 or self.get_height() <= 0:
            return
        bounds = Graphene.Rect().init(0.0, 0.0, self.get_width(), self.get_height())
        self._draw_base(snapshot, bounds)
        self._draw_platter(snapshot)
        self._draw_details(snapshot, bounds)

    # -- layer 1: casing, recess and platter well --------------------------

    def _draw_base(self, snapshot, bounds):
        cr = snapshot.append_cairo(bounds)
        w, h = self.get_width(), self.get_height()
        m = _BODY_MARGIN

        # Soft cast shadow under the drive.
        cr.set_source_rgba(0.0, 0.0, 0.0, 0.30)
        _rounded_rect(cr, m, m + 4.0, w - 2 * m, h - 2 * m, 16.0)
        cr.fill()

        # Brushed-metal casing.
        body = cairo.LinearGradient(0.0, m, 0.0, h - m)
        body.add_color_stop_rgb(0.00, 0.27, 0.29, 0.33)
        body.add_color_stop_rgb(0.45, 0.19, 0.21, 0.24)
        body.add_color_stop_rgb(1.00, 0.10, 0.11, 0.13)
        cr.set_source(body)
        _rounded_rect(cr, m, m, w - 2 * m, h - 2 * m, 16.0)
        cr.fill()

        # Rim highlight.
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.10)
        cr.set_line_width(1.0)
        _rounded_rect(cr, m + 0.6, m + 0.6, w - 2 * m - 1.2, h - 2 * m - 1.2, 15.4)
        cr.stroke()

        # Recessed interior.
        r = _RECESS_MARGIN
        inner = cairo.LinearGradient(0.0, r, 0.0, h - r)
        inner.add_color_stop_rgb(0.00, 0.035, 0.040, 0.050)
        inner.add_color_stop_rgb(1.00, 0.100, 0.110, 0.130)
        cr.set_source(inner)
        _rounded_rect(cr, r, r, w - 2 * r, h - 2 * r, 10.0)
        cr.fill()

        # Recess lip (dark inner shadow, then a faint top light).
        cr.set_source_rgba(0.0, 0.0, 0.0, 0.55)
        cr.set_line_width(1.4)
        _rounded_rect(cr, r + 0.7, r + 0.7, w - 2 * r - 1.4, h - 2 * r - 1.4, 9.4)
        cr.stroke()
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.05)
        cr.set_line_width(1.0)
        _rounded_rect(cr, r + 1.6, r + 1.6, w - 2 * r - 3.2, h - 2 * r - 3.2, 8.6)
        cr.stroke()

        # Platter well shadow.
        cx, cy, radius = _PLATTER_CX, _PLATTER_CY, _PLATTER_R
        well = cairo.RadialGradient(cx, cy + 3.0, radius * 0.55,
                                    cx, cy + 3.0, radius + 7.0)
        well.add_color_stop_rgba(0.80, 0.0, 0.0, 0.0, 0.00)
        well.add_color_stop_rgba(1.00, 0.0, 0.0, 0.0, 0.55)
        cr.set_source(well)
        cr.arc(cx, cy, radius + 7.0, 0.0, _TAU)
        cr.fill()

        # Base platter (visible only if the artwork asset is missing).
        base = cairo.RadialGradient(cx, cy, 4.0, cx, cy, radius)
        base.add_color_stop_rgb(0.00, 0.30, 0.32, 0.36)
        base.add_color_stop_rgb(0.70, 0.16, 0.17, 0.20)
        base.add_color_stop_rgb(1.00, 0.09, 0.10, 0.12)
        cr.set_source(base)
        cr.arc(cx, cy, radius, 0.0, _TAU)
        cr.fill()

    # -- layer 2: the spinning character platter ---------------------------

    def _draw_platter(self, snapshot):
        texture = self._texture
        if texture is None:
            return

        cx, cy, radius = _PLATTER_CX, _PLATTER_CY, _PLATTER_R
        outline = Gsk.RoundedRect()
        outline.init_from_rect(
            Graphene.Rect().init(cx - radius, cy - radius, radius * 2, radius * 2),
            radius,
        )
        snapshot.push_rounded_clip(outline)

        transform = (
            Gsk.Transform.new()
            .translate(Graphene.Point().init(cx, cy))
            .rotate(self._angle)
            .translate(Graphene.Point().init(-cx, -cy))
        )
        snapshot.transform(transform)

        side = 2.0 * radius / _TEXTURE_FILL
        snapshot.append_texture(
            texture,
            Graphene.Rect().init(cx - side / 2.0, cy - side / 2.0, side, side),
        )
        snapshot.pop()

    # -- layer 3: rim, spindle, actuator, screws and LED -------------------

    def _draw_details(self, snapshot, bounds):
        cr = snapshot.append_cairo(bounds)
        cx, cy, radius = _PLATTER_CX, _PLATTER_CY, _PLATTER_R

        # Edge vignette over the artwork: it reads as a platter set into the
        # casing instead of a flat sticker.
        vignette = cairo.RadialGradient(cx, cy, radius * 0.45, cx, cy, radius)
        vignette.add_color_stop_rgba(0.62, 0.0, 0.0, 0.0, 0.00)
        vignette.add_color_stop_rgba(1.00, 0.0, 0.0, 0.0, 0.42)
        cr.set_source(vignette)
        cr.arc(cx, cy, radius, 0.0, _TAU)
        cr.fill()

        # Machined platter rim.
        rim = cairo.LinearGradient(cx - radius, cy - radius,
                                   cx + radius, cy + radius)
        rim.add_color_stop_rgb(0.00, 0.72, 0.76, 0.82)
        rim.add_color_stop_rgb(0.50, 0.38, 0.41, 0.46)
        rim.add_color_stop_rgb(1.00, 0.68, 0.72, 0.78)
        cr.set_source(rim)
        cr.set_line_width(2.4)
        cr.arc(cx, cy, radius - 1.2, 0.0, _TAU)
        cr.stroke()
        cr.set_source_rgba(0.0, 0.0, 0.0, 0.45)
        cr.set_line_width(1.0)
        cr.arc(cx, cy, radius + 1.4, 0.0, _TAU)
        cr.stroke()

        self._draw_spindle(cr)
        self._draw_arm(cr)
        self._draw_screws(cr)
        self._draw_led(cr)

    def _draw_spindle(self, cr):
        cx, cy = _PLATTER_CX, _PLATTER_CY

        hub = cairo.RadialGradient(cx - 2.0, cy - 2.0, 1.0, cx, cy, 11.0)
        hub.add_color_stop_rgb(0.00, 0.80, 0.83, 0.88)
        hub.add_color_stop_rgb(0.50, 0.44, 0.47, 0.52)
        hub.add_color_stop_rgb(1.00, 0.16, 0.17, 0.20)
        cr.set_source(hub)
        cr.arc(cx, cy, 10.5, 0.0, _TAU)
        cr.fill()

        cr.set_source_rgba(0.0, 0.0, 0.0, 0.55)
        cr.set_line_width(1.0)
        cr.arc(cx, cy, 10.5, 0.0, _TAU)
        cr.stroke()
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.35)
        cr.arc(cx, cy, 7.6, 0.0, _TAU)
        cr.stroke()

        cr.set_source_rgb(0.10, 0.11, 0.13)
        cr.arc(cx, cy, 3.6, 0.0, _TAU)
        cr.fill()
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.50)
        cr.arc(cx - 0.8, cy - 0.8, 1.1, 0.0, _TAU)
        cr.fill()

    def _draw_arm(self, cr):
        px, py = _PIVOT
        angle = self._arm_angle
        tip_x = px + math.cos(angle) * _ARM_LEN
        tip_y = py + math.sin(angle) * _ARM_LEN
        red, green, blue = _STATE_COLORS.get(self._state, _STATE_COLORS["neutral"])

        cr.set_line_cap(cairo.LINE_CAP_ROUND)

        # Arm shadow on the platter.
        cr.set_source_rgba(0.0, 0.0, 0.0, 0.55)
        cr.set_line_width(11.0)
        cr.move_to(px + 2.0, py + 3.0)
        cr.line_to(tip_x + 2.0, tip_y + 3.0)
        cr.stroke()

        # Brushed aluminium arm.
        arm = cairo.LinearGradient(px, py, tip_x, tip_y)
        arm.add_color_stop_rgb(0.00, 0.52, 0.55, 0.60)
        arm.add_color_stop_rgb(0.55, 0.78, 0.81, 0.86)
        arm.add_color_stop_rgb(1.00, 0.55, 0.58, 0.63)
        cr.set_source(arm)
        cr.set_line_width(9.0)
        cr.move_to(px, py)
        cr.line_to(tip_x, tip_y)
        cr.stroke()

        cr.set_source_rgba(1.0, 1.0, 1.0, 0.30)
        cr.set_line_width(3.0)
        cr.move_to(px, py)
        cr.line_to(tip_x, tip_y)
        cr.stroke()

        # Read/write head at the tip.
        cr.save()
        cr.translate(tip_x, tip_y)
        cr.rotate(angle)
        head = cairo.LinearGradient(0.0, -5.5, 0.0, 5.5)
        head.add_color_stop_rgb(0.00, 0.40, 0.43, 0.48)
        head.add_color_stop_rgb(1.00, 0.08, 0.09, 0.11)
        cr.set_source(head)
        _rounded_rect(cr, -7.0, -5.5, 14.0, 11.0, 3.0)
        cr.fill()
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.18)
        cr.set_line_width(0.8)
        _rounded_rect(cr, -7.0, -5.5, 14.0, 11.0, 3.0)
        cr.stroke()

        # Writing spark: a small accent pulse just ahead of the head.
        if self._active:
            pulse = 0.35 + 0.30 * (0.5 + 0.5 * math.sin(self._pulse * 3.0))
            cr.set_source_rgba(red, green, blue, pulse)
            cr.arc(-3.0, 0.0, 4.0, 0.0, _TAU)
            cr.fill()
            cr.set_source_rgba(1.0, 1.0, 1.0, pulse * 0.9)
            cr.arc(-3.0, 0.0, 1.4, 0.0, _TAU)
            cr.fill()
        cr.restore()

        # Pivot bearing.
        pivot = cairo.RadialGradient(px - 2.0, py - 2.0, 1.0, px, py, 9.0)
        pivot.add_color_stop_rgb(0.00, 0.72, 0.75, 0.80)
        pivot.add_color_stop_rgb(1.00, 0.16, 0.17, 0.20)
        cr.set_source(pivot)
        cr.arc(px, py, 8.5, 0.0, _TAU)
        cr.fill()
        cr.set_source_rgba(0.0, 0.0, 0.0, 0.60)
        cr.set_line_width(1.0)
        cr.arc(px, py, 8.5, 0.0, _TAU)
        cr.stroke()
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.45)
        cr.arc(px - 2.0, py - 2.0, 1.6, 0.0, _TAU)
        cr.fill()

    def _draw_screws(self, cr):
        w, h = self.get_width(), self.get_height()
        for sx, sy in ((15.0, 15.0), (w - 15.0, 15.0),
                       (15.0, h - 15.0), (w - 15.0, h - 15.0)):
            screw = cairo.RadialGradient(sx - 0.8, sy - 0.8, 0.5, sx, sy, 3.6)
            screw.add_color_stop_rgb(0.00, 0.62, 0.65, 0.70)
            screw.add_color_stop_rgb(1.00, 0.16, 0.17, 0.20)
            cr.set_source(screw)
            cr.arc(sx, sy, 3.2, 0.0, _TAU)
            cr.fill()
            cr.set_source_rgba(0.0, 0.0, 0.0, 0.55)
            cr.set_line_width(0.8)
            cr.arc(sx, sy, 3.2, 0.0, _TAU)
            cr.stroke()
            cr.set_source_rgba(0.0, 0.0, 0.0, 0.65)
            cr.set_line_width(1.0)
            cr.move_to(sx - 1.8, sy)
            cr.line_to(sx + 1.8, sy)
            cr.stroke()

    def _draw_led(self, cr):
        red, green, blue = _STATE_COLORS.get(self._state, _STATE_COLORS["neutral"])
        x, y = 30.0, self.get_height() - 30.0

        # Status glow. Idle states are dimmer so they do not shout.
        strength = 0.55 if self._state in ("writing", "success", "failed") else 0.22
        if self._active:
            strength = 0.55 + 0.18 * (0.5 + 0.5 * math.sin(self._pulse * 2.0))
        glow = cairo.RadialGradient(x, y, 0.5, x, y, 12.0)
        glow.add_color_stop_rgba(0.00, red, green, blue, strength)
        glow.add_color_stop_rgba(1.00, red, green, blue, 0.0)
        cr.set_source(glow)
        cr.arc(x, y, 12.0, 0.0, _TAU)
        cr.fill()

        cr.set_source_rgba(red, green, blue, 0.95)
        cr.arc(x, y, 2.8, 0.0, _TAU)
        cr.fill()
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.55)
        cr.arc(x - 0.7, y - 0.7, 0.9, 0.0, _TAU)
        cr.fill()
