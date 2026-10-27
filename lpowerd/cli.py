"""lpctl - command line client for lpowerd."""

import argparse
import json
import os
import sys

from . import BUS_NAME, INTERFACE, OBJECT_PATH, __version__

USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def c(text, code):
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else str(text)


def bold(t): return c(t, "1")
def dim(t): return c(t, "2")
def green(t): return c(t, "32")
def yellow(t): return c(t, "33")
def red(t): return c(t, "31")


def proxy():
    import dbus
    bus = dbus.SessionBus() if os.environ.get("LPOWERD_BUS") == "session" else dbus.SystemBus()
    try:
        obj = bus.get_object(BUS_NAME, OBJECT_PATH)
    except dbus.DBusException as e:
        sys.exit(red(f"lpowerd is not running ({e.get_dbus_name()}). "
                     "Try: systemctl status lpowerd"))
    return dbus.Interface(obj, INTERFACE)


def call(method, *args):
    import dbus
    try:
        # Long timeout: Polkit may be showing an authentication dialog.
        return getattr(proxy(), method)(*args, timeout=300)
    except dbus.DBusException as e:
        msg = e.get_dbus_message() or e.get_dbus_name()
        sys.exit(red(f"error: {msg}"))


def onoff(v):
    v = v.lower()
    if v in ("on", "1", "true", "yes", "enable"):
        return True
    if v in ("off", "0", "false", "no", "disable"):
        return False
    raise argparse.ArgumentTypeError("expected on/off")


def flag(v, true="on", false="off"):
    if v is None:
        return dim("n/a")
    return green(true) if v else dim(false)


def bar(pct, width=24):
    if pct is None:
        return ""
    n = round(width * pct / 100)
    col = "32" if pct > 40 else ("33" if pct > 15 else "31")
    return c("█" * n, col) + dim("░" * (width - n))


def print_status(st):
    b, ch, th, pr, hw = st["battery"], st["charge"], st["thermal"], st["profile"], st["hardware"]
    row = lambda k, v: print(f"  {dim(k.ljust(16))}{v}")
    print(bold("Battery"))
    cap = b.get("capacity")
    row("Charge", f"{bar(cap)} {bold(cap)}%  {b.get('status', '')}"
        + (f"  {b['power_w']} W" if b.get("power_w") else ""))
    row("AC adapter", flag(st["on_ac"], "connected", "unplugged"))
    if b.get("health") is not None:
        row("Health", f"{b['health']}% of design" + (f", {b['cycle_count']} cycles" if b.get('cycle_count') else ""))
    print(bold("Charging"))
    if ch["conservation"] and ch["limit"] == ch["conservation_level"]:
        lim = green(f"conservation ({ch['conservation_level']}%)")
    elif ch["limit"] < 100:
        lim = yellow(f"{ch['limit']}%")
    else:
        lim = "100% (no limit)"
    row("Limit", lim)
    if ch["target"] != ch["limit"]:
        row("Right now", yellow(f"charging to {ch['target']}%"))
    row("Firmware mode", ch["mode"] or dim("unsupported"))
    row("Rapid charge", flag(ch["rapid_charge"]) if hw["rapid_charge"] else dim("unsupported"))
    s = ch["schedule"]
    row("Full-charge at", f"{s['start']}-{s['end']}" if s else dim("off"))
    if ch["full_once"]:
        row("One-shot", yellow("charging to 100% once"))
    print(bold("Thermal guard"))
    t = th["temperature"]
    tt = f"{t:.0f}°C" if t is not None else dim("no sensor")
    if th["source"] and th["source"] != "battery":
        tt += dim(f" (CPU sensor {th['source'].split(':', 1)[1]}; no battery sensor)")
    row("Temperature", tt)
    if th["enabled"]:
        row("Guard", (red("TRIPPED") if th["tripped"] else green("armed"))
            + dim(f"  trip {th['high']:.0f}°C / resume {th['low']:.0f}°C"))
    else:
        row("Guard", dim("off"))
    print(bold("Performance"))
    row("Profile", bold(pr["active"] or "n/a") + dim(f"  [{' '.join(pr['choices'])}]"))
    if pr["driver"]:
        row("Driver", dim(pr["driver"]))
    for r in st["reasons"]:
        print(f"  {c('→', '36')} {r}")
    for w in st["warnings"]:
        print(f"  {yellow('!')} {w}")
    if st.get("error"):
        print(f"  {red('✗')} {st['error']}")


def cmd_detect(_):
    from .hardware import Hardware
    hw = Hardware()
    caps = hw.caps.to_dict()
    for k, v in caps.items():
        if v not in (None, [], False):
            print(f"{k.ljust(22)}{v}")
    print(f"{'charge_mode'.ljust(22)}{hw.charge_mode()}")
    print(f"{'temperature'.ljust(22)}{hw.temperature()}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="lpctl", description="Control lpowerd (Lenovo power daemon)")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", metavar="COMMAND")

    p = sub.add_parser("status", help="show battery, charging and profile state")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("set-threshold", help="charge limit in percent (100 = off)")
    p.add_argument("percent", type=int)
    p = sub.add_parser("conservation", help="firmware conservation mode on/off")
    p.add_argument("state", type=onoff)
    p = sub.add_parser("rapid-charge", help="rapid charge on/off")
    p.add_argument("state", type=onoff)
    p = sub.add_parser("profile", help="show or set performance profile")
    p.add_argument("name", nargs="?", help="quiet | balance | performance | extreme")
    p = sub.add_parser("schedule", help="charge to 100%% only inside HH:MM-HH:MM, or 'off'")
    p.add_argument("window")
    p = sub.add_parser("thermal", help="temperature-aware charging on/off")
    p.add_argument("state", type=onoff)
    p.add_argument("--high", type=float, default=0, help="trip temperature °C (default: auto)")
    p.add_argument("--low", type=float, default=0, help="resume temperature °C (default: auto)")
    sub.add_parser("full-once", help="charge to 100%% once, then return to the limit")
    sub.add_parser("reapply", help="re-detect hardware and re-apply all settings")
    sub.add_parser("detect", help="print detected hardware interfaces (no daemon needed)")
    p = sub.add_parser("monitor", help="print status changes as they happen")
    p.add_argument("--json", action="store_true")

    a = ap.parse_args(argv)
    cmd = a.cmd or "status"

    if cmd == "detect":
        return cmd_detect(a)
    if cmd == "status":
        st = json.loads(str(call("GetStatus")))
        if getattr(a, "json", False):
            print(json.dumps(st, indent=2))
        else:
            print_status(st)
        return
    if cmd == "set-threshold":
        call("SetChargeLimit", a.percent)
    elif cmd == "conservation":
        call("SetConservation", a.state)
    elif cmd == "rapid-charge":
        call("SetRapidCharge", a.state)
    elif cmd == "profile":
        if a.name:
            call("SetProfile", a.name)
        else:
            pr = json.loads(str(call("GetStatus")))["profile"]
            for ch in pr["choices"]:
                mark = green("●") if ch == pr["active"] else dim("○")
                print(f"{mark} {ch}")
            return
    elif cmd == "schedule":
        if a.window.lower() == "off":
            call("SetSchedule", "", "")
        else:
            try:
                start, end = a.window.split("-")
            except ValueError:
                sys.exit(red("expected HH:MM-HH:MM, e.g. 05:00-07:00"))
            call("SetSchedule", start, end)
    elif cmd == "thermal":
        call("SetThermalGuard", a.state, a.high, a.low)
    elif cmd == "full-once":
        call("ChargeFullOnce")
    elif cmd == "reapply":
        call("Reapply")
    elif cmd == "monitor":
        return monitor(a.json)
    print_status(json.loads(str(call("GetStatus"))))


def monitor(as_json):
    import dbus
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib
    DBusGMainLoop(set_as_default=True)
    proxy()  # fail early if not running
    bus = dbus.SessionBus() if os.environ.get("LPOWERD_BUS") == "session" else dbus.SystemBus()

    def on_change(s):
        st = json.loads(str(s))
        if as_json:
            print(json.dumps(st), flush=True)
        else:
            print(dim("─" * 40))
            print_status(st)
            sys.stdout.flush()

    bus.add_signal_receiver(on_change, "StatusChanged", INTERFACE, BUS_NAME, OBJECT_PATH)
    try:
        GLib.MainLoop().run()
    except KeyboardInterrupt:
        pass
