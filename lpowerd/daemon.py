"""lpowerd system daemon: D-Bus service + Polkit + event loop.

Idle cost is zero wakeups: the loop blocks on the uevent socket, the
logind/PPD signals and the platform_profile sysfs notifier. Timers exist
only while a policy needs them (software limit or thermal guard on AC,
or the next schedule boundary).
"""

import argparse
import json
import logging
import os
import signal
import sys
from datetime import datetime

import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

from . import (BUS_NAME, INTERFACE, OBJECT_PATH, POLKIT_CHARGING, POLKIT_CONFIGURE,
               POLKIT_PROFILE, __version__, policy, uevent)
from .hardware import Hardware, friendly_profile, friendly_profiles, resolve_profile
from .state import Store

log = logging.getLogger("lpowerd")

PPD_NAME = "net.hadess.PowerProfiles"
PPD_PATH = "/net/hadess/PowerProfiles"
ADVANCED_PROFILES = ("max-power", "custom")
PPD_MAP = {
    "low-power": "power-saver", "quiet": "power-saver", "cool": "power-saver",
    "balanced": "balanced",
    "performance": "performance", "max-power": "performance", "balanced-performance": "performance",
}


class Failed(dbus.DBusException):
    _dbus_error_name = INTERFACE + ".Error.Failed"


class AccessDenied(dbus.DBusException):
    _dbus_error_name = INTERFACE + ".Error.AccessDenied"


class Daemon(dbus.service.Object):
    def __init__(self, bus, hw, store, *, use_polkit, use_ppd):
        self.bus = bus
        self.hw = hw
        self.store = store
        self.use_polkit = use_polkit
        self.rt = policy.Runtime()
        self.decision = policy.Decision()
        self.last_error = None
        self._last_status = None
        self._src = {"eval": None, "poll": None, "boundary": None, "profile": None}
        self._eval_profile = False
        self._ppd = None
        name = dbus.service.BusName(BUS_NAME, bus, do_not_queue=True)
        super().__init__(name, OBJECT_PATH)

        if use_ppd:
            self._setup_ppd()
        self._setup_logind()
        self._setup_uevent()
        self._watch_profile()

    # ------------------------------------------------------------ setup
    def _setup_ppd(self):
        try:
            self._ppd = self.bus.get_object(PPD_NAME, PPD_PATH, introspect=False)
            self.bus.add_signal_receiver(
                self._on_ppd_changed, "PropertiesChanged",
                "org.freedesktop.DBus.Properties", PPD_NAME, PPD_PATH)
            log.info("power-profiles provider found; profile changes go through it first")
        except dbus.DBusException:
            self._ppd = None

    def _setup_logind(self):
        try:
            self.bus.add_signal_receiver(
                self._on_sleep, "PrepareForSleep", "org.freedesktop.login1.Manager",
                "org.freedesktop.login1", "/org/freedesktop/login1")
        except dbus.DBusException as e:
            log.warning("logind not available: %s", e)

    def _setup_uevent(self):
        try:
            self._sock = uevent.open_socket()
        except OSError as e:
            log.warning("uevents unavailable (%s); falling back to resume/poll only", e)
            return
        GLib.io_add_watch(self._sock.fileno(), GLib.PRIORITY_DEFAULT, GLib.IO_IN, self._on_uevent)

    def _watch_profile(self):
        # platform_profile calls sysfs_notify() on hotkey (Fn+Q) changes;
        # sysfs signals that with POLLPRI.
        if self._src["profile"]:
            GLib.source_remove(self._src["profile"])
            self._src["profile"] = None
        path = self.hw.caps.profile
        if not path:
            return
        try:
            self._profile_fd = os.open(path, os.O_RDONLY)
            os.read(self._profile_fd, 64)
        except OSError:
            return
        self._src["profile"] = GLib.io_add_watch(
            self._profile_fd, GLib.PRIORITY_DEFAULT, GLib.IO_PRI | GLib.IO_ERR,
            self._on_profile_notify)

    # ----------------------------------------------------------- events
    def _on_uevent(self, fd, cond):
        for ev in uevent.drain(self._sock):
            if ev.get("SUBSYSTEM") == "power_supply":
                self.request_eval("uevent:" + ev.get("POWER_SUPPLY_NAME", "?"))
        return True

    def _on_sleep(self, going_down):
        if going_down:
            log.info("system going to sleep")
            return
        log.info("resumed; re-applying settings")
        self.hw.detect()
        self._watch_profile()
        # The EC can take a moment to accept writes after resume.
        self.request_eval("resume", 1500, profile=True)
        GLib.timeout_add(8000, lambda: self.request_eval("resume-verify", 0, profile=True) and False)

    def _on_profile_notify(self, fd, cond):
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            raw = os.read(fd, 64).decode().strip()
        except OSError:
            return True
        if raw and self.store["profile"] not in (None, raw):
            log.info("profile changed externally to %s", raw)
            self.store.update(profile=raw)
        self._emit_status()
        return True

    def _on_ppd_changed(self, iface, changed, invalidated):
        new = changed.get("ActiveProfile")
        cur = self.store["profile"]
        if new is None or cur is None or PPD_MAP.get(cur) == str(new):
            return
        # Let the provider finish writing sysfs, then adopt what it chose.
        def adopt():
            raw = self.hw.profile()
            if raw and raw != self.store["profile"]:
                log.info("profile changed via power-profiles to %s", raw)
                self.store.update(profile=raw)
                self._emit_status()
            return False
        GLib.timeout_add(1000, adopt)

    # --------------------------------------------------------- evaluate
    def request_eval(self, why, delay_ms=400, profile=False):
        self._eval_profile |= profile
        if self._src["eval"]:
            GLib.source_remove(self._src["eval"])
        self._src["eval"] = GLib.timeout_add(delay_ms, self._eval_cb, why)
        return True

    def _eval_cb(self, why):
        self._src["eval"] = None
        self.evaluate(why)
        return False

    def evaluate(self, why, profile=False):
        profile |= self._eval_profile
        self._eval_profile = False
        hw = self.hw
        info = hw.battery_info()
        on_ac = hw.on_ac()
        temp = hw.temperature()
        d, self.rt = policy.decide(
            self.store.data, hw.caps, self.rt, now=datetime.now().astimezone(),
            capacity=info.get("capacity"), on_ac=on_ac, temp=temp)
        self.decision = d
        log.debug("evaluate(%s): %s", why, d)
        errors = []
        try:
            if d.end_threshold is not None and hw.end_threshold() != d.end_threshold:
                hw.set_end_threshold(d.end_threshold)
            if d.mode and hw.charge_mode() != d.mode:
                hw.set_charge_mode(d.mode)
                if hw.charge_mode() != d.mode:
                    errors.append(f"firmware did not accept charge mode {d.mode}")
            if d.inhibit is not None and hw.inhibited() != d.inhibit:
                hw.set_inhibit(d.inhibit)
        except OSError as e:
            errors.append(str(e))
        if self.store["profile"] and self.store["profile"] not in self._profile_choices():
            log.info("stored profile %s is hidden; switching to balanced", self.store["profile"])
            self.store.update(profile="balanced" if "balanced" in self._profile_choices() else None)
            profile = True
        if profile and self.store["profile"]:
            try:
                self._apply_profile(self.store["profile"])
            except (OSError, ValueError) as e:
                errors.append(str(e))
        for e in errors:
            log.error("%s", e)
        self.last_error = "; ".join(errors) or None
        self._arm_timers(d)
        self._emit_status()

    def _arm_timers(self, d):
        if d.poll and not self._src["poll"]:
            iv = max(10, int(self.store["poll_interval"]))
            self._src["poll"] = GLib.timeout_add_seconds(iv, self._on_poll)
        elif not d.poll and self._src["poll"]:
            GLib.source_remove(self._src["poll"])
            self._src["poll"] = None
        if self._src["boundary"]:
            GLib.source_remove(self._src["boundary"])
            self._src["boundary"] = None
        if d.next_boundary:
            secs = (d.next_boundary - datetime.now().astimezone()).total_seconds()
            self._src["boundary"] = GLib.timeout_add_seconds(
                max(1, int(secs) + 1), self._on_boundary)

    def _on_poll(self):
        self.evaluate("poll")
        return self._src["poll"] is not None

    def _on_boundary(self):
        self._src["boundary"] = None
        self.evaluate("schedule")
        return False

    def _profile_choices(self):
        choices = self.hw.caps.profile_choices
        if self.store["advanced_profiles"]:
            return choices
        return [p for p in choices if p not in ADVANCED_PROFILES]

    def _apply_profile(self, raw):
        hw = self.hw
        if self._ppd is not None and PPD_MAP.get(raw):
            try:
                self._ppd.Set(PPD_NAME, "ActiveProfile", dbus.String(PPD_MAP[raw], variant_level=1),
                              dbus_interface="org.freedesktop.DBus.Properties", timeout=5)
            except dbus.DBusException as e:
                log.warning("power-profiles provider refused %s: %s", raw, e)
        if hw.profile() != raw:
            hw.set_profile(raw)

    # ----------------------------------------------------------- status
    def status(self):
        hw, c, d, s = self.hw, self.hw.caps, self.decision, self.store.data
        temp = hw.temperature()
        hi = lo = None
        if c.temp:
            hi, lo = policy.thermal_points(s, c.temp_kind)
        raw = hw.profile()
        return {
            "version": __version__,
            "battery": hw.battery_info(),
            "on_ac": hw.on_ac(),
            "charge": {
                "mode": hw.charge_mode(),
                "limit": s["charge_limit"],
                "target": d.target,
                "conservation": hw.charge_mode() == "conservation",
                "conservation_level": s["conservation_level"],
                "rapid_charge": s["rapid_charge"],
                "inhibited": hw.inhibited(),
                "end_threshold": hw.end_threshold(),
                "schedule": s["schedule"],
                "full_once": self.rt.full_once,
            },
            "thermal": {
                "enabled": s["thermal"]["enabled"],
                "temperature": temp,
                "source": c.temp_kind,
                "high": hi, "low": lo,
                "tripped": self.rt.thermal_tripped,
            },
            "profile": {
                "active": friendly_profile(raw) if raw else None,
                "raw": raw,
                "choices": friendly_profiles(self._profile_choices()),
                "driver": c.profile_driver,
                "managed": s["profile"] is not None,
            },
            "hardware": {
                "ideapad": c.ideapad,
                "battery": c.battery and os.path.basename(c.battery),
                "charge_api": "charge_types" if c.charge_types else
                              ("conservation_mode" if c.conservation else None),
                "conservation": c.has_conservation,
                "rapid_charge": c.has_rapid,
                "inhibit_charge": c.has_inhibit,
                "end_threshold": c.has_end_threshold,
                "fan_mode": hw_read_int(c.fan_mode),
                "fn_lock": hw_read_int(c.fn_lock),
            },
            "polling": self._src["poll"] is not None,
            "reasons": d.reasons,
            "warnings": d.warnings,
            "error": self.last_error,
        }

    def _emit_status(self):
        st = self.status()
        cmp = json.dumps({k: v for k, v in st.items() if k != "battery"}, sort_keys=True)
        cmp += str(st["battery"].get("capacity")) + str(st["battery"].get("status"))
        if cmp != self._last_status:
            self._last_status = cmp
            self.StatusChanged(json.dumps(st))

    # ------------------------------------------------------------ auth
    def _authorized(self, sender, action, ok, err, fn):
        def run():
            try:
                res = fn()
            except (OSError, ValueError) as e:
                err(Failed(str(e)))
                return
            ok() if res is None else ok(res)

        if not self.use_polkit or self.bus.get_unix_user(sender) == 0:
            return run()
        authority = self.bus.get_object("org.freedesktop.PolicyKit1",
                                        "/org/freedesktop/PolicyKit1/Authority")
        subject = dbus.Struct(("system-bus-name", {"name": dbus.String(sender, variant_level=1)}),
                              signature="sa{sv}")

        def reply(result):
            if result[0]:
                run()
            else:
                err(AccessDenied(f"not authorized for {action}"))

        authority.CheckAuthorization(
            subject, action, dbus.Dictionary({}, signature="ss"), dbus.UInt32(1), "",
            dbus_interface="org.freedesktop.PolicyKit1.Authority", timeout=300,
            reply_handler=reply, error_handler=lambda e: err(AccessDenied(str(e))))

    # ------------------------------------------------------- D-Bus API
    @dbus.service.signal(INTERFACE, signature="s")
    def StatusChanged(self, status_json):
        pass

    @dbus.service.method(INTERFACE, out_signature="s")
    def GetStatus(self):
        return json.dumps(self.status())

    @dbus.service.method(INTERFACE)
    def Reapply(self):
        self.hw.detect()
        self._watch_profile()
        self.evaluate("reapply", profile=True)

    def _charging(self, sender, ok, err, fn):
        def wrapped():
            fn()
            self.evaluate("set")
        self._authorized(sender, POLKIT_CHARGING, ok, err, wrapped)

    @dbus.service.method(INTERFACE, in_signature="u", sender_keyword="sender",
                         async_callbacks=("ok", "err"))
    def SetChargeLimit(self, limit, sender=None, ok=None, err=None):
        def fn():
            limit_ = int(limit)
            c, floor = self.hw.caps, self.store["conservation_level"]
            if not 20 <= limit_ <= 100:
                raise ValueError("limit must be between 20 and 100")
            if limit_ < floor and not (c.has_inhibit or c.has_end_threshold):
                raise ValueError(f"this firmware cannot stop charging below {floor}% "
                                 f"(only conservation mode is available); use {floor}-100")
            if limit_ < 100 and not (c.has_conservation or c.has_inhibit or c.has_end_threshold):
                raise ValueError("no charge control interface on this machine")
            self.store.update(charge_limit=limit_)
        self._charging(sender, ok, err, fn)

    @dbus.service.method(INTERFACE, in_signature="b", sender_keyword="sender",
                         async_callbacks=("ok", "err"))
    def SetConservation(self, on, sender=None, ok=None, err=None):
        def fn():
            if not self.hw.caps.has_conservation:
                raise ValueError("conservation mode is not supported here")
            if on:
                self.store.update(charge_limit=self.store["conservation_level"], rapid_charge=False)
            else:
                self.store.update(charge_limit=100)
        self._charging(sender, ok, err, fn)

    @dbus.service.method(INTERFACE, in_signature="b", sender_keyword="sender",
                         async_callbacks=("ok", "err"))
    def SetRapidCharge(self, on, sender=None, ok=None, err=None):
        def fn():
            if on and not self.hw.caps.has_rapid:
                raise ValueError("rapid charge is not supported here")
            upd = {"rapid_charge": bool(on)}
            # Same as Vantage: rapid charge and conservation are exclusive.
            if on and self.store["charge_limit"] <= self.store["conservation_level"]:
                upd["charge_limit"] = 100
            self.store.update(**upd)
        self._charging(sender, ok, err, fn)

    @dbus.service.method(INTERFACE, sender_keyword="sender", async_callbacks=("ok", "err"))
    def ChargeFullOnce(self, sender=None, ok=None, err=None):
        def fn():
            self.rt.full_once = True
        self._charging(sender, ok, err, fn)

    @dbus.service.method(INTERFACE, in_signature="s", sender_keyword="sender",
                         async_callbacks=("ok", "err"))
    def SetProfile(self, name, sender=None, ok=None, err=None):
        def fn():
            raw = resolve_profile(str(name), self._profile_choices())
            self._apply_profile(raw)
            self.store.update(profile=raw)
            self._emit_status()
        self._authorized(sender, POLKIT_PROFILE, ok, err, fn)

    @dbus.service.method(INTERFACE, in_signature="ss", sender_keyword="sender",
                         async_callbacks=("ok", "err"))
    def SetSchedule(self, start, end, sender=None, ok=None, err=None):
        def fn():
            if not start and not end:
                self.store.update(schedule=None)
            else:
                try:
                    a, b = policy.parse_hhmm(start), policy.parse_hhmm(end)
                except ValueError:
                    raise ValueError("times must be HH:MM") from None
                if a == b:
                    raise ValueError("start and end must differ")
                self.store.update(schedule={"start": a.strftime("%H:%M"), "end": b.strftime("%H:%M")})
            self.evaluate("schedule-set")
        self._authorized(sender, POLKIT_CONFIGURE, ok, err, fn)

    @dbus.service.method(INTERFACE, in_signature="bdd", sender_keyword="sender",
                         async_callbacks=("ok", "err"))
    def SetThermalGuard(self, enabled, high, low, sender=None, ok=None, err=None):
        def fn():
            th = dict(self.store["thermal"], enabled=bool(enabled))
            th["high"] = float(high) if high > 0 else None
            th["low"] = float(low) if low > 0 else None
            if th["high"] and th["low"] and th["low"] >= th["high"]:
                raise ValueError("low must be below high")
            if enabled and not self.hw.caps.temp:
                raise ValueError("no temperature sensor found")
            self.store.update(thermal=th)
            self.evaluate("thermal-set")
        self._authorized(sender, POLKIT_CONFIGURE, ok, err, fn)


def hw_read_int(path):
    from .hardware import read_int
    return read_int(path) if path else None


def main(argv=None):
    ap = argparse.ArgumentParser(prog="lpowerd", description="Lenovo power management daemon")
    ap.add_argument("--session", action="store_true",
                    help="development: use the session bus and skip Polkit")
    ap.add_argument("--sysfs-root", default="/", help="development: fake sysfs root")
    ap.add_argument("--state", default=os.path.join(
        os.environ.get("STATE_DIRECTORY", "/var/lib/lpowerd"), "state.json"))
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus() if args.session else dbus.SystemBus()
    first_run = not os.path.exists(args.state)
    store = Store(args.state)
    hw = Hardware(args.sysfs_root, store["thermal"]["sensor"])
    if first_run:
        # Adopt whatever the firmware is doing now instead of resetting it.
        mode = hw.charge_mode()
        store.update(charge_limit=store["conservation_level"] if mode == "conservation" else 100,
                     rapid_charge=mode == "rapid")
        log.info("first run: adopted current charge mode %s", mode)
    try:
        d = Daemon(bus, hw, store, use_polkit=not args.session,
                   use_ppd=not args.session and args.sysfs_root == "/")
    except dbus.exceptions.NameExistsException:
        log.error("%s is already running", BUS_NAME)
        return 1
    d.evaluate("startup", profile=True)

    loop = GLib.MainLoop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, loop.quit)
    log.info("lpowerd %s ready", __version__)
    loop.run()
    return 0
