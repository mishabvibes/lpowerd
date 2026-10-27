"""lpowerd-gui - hardware-dashboard style settings window for lpowerd.

The accent colour follows the active profile the way the LOQ/Legion power
button LED does (quiet = blue, balanced = white, performance = red).
Animations run only while something is changing, or while charging with the
window focused, so an idle window draws no frames.
"""

import json
import math
import os
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

import dbus  # noqa: E402
from dbus.mainloop.glib import DBusGMainLoop  # noqa: E402
from gi.repository import Adw, Gdk, GLib, Graphene, Gsk, Gtk  # noqa: E402

from . import BUS_NAME, INTERFACE, OBJECT_PATH  # noqa: E402

APP_ID = "io.github.lpowerd.Gui"

# name -> (label, icon, blurb, LED colour)
PROFILES = {
    "quiet": ("Quiet", "power-profile-power-saver-symbolic", "Cool & silent", "#3d8bff"),
    "balance": ("Balanced", "power-profile-balanced-symbolic", "Everyday use", "#e9eef6"),
    "performance": ("Performance", "power-profile-performance-symbolic", "Gaming & heavy work",
                    "#ff3b4e"),
    "extreme": ("Extreme", "power-profile-performance-symbolic", "Maximum power", "#ff3bd4"),
    "custom": ("Custom", "preferences-system-symbolic", "Firmware custom", "#b48cff"),
}
WARN = "#ffb020"
HOT = "#ff7a1a"


def hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _profile_css():
    out = []
    for name, (_, _, _, col) in PROFILES.items():
        r, g, b = (int(x * 255) for x in hex_rgb(col))
        on_accent = "#0b0e13" if name == "balance" else "#ffffff"
        out.append(f"""
.p-{name} switch:checked {{ background-color: {col}; }}
.p-{name} switch:checked > slider {{ background-color: {on_accent}; }}
.p-{name} scale > trough > highlight {{ background-color: {col}; }}
.p-{name} .accent-text {{ color: {col}; }}
.p-{name} button.cta {{ background-color: {col}; color: {on_accent}; }}
.p-{name} spinbutton:focus-within {{ border-color: {col}; }}
.profile-card.pc-{name}:checked {{ border-color: {col};
    background-image: linear-gradient(180deg, rgba({r},{g},{b},0.16), rgba({r},{g},{b},0.03)); }}
.profile-card.pc-{name}:checked image {{ color: {col}; }}
.profile-card.pc-{name}:checked .pc-led {{ background-color: {col}; }}
""")
    return "".join(out)


CSS = """
window.dash { background-color: #0b0e13; color: #e6ebf2; }
window.dash headerbar { background: transparent; box-shadow: none; border: none; }
.brand { font-weight: 800; letter-spacing: 4px; font-size: 13px; }
.brand-dim { color: alpha(#e6ebf2, 0.45); font-weight: 600; letter-spacing: 4px; font-size: 13px; }
.eyebrow { font-size: 11px; font-weight: 700; letter-spacing: 2.5px; color: alpha(#e6ebf2, 0.45); }
.card {
  background-image: linear-gradient(180deg, #151b25, #11161e);
  border: 1px solid #1f2733; border-radius: 16px; padding: 16px;
  box-shadow: none;
}
.pct { font-size: 52px; font-weight: 800; font-feature-settings: "tnum"; letter-spacing: -1px; }
.pct-unit { font-size: 22px; font-weight: 700; color: alpha(#e6ebf2, 0.55); }
.hero-state { font-size: 12px; font-weight: 700; letter-spacing: 2px; }
.hero-sub { font-size: 12px; color: alpha(#e6ebf2, 0.55); }
.chip { box-shadow: none; background-color: #121821; border: 1px solid #1f2733; border-radius: 12px; padding: 8px 10px; }
.chip-label { font-size: 10px; font-weight: 700; letter-spacing: 1.5px; color: alpha(#e6ebf2, 0.45); }
.chip-value { font-size: 15px; font-weight: 700; font-feature-settings: "tnum"; }
.profile-card {
  background-color: #121821; border: 1px solid #1f2733; border-radius: 14px;
  padding: 14px 8px 12px 8px; color: #e6ebf2; box-shadow: none;
  transition: border-color 350ms ease, background-image 350ms ease;
}
.profile-card:hover { border-color: #33405a; }
.profile-card image { color: alpha(#e6ebf2, 0.6); transition: color 350ms ease; }
.pc-name { font-weight: 800; font-size: 13px; letter-spacing: 0.5px; }
.pc-blurb { font-size: 11px; color: alpha(#e6ebf2, 0.5); }
.pc-led { min-width: 18px; min-height: 3px; border-radius: 2px; background-color: #26303e;
  transition: background-color 350ms ease; }
.row-title { font-weight: 600; }
.row-sub { font-size: 12px; color: alpha(#e6ebf2, 0.5); }
.sep { background-color: #1f2733; min-height: 1px; margin: 12px 0; }
.limit-value { font-weight: 800; font-size: 15px; min-width: 44px; font-feature-settings: "tnum"; }
.note { font-size: 11px; color: alpha(#e6ebf2, 0.45); }
.alert { background-color: alpha(#ff7a1a, 0.12); border: 1px solid alpha(#ff7a1a, 0.5);
  border-radius: 12px; padding: 10px 14px; color: #ffb27a; font-weight: 600; }
.alert.down { background-color: alpha(#ff3b4e, 0.12); border-color: alpha(#ff3b4e, 0.5); color: #ff8f9b; }
button.cta { border-radius: 10px; font-weight: 800; padding: 6px 16px; box-shadow: none;
  transition: background-color 350ms ease; }
button.cta:disabled { background-color: #1c2430; color: alpha(#e6ebf2, 0.35); }
switch { transition: background-color 300ms ease; }
spinbutton { background-color: #0f141b; border: 1px solid #1f2733; border-radius: 8px; }

@keyframes rise { from { opacity: 0; transform: translateY(14px); } to { opacity: 1; } }
.rise { animation: rise 450ms cubic-bezier(.2,.8,.2,1) both; }
.d1 { animation-delay: 60ms; } .d2 { animation-delay: 120ms; } .d3 { animation-delay: 180ms; }
.d4 { animation-delay: 240ms; } .d5 { animation-delay: 300ms; }
""" + _profile_css()


def debounce(holder, key, ms, fn):
    if holder.get(key):
        GLib.source_remove(holder[key])

    def fire():
        holder[key] = None
        fn()
        return False
    holder[key] = GLib.timeout_add(ms, fire)


def label(text="", *classes, xalign=0.0, **kw):
    return Gtk.Label(label=text, xalign=xalign, css_classes=list(classes), **kw)


class Ring(Gtk.Widget):
    """Segmented 270° battery gauge with an animated value, colour and glow.

    Drawn with GSK paths (GPU), so it needs no cairo bindings.
    """

    SWEEP = 270.0
    START = 135.0
    SIZE = 240

    def __init__(self):
        super().__init__()
        self.value = 0.0          # shown (animated)
        self.target = 0.0
        self.limit = 100
        self.rgb = hex_rgb(PROFILES["balance"][3])
        self.target_rgb = self.rgb
        self.charging = False
        self.phase = 0.0
        self._tick = None
        self._last = None

    def do_measure(self, orientation, for_size):
        return self.SIZE, self.SIZE, -1, -1

    def set_state(self, value, limit, colour, charging):
        self.target = float(value)
        self.limit = limit
        self.target_rgb = hex_rgb(colour)
        self.charging = charging
        self._kick()

    def _kick(self):
        if self._tick is None:
            self._last = None
            self._tick = self.add_tick_callback(self._on_tick)

    def resume(self):
        self._kick()

    def _pulsing(self):
        root = self.get_root()
        return self.charging and root is not None and root.is_active() and root.get_mapped()

    def _on_tick(self, widget, clock):
        now = clock.get_frame_time() / 1e6
        dt = 0 if self._last is None else min(0.05, now - self._last)
        self._last = now
        k = 1 - math.exp(-dt * 5.5)
        self.value += (self.target - self.value) * k
        self.rgb = tuple(c + (t - c) * k for c, t in zip(self.rgb, self.target_rgb))
        pulsing = self._pulsing()
        self.phase = (self.phase + dt / 2.4) % 1.0 if pulsing else 0.0
        self.queue_draw()
        settled = (abs(self.target - self.value) < 0.05
                   and max(abs(a - b) for a, b in zip(self.rgb, self.target_rgb)) < 0.004)
        if settled and not pulsing:
            self.value, self.rgb = self.target, self.target_rgb
            self.queue_draw()
            self._tick = None
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _arc(self, cx, cy, r, frac):
        frac = max(0.0, min(1.0, frac))
        b = Gsk.PathBuilder.new()
        a0 = math.radians(self.START)
        sweep = math.radians(self.SWEEP * frac)
        n = max(2, int(120 * frac))
        for i in range(n + 1):
            a = a0 + sweep * i / n
            pt = Graphene.Point().init(cx + r * math.cos(a), cy + r * math.sin(a))
            (b.move_to if i == 0 else b.line_to)(pt.x, pt.y)
        return b.to_path()

    @staticmethod
    def _rgba(r, g, b, a):
        c = Gdk.RGBA()
        c.red, c.green, c.blue, c.alpha = r, g, b, a
        return c

    def _stroke(self, snap, path, width, rgba, dash=None, cap=Gsk.LineCap.BUTT):
        st = Gsk.Stroke.new(width)
        st.set_line_cap(cap)
        if dash:
            st.set_dash(dash)
        snap.append_stroke(path, st, rgba)

    def do_snapshot(self, snap):
        w, h = self.get_width(), self.get_height()
        cx, cy = w / 2, h / 2
        r = min(w, h) / 2 - 22
        lw = 14
        seg = 2 * math.pi * r * (self.SWEEP / 360) / 50
        dash = [seg * 0.72, seg * 0.28]
        R, G, B = self.rgb
        frac = self.value / 100

        self._stroke(snap, self._arc(cx, cy, r, 1.0), lw, self._rgba(1, 1, 1, 0.07), dash)

        if frac > 0.005:
            glow = 0.75 + 0.25 * math.sin(self.phase * 2 * math.pi) if self.phase else 0.75
            path = self._arc(cx, cy, r, frac)
            for i in range(6, 0, -1):
                self._stroke(snap, path, lw + i * 5,
                             self._rgba(R, G, B, 0.035 * glow * (7 - i) / 6),
                             cap=Gsk.LineCap.ROUND)
            self._stroke(snap, path, lw, self._rgba(R, G, B, 0.95), dash)

            a = math.radians(self.START + self.SWEEP * frac)
            dot = Gsk.PathBuilder.new()
            dot.add_circle(Graphene.Point().init(cx + r * math.cos(a), cy + r * math.sin(a)), 3.2)
            snap.append_fill(dot.to_path(), Gsk.FillRule.WINDING, self._rgba(1, 1, 1, 0.85))

        if self.limit < 100:
            a = math.radians(self.START + self.SWEEP * self.limit / 100)
            b = Gsk.PathBuilder.new()
            b.move_to(cx + (r - lw) * math.cos(a), cy + (r - lw) * math.sin(a))
            b.line_to(cx + (r + lw) * math.cos(a), cy + (r + lw) * math.sin(a))
            self._stroke(snap, b.to_path(), 3, self._rgba(1, 1, 1, 0.75), cap=Gsk.LineCap.ROUND)

        ring = Gsk.PathBuilder.new()
        ring.add_circle(Graphene.Point().init(cx, cy), r - lw - 8)
        self._stroke(snap, ring.to_path(), 1, self._rgba(1, 1, 1, 0.05))


class TimeBox(Gtk.Box):
    """HH : MM picker."""

    def __init__(self, on_change):
        super().__init__(spacing=4, valign=Gtk.Align.CENTER)
        self.h = Gtk.SpinButton.new_with_range(0, 23, 1)
        self.m = Gtk.SpinButton.new_with_range(0, 55, 5)
        for s in (self.h, self.m):
            s.set_wrap(True)
            s.set_numeric(True)
            s.connect("output", self._pad)
            s.connect("value-changed", lambda *_: on_change())
        self.append(self.h)
        self.append(label(":", xalign=0.5))
        self.append(self.m)

    @staticmethod
    def _pad(spin):
        spin.set_text(f"{int(spin.get_value()):02d}")
        return True

    def get(self):
        return f"{int(self.h.get_value()):02d}:{int(self.m.get_value()):02d}"

    def set(self, hhmm):
        h, m = (int(x) for x in hhmm.split(":"))
        self.h.set_value(h)
        self.m.set_value(m - m % 5)


def switch_row(title, sub):
    row = Gtk.Box(spacing=12)
    text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
    t = label(title, "row-title")
    s = label(sub, "row-sub", wrap=True)
    text.append(t)
    text.append(s)
    sw = Gtk.Switch(valign=Gtk.Align.CENTER)
    row.append(text)
    row.append(sw)
    row.sub = s
    return row, sw


def value_rows(rows):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=4)
    for title, widget in rows:
        row = Gtk.Box(spacing=12)
        row.append(label(title, "row-sub", hexpand=True, valign=Gtk.Align.CENTER, wrap=True))
        row.append(widget)
        box.append(row)
    return box


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Power Control", default_width=540,
                         default_height=900, css_classes=["dash", "p-balance"])
        self.app = app
        self.status = None
        self._updating = True  # widget defaults must not reach the daemon
        self._timers = {}
        self._accent_class = "p-balance"
        self.set_size_request(380, 480)

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        brand = Gtk.Box(spacing=6)
        brand.append(label("POWER", "brand", "accent-text"))
        brand.append(label("CONTROL", "brand-dim"))
        header.set_title_widget(brand)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Re-apply settings",
                             css_classes=["flat"])
        refresh.connect("clicked", lambda *_: self.call("Reapply"))
        header.pack_end(refresh)
        view.add_top_bar(header)

        self.scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=560, tightening_threshold=420)
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                            margin_start=16, margin_end=16, margin_bottom=24, margin_top=4)
        clamp.set_child(self.body)
        self.scroller.set_child(clamp)
        view.set_content(self.scroller)
        self.toasts.set_child(view)
        self.set_content(self.toasts)

        self.alert = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN,
                                  transition_duration=300)
        self.alert_lbl = label("", "alert", wrap=True)
        self.alert.set_child(self.alert_lbl)
        self.body.append(self.alert)

        self._build_hero()
        self._build_profiles()
        self._build_charging()
        self._build_schedule()
        self._build_thermal()
        self._set_sensitive(False)
        self._updating = False

        self.connect("notify::is-active", lambda *_: self.ring.resume())
        # The kernel doesn't send an event for every percent of charge, so poll
        # while the window is actually on screen (stops when hidden/minimised).
        self._live = None
        self.connect("map", lambda *_: self._live_start())
        self.connect("unmap", lambda *_: self._live_stop())
        if hasattr(Gtk.Window, "is_suspended"):
            self.connect("notify::suspended", lambda *_: self._live_tick())

    # ------------------------------------------------------------ build
    def _card(self, title, delay):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                      css_classes=["card", "rise", f"d{delay}"])
        if title:
            box.append(label(title, "eyebrow"))
        self.body.append(box)
        return box

    def _build_hero(self):
        card = self._card(None, 1)
        overlay = Gtk.Overlay(halign=Gtk.Align.CENTER)
        self.ring = Ring()
        overlay.set_child(self.ring)
        center = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, halign=Gtk.Align.CENTER,
                         valign=Gtk.Align.CENTER, spacing=2)
        num = Gtk.Box(halign=Gtk.Align.CENTER)
        self.pct = label("--", "pct", xalign=0.5)
        num.append(self.pct)
        num.append(label("%", "pct-unit", valign=Gtk.Align.START, margin_top=12))
        self.hero_state = label("", "hero-state", "accent-text", xalign=0.5)
        self.hero_sub = label("", "hero-sub", xalign=0.5, justify=Gtk.Justification.CENTER,
                              wrap=True, max_width_chars=18)
        center.append(num)
        center.append(self.hero_state)
        center.append(self.hero_sub)
        overlay.add_overlay(center)
        card.append(overlay)

        chips = Gtk.Box(spacing=8, homogeneous=True)
        self.chip = {}
        for key, title in (("health", "HEALTH"), ("cycles", "CYCLES"), ("temp", "CPU TEMP"),
                           ("power", "POWER")):
            c = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["chip"], spacing=2)
            c.append(label(title, "chip-label"))
            v = label("—", "chip-value")
            c.append(v)
            chips.append(c)
            self.chip[key] = v
        card.append(chips)

    def _build_profiles(self):
        self.profile_card = self._card("PERFORMANCE MODE", 2)
        self.profile_box = Gtk.Box(spacing=8, homogeneous=True)
        self.profile_card.append(self.profile_box)
        self.profile_btns = {}
        self._profile_choices = None

    def _rebuild_profiles(self, choices):
        if choices == self._profile_choices:
            return
        self._profile_choices = choices
        while (child := self.profile_box.get_first_child()):
            self.profile_box.remove(child)
        self.profile_btns = {}
        first = None
        for name in choices:
            title, icon, blurb, _ = PROFILES.get(
                name, (name.title(), "preferences-system-symbolic", "", "#e9eef6"))
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            inner.append(Gtk.Image(icon_name=icon, pixel_size=30))
            inner.append(label(title, "pc-name", xalign=0.5))
            inner.append(label(blurb, "pc-blurb", xalign=0.5, wrap=True,
                               justify=Gtk.Justification.CENTER))
            inner.append(Gtk.Box(css_classes=["pc-led"], halign=Gtk.Align.CENTER, margin_top=4))
            b = Gtk.ToggleButton(child=inner, css_classes=["profile-card", f"pc-{name}"])
            if first:
                b.set_group(first)
            first = first or b
            b.connect("toggled", self._on_profile, name)
            self.profile_box.append(b)
            self.profile_btns[name] = b

    def _build_charging(self):
        card = self._card("CHARGING", 3)
        self.cons_row, self.cons = switch_row("Conservation mode", "Stop charging at about 80%")
        self.cons.connect("notify::active", self._on_cons)
        card.append(self.cons_row)
        self.rapid_row, self.rapid = switch_row("Rapid charge", "Charge faster, more heat")
        self.rapid.connect("notify::active", self._on_rapid)
        card.append(self.rapid_row)

        card.append(Gtk.Box(css_classes=["sep"]))
        self.limit_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        top = Gtk.Box()
        top.append(label("Charge limit", "row-title", hexpand=True))
        self.limit_lbl = label("", "limit-value", "accent-text", xalign=1.0)
        top.append(self.limit_lbl)
        self.limit = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 80, 100, 1)
        self.limit.set_draw_value(False)
        self.limit.connect("value-changed", self._on_limit)
        self.limit_sub = label("", "row-sub")
        self.limit_box.append(top)
        self.limit_box.append(self.limit)
        self.limit_box.append(self.limit_sub)
        card.append(self.limit_box)

        self.full_row = Gtk.Box(spacing=12, margin_top=6)
        t = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        t.append(label("Need 100% today?", "row-title"))
        t.append(label("Charge full once, then back to the limit", "row-sub", wrap=True))
        self.full_btn = Gtk.Button(label="Charge now", valign=Gtk.Align.CENTER,
                                   css_classes=["cta"])
        self.full_btn.connect("clicked", lambda *_: self.call("ChargeFullOnce"))
        self.full_row.append(t)
        self.full_row.append(self.full_btn)
        card.append(self.full_row)

    def _build_schedule(self):
        card = self._card("SCHEDULED FULL CHARGE", 4)
        row, self.sched = switch_row("Charge to 100% every day",
                                     "Stay at the limit, except between these times")
        self.sched.connect("notify::active", self._on_sched)
        card.append(row)
        self.start = TimeBox(self._on_sched)
        self.end = TimeBox(self._on_sched)
        self.sched_times = value_rows((("From", self.start), ("Until", self.end)))
        self.start.set("05:00")
        self.end.set("07:00")
        card.append(self.sched_times)

    def _build_thermal(self):
        card = self._card("HEAT GUARD", 5)
        row, self.thermal = switch_row("Charge gently when hot",
                                       "Turns rapid charge off while the laptop works hard")
        self.thermal.connect("notify::active", self._on_thermal)
        card.append(row)
        self.high = Gtk.SpinButton.new_with_range(30, 100, 1)
        self.low = Gtk.SpinButton.new_with_range(25, 95, 1)
        for sp in (self.high, self.low):
            sp.set_valign(Gtk.Align.CENTER)
            sp.connect("value-changed", self._on_thermal)
        self.thermal_box = value_rows((("Gentle charging above (°C)", self.high),
                                       ("Back to normal below (°C)", self.low)))
        card.append(self.thermal_box)
        self.thermal_note = label("", "note", wrap=True, margin_top=4)
        card.append(self.thermal_note)

    LIVE_INTERVAL = 3

    def _live_start(self):
        if self._live is None:
            self._live = GLib.timeout_add_seconds(self.LIVE_INTERVAL, self._live_tick)

    def _live_stop(self):
        if self._live is not None:
            GLib.source_remove(self._live)
            self._live = None

    def _live_tick(self):
        if self.get_mapped() and not (hasattr(self, "is_suspended") and self.is_suspended()):
            self.app.refresh()
        return GLib.SOURCE_CONTINUE

    # ---------------------------------------------------------- handlers
    def _on_cons(self, sw, _):
        if not self._updating:
            self.call("SetConservation", sw.get_active())

    def _on_rapid(self, sw, _):
        if not self._updating:
            self.call("SetRapidCharge", sw.get_active())

    def _on_limit(self, scale):
        v = int(scale.get_value())
        self.limit_lbl.set_label("OFF" if v >= 100 else f"{v}%")
        if not self._updating:
            debounce(self._timers, "limit", 700, lambda: self.call("SetChargeLimit", dbus.UInt32(v)))

    def _on_profile(self, btn, name):
        if btn.get_active():
            self._set_accent(name)
            if not self._updating:
                self.call("SetProfile", name)

    def _on_sched(self, *_):
        on = self.sched.get_active()
        self.sched_times.set_sensitive(on)
        if self._updating:
            return
        if not on:
            self.call("SetSchedule", "", "")
        elif self.start.get() == self.end.get():
            self.toast("Start and end time must be different")
        else:
            debounce(self._timers, "sched", 900,
                     lambda: self.call("SetSchedule", self.start.get(), self.end.get()))

    def _on_thermal(self, *_):
        on = self.thermal.get_active()
        self.thermal_box.set_sensitive(on)
        if self._updating:
            return
        hi, lo = self.high.get_value(), self.low.get_value()
        if on and lo >= hi:
            self.toast("The resume temperature must be lower than the trip temperature")
            return
        debounce(self._timers, "thermal", 900,
                 lambda: self.call("SetThermalGuard", on, float(hi), float(lo)))

    # ------------------------------------------------------------- D-Bus
    def call(self, method, *args):
        try:
            proxy = self.app.iface()
        except dbus.DBusException as e:
            self.toast(str(e.get_dbus_message() or e))
            return

        def err(e):
            msg = e.get_dbus_message() if hasattr(e, "get_dbus_message") else str(e)
            self.toast(msg or str(e))
            self.app.refresh()

        getattr(proxy, method)(*args, reply_handler=lambda *a: self.app.refresh(),
                               error_handler=err, timeout=300)

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=5))

    # ------------------------------------------------------------ update
    def _set_accent(self, name):
        cls = f"p-{name}" if name in PROFILES else "p-balance"
        if cls != self._accent_class:
            self.remove_css_class(self._accent_class)
            self.add_css_class(cls)
            self._accent_class = cls

    def _set_sensitive(self, on):
        for w in (self.cons_row, self.rapid_row, self.limit_box, self.full_row,
                  self.profile_card, self.sched, self.thermal):
            w.set_sensitive(on)

    def _show_alert(self, msg, down=False):
        self.alert_lbl.set_label(msg or "")
        (self.alert_lbl.add_css_class if down else self.alert_lbl.remove_css_class)("down")
        self.alert.set_reveal_child(bool(msg))

    def update(self, st):
        self.status = st
        self._updating = True
        try:
            self._update(st)
        finally:
            self._updating = False

    def _update(self, st):
        if not st:
            self._set_sensitive(False)
            self._show_alert("The lpowerd service is not running. "
                             "Run: sudo systemctl start lpowerd", down=True)
            self.pct.set_label("--")
            self.hero_state.set_label("OFFLINE")
            self.hero_sub.set_label("")
            self.ring.set_state(0, 100, "#3a4456", False)
            return
        self._set_sensitive(True)
        b, ch, th, pr, hw = st["battery"], st["charge"], st["thermal"], st["profile"], st["hardware"]

        cap = b.get("capacity") or 0
        status = b.get("status") or ""
        charging = status == "Charging"
        self.pct.set_label(str(cap))
        if charging:
            state = "CHARGING"
        elif st["on_ac"] and cap >= ch["target"] - 1 and ch["target"] < 100:
            state = "HOLDING"
        elif st["on_ac"]:
            state = "PLUGGED IN" if status != "Full" else "FULL"
        else:
            state = "ON BATTERY"
        self.hero_state.set_label(state)
        if st["on_ac"] and ch["target"] < 100:
            self.hero_sub.set_label(f"limit {ch['target']}%")
        elif ch["target"] == 100 and ch["limit"] < 100:
            self.hero_sub.set_label("full charge now")
        elif b.get("power_w") and not st["on_ac"]:
            self.hero_sub.set_label(f"{b['power_w']:.1f} W draw")
        else:
            self.hero_sub.set_label("")

        active = pr["active"]
        colour = PROFILES.get(active, PROFILES["balance"])[3]
        if th["tripped"]:
            colour = HOT
        elif not st["on_ac"] and cap <= 15:
            colour = WARN
        self.ring.set_state(cap, ch["target"] if st["on_ac"] else ch["limit"], colour, charging)
        self._set_accent(active)

        self.chip["health"].set_label(f"{b['health']:.0f}%" if b.get("health") else "—")
        self.chip["cycles"].set_label(str(b["cycle_count"]) if b.get("cycle_count") else "—")
        t = th["temperature"]
        self.chip["temp"].set_label(f"{t:.0f}°" if t is not None else "—")
        self.chip["power"].set_label(f"{b['power_w']:.0f} W" if b.get("power_w") else "—")
        if th["source"] == "battery":
            self.chip["temp"].get_parent().get_first_child().set_label("BATT TEMP")

        floor = ch["conservation_level"]
        self.cons_row.set_visible(hw["conservation"])
        self.cons_row.sub.set_label(f"Stop charging at about {floor}%")
        self.cons.set_active(ch["conservation"] and ch["limit"] == floor)
        self.rapid_row.set_visible(hw["rapid_charge"])
        self.rapid.set_active(ch["rapid_charge"])
        lo = 20 if (hw["inhibit_charge"] or hw["end_threshold"]) else floor
        self.limit.set_range(lo, 100)
        self.limit.clear_marks()
        self.limit.add_mark(floor, Gtk.PositionType.BOTTOM, None)
        self.limit.set_value(ch["limit"])
        self.limit_lbl.set_label("OFF" if ch["limit"] >= 100 else f"{ch['limit']}%")
        self.limit_sub.set_label("Stops charging at this level" if ch["limit"] < 100
                                 else "No limit, charges to 100%")
        self.full_row.set_visible(ch["limit"] < 100)
        self.full_btn.set_sensitive(st["on_ac"] and not ch["full_once"])
        self.full_btn.set_label("Charging…" if ch["full_once"] else
                                ("Charge now" if st["on_ac"] else "Plug in first"))

        self.profile_card.set_visible(bool(pr["choices"]))
        self._rebuild_profiles(pr["choices"])
        if active in self.profile_btns:
            self.profile_btns[active].set_active(True)

        s = ch["schedule"]
        self.sched.set_active(bool(s))
        if s:
            self.start.set(s["start"])
            self.end.set(s["end"])
        self.sched_times.set_sensitive(bool(s))

        self.thermal.set_active(th["enabled"])
        self.thermal.set_sensitive(t is not None)
        if th["high"] is not None:
            self.high.set_value(th["high"])
            self.low.set_value(th["low"])
        self.thermal_box.set_sensitive(th["enabled"])
        note = ""
        if th["source"] and th["source"] != "battery":
            note = ("No battery temperature sensor on this laptop, so CPU temperature is used. "
                    "The battery's own overheat protection always stays active. ")
        note += (f"When hot, rapid charge turns off and charging pauses above {floor}%. "
                 "It never makes charging faster or hotter.")
        self.thermal_note.set_label(note)

        msg = None
        if th["tripped"]:
            msg = f"Heat guard active ({t:.0f}°C): charging gently"
        elif st.get("error"):
            msg = st["error"]
        elif st["warnings"]:
            msg = st["warnings"][0]
        self._show_alert(msg)


class App(Adw.Application):
    def __init__(self, **kw):
        super().__init__(application_id=kw.pop("application_id", APP_ID), **kw)
        self.win = None
        self.bus = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        # The dashboard is designed dark, like the hardware it controls.
        self.get_style_manager().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        css = Gtk.CssProvider()
        css.load_from_string(CSS) if hasattr(css, "load_from_string") else \
            css.load_from_data(CSS.encode(), -1)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.bus = (dbus.SessionBus() if os.environ.get("LPOWERD_BUS") == "session"
                    else dbus.SystemBus())
        self.bus.add_signal_receiver(self._on_status, "StatusChanged",
                                     INTERFACE, BUS_NAME, OBJECT_PATH)
        self.bus.watch_name_owner(BUS_NAME, lambda owner: self.refresh() if owner
                                  else self.win and self.win.update(None))

    def do_activate(self):
        if not self.win:
            self.win = Window(self)
            self.refresh()
            shot = os.environ.get("LPOWERD_GUI_SHOT")
            if shot:
                GLib.timeout_add(2000, self._screenshot, shot)
        self.win.present()

    def iface(self):
        return dbus.Interface(self.bus.get_object(BUS_NAME, OBJECT_PATH, introspect=False),
                              INTERFACE)

    def refresh(self):
        try:
            self.iface().GetStatus(reply_handler=self._on_status,
                                   error_handler=lambda e: self.win and self.win.update(None))
        except dbus.DBusException:
            if self.win:
                self.win.update(None)

    def _on_status(self, s):
        if self.win:
            self.win.update(json.loads(str(s)))

    def _screenshot(self, path):
        # Development aid: render the window to a PNG and quit.
        w = self.win
        paintable = Gtk.WidgetPaintable.new(w)
        snap = Gtk.Snapshot()
        paintable.snapshot(snap, w.get_width(), w.get_height())
        node = snap.to_node()
        if node:
            w.get_native().get_renderer().render_texture(node, None).save_to_png(path)
        self.quit()
        return False


def main():
    DBusGMainLoop(set_as_default=True)
    return App().run(sys.argv[:1])
