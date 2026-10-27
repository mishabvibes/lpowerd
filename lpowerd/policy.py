"""Pure charging policy: settings + sensor readings -> desired hardware state.

Kept free of I/O so every rule (limits, schedule, thermal guard, one-shot
full charge) is unit-testable without a laptop.
"""

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

SOFT_HYSTERESIS = 3  # % below the limit before charging resumes


@dataclass
class Runtime:
    """Hysteresis state carried between evaluations (not persisted)."""
    thermal_tripped: bool = False
    soft_hold: bool = False
    full_once: bool = False


@dataclass
class Decision:
    mode: str = None             # conservation | standard | rapid | None (unsupported)
    inhibit: bool = None         # None = leave alone
    end_threshold: int = None
    target: int = 100
    poll: bool = False
    next_boundary: datetime = None
    reasons: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def parse_hhmm(s):
    h, m = s.strip().split(":")
    t = time(int(h), int(m))
    return t


def in_window(now, start, end):
    t = now.time()
    if start <= end:
        return start <= t < end
    return t >= start or t < end  # crosses midnight


def next_boundary(now, start, end):
    cands = []
    for t in (start, end):
        dt = datetime.combine(now.date(), t, tzinfo=now.tzinfo)
        if dt <= now:
            dt += timedelta(days=1)
        cands.append(dt)
    return min(cands)


def thermal_points(settings, temp_kind):
    from .state import THERMAL_DEFAULTS
    th = settings["thermal"]
    kind = "battery" if temp_kind == "battery" else "proxy"
    hi, lo = THERMAL_DEFAULTS[kind]
    hi = th.get("high") if th.get("high") is not None else hi
    lo = th.get("low") if th.get("low") is not None else lo
    return float(hi), float(min(lo, hi - 1))


def decide(settings, caps, rt, *, now, capacity, on_ac, temp):
    """Return (Decision, new Runtime). `caps` is a hardware.Capabilities."""
    d = Decision()
    rt = Runtime(**vars(rt))
    floor = int(settings["conservation_level"])
    limit = int(settings["charge_limit"])
    rapid = bool(settings["rapid_charge"])

    # ---- target percentage -------------------------------------------
    target = limit
    sched = settings.get("schedule")
    if sched:
        start, end = parse_hhmm(sched["start"]), parse_hhmm(sched["end"])
        d.next_boundary = next_boundary(now, start, end)
        if in_window(now, start, end):
            target = 100
            d.reasons.append(f"scheduled full charge {sched['start']}-{sched['end']}")

    if rt.full_once:
        if not on_ac or (capacity is not None and capacity >= 100):
            rt.full_once = False
            d.reasons.append("one-shot full charge finished")
        else:
            target = 100
            d.reasons.append("one-shot full charge")
    d.target = target

    # ---- base mode for that target ------------------------------------
    fast = "rapid" if (rapid and caps.has_rapid) else "standard"
    soft = False
    if caps.has_end_threshold:
        d.end_threshold = target
        d.mode = fast if (caps.has_rapid or caps.has_conservation) else None
        if caps.has_inhibit:
            d.inhibit = False
    elif target >= 100:
        d.mode = fast
        rt.soft_hold = False
    elif target == floor and caps.has_conservation:
        d.mode = "conservation"
        rt.soft_hold = False
    elif target > floor and caps.has_conservation:
        # Emulate e.g. 90%: charge normally, switch conservation on at the
        # limit (firmware then holds instead of charging).
        soft = True
        _soft_step(rt, capacity, target)
        d.mode = "conservation" if rt.soft_hold else fast
        d.reasons.append(f"software limit {target}% ({'holding' if rt.soft_hold else 'charging'})")
    elif caps.has_inhibit:
        soft = True
        _soft_step(rt, capacity, target)
        d.mode = "conservation" if caps.has_conservation else fast
        d.inhibit = rt.soft_hold
        d.reasons.append(f"software limit {target}% via inhibit-charge")
    elif caps.has_conservation:
        d.mode = "conservation"
        d.warnings.append(f"limit {target}% is below the firmware floor; holding at {floor}%")
    else:
        d.mode = None
        d.warnings.append("no charge control interface found")

    if d.inhibit is None and caps.has_inhibit:
        d.inhibit = False

    if d.mode == "rapid" and target < 100 and not caps.has_end_threshold:
        d.reasons.append("rapid charge active until limit")

    # ---- thermal guard -----------------------------------------------
    th_on = settings["thermal"]["enabled"] and temp is not None
    if th_on:
        hi, lo = thermal_points(settings, caps.temp_kind)
        if rt.thermal_tripped and temp <= lo:
            rt.thermal_tripped = False
        elif not rt.thermal_tripped and temp >= hi:
            rt.thermal_tripped = True
        if rt.thermal_tripped and on_ac:
            d.reasons.append(f"thermal guard: {temp:.0f}°C >= {hi:.0f}°C, charging throttled")
            if d.mode == "rapid":
                d.mode = "standard"
            if caps.has_inhibit:
                d.inhibit = True
            elif caps.has_conservation and capacity is not None and capacity >= floor:
                d.mode = "conservation"
    else:
        if settings["thermal"]["enabled"] and temp is None:
            d.warnings.append("thermal guard enabled but no temperature sensor found")
        rt.thermal_tripped = False

    d.poll = on_ac and (soft or th_on or rt.full_once)
    return d, rt


def _soft_step(rt, capacity, target):
    if capacity is None:
        return
    if rt.soft_hold:
        if capacity <= target - SOFT_HYSTERESIS:
            rt.soft_hold = False
    elif capacity >= target:
        rt.soft_hold = True
