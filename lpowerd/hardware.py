"""Runtime detection of Lenovo ACPI/WMI interfaces and sysfs I/O.

Nothing here hardcodes a single path: every interface is located by
probing a list of known layouts (driver dir, bus device, PCI/EC tree,
legacy firmware path) so the same code works across kernel versions.
All paths are resolved relative to `root` so tests can use a fake tree.
"""

import glob
import logging
import os
from dataclasses import dataclass, field, asdict

log = logging.getLogger(__name__)

# charge_types values (kernel >= 6.14 ideapad_laptop) -> our mode names
CHARGE_TYPE_TO_MODE = {"Long_Life": "conservation", "Standard": "standard", "Fast": "rapid"}
MODE_TO_CHARGE_TYPE = {v: k for k, v in CHARGE_TYPE_TO_MODE.items()}

IDEAPAD_PATTERNS = (
    "sys/bus/platform/drivers/ideapad_acpi/VPC2004:*",
    "sys/bus/platform/drivers/ideapad_laptop/VPC2004:*",
    "sys/bus/platform/devices/VPC2004:*",
    "sys/devices/pci*/*/PNP0C09:*/VPC2004:*",
    "sys/devices/pci*/*/*/PNP0C09:*/VPC2004:*",
    "sys/devices/platform/VPC2004:*",
)

# LenovoLegionLinux out-of-tree driver
LEGION_PATTERNS = (
    "sys/bus/platform/drivers/legion/PNP0C09:*",
    "sys/devices/pci*/*/PNP0C09:*",
)


def read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def read_int(path):
    v = read(path)
    try:
        return int(v) if v is not None else None
    except ValueError:
        return None


def write(path, value):
    """Write a sysfs attribute. Raises OSError with a readable message."""
    try:
        with open(path, "w") as f:
            f.write(str(value))
    except OSError as e:
        raise OSError(e.errno, f"write {value!r} to {path}: {e.strerror}") from None
    log.info("wrote %s <- %s", path, value)


def parse_choices(text):
    """'Fast Standard [Long_Life]' -> (['Fast','Standard','Long_Life'], 'Long_Life')"""
    choices, active = [], None
    for tok in (text or "").split():
        if tok.startswith("[") and tok.endswith("]"):
            tok = tok[1:-1]
            active = tok
        choices.append(tok)
    if active is None and len(choices) == 1:
        active = choices[0]  # some attributes read back only the current value
    return choices, active


@dataclass
class Capabilities:
    ideapad: str = None
    battery: str = None
    ac: str = None
    charge_types: str = None
    charge_types_choices: list = field(default_factory=list)
    conservation: str = None
    rapid: str = None
    end_threshold: str = None
    start_threshold: str = None
    behaviour: str = None
    behaviour_choices: list = field(default_factory=list)
    profile: str = None
    profile_choices: list = field(default_factory=list)
    profile_driver: str = None
    temp: str = None
    temp_kind: str = None  # "battery" | "proxy:<hwmon name>"
    fan_mode: str = None
    fn_lock: str = None

    @property
    def has_conservation(self):
        return bool(self.conservation or "Long_Life" in self.charge_types_choices)

    @property
    def has_rapid(self):
        return bool(self.rapid or "Fast" in self.charge_types_choices)

    @property
    def has_inhibit(self):
        return "inhibit-charge" in self.behaviour_choices

    @property
    def has_end_threshold(self):
        return bool(self.end_threshold)

    def to_dict(self):
        d = asdict(self)
        d.update(has_conservation=self.has_conservation, has_rapid=self.has_rapid,
                 has_inhibit=self.has_inhibit, has_end_threshold=self.has_end_threshold)
        return d


class Hardware:
    def __init__(self, root="/", temp_sensor="auto"):
        self.root = root
        self.temp_sensor = temp_sensor
        self.caps = Capabilities()
        self.detect()

    def _p(self, rel):
        return os.path.join(self.root, rel)

    def _first(self, patterns, attr):
        for pat in patterns:
            for d in sorted(glob.glob(self._p(pat))):
                if os.path.exists(os.path.join(d, attr)):
                    return os.path.realpath(d)
        return None

    # ---------------------------------------------------------------- detect
    def detect(self):
        c = Capabilities()
        c.ideapad = self._first(IDEAPAD_PATTERNS, "conservation_mode")
        if c.ideapad:
            c.conservation = os.path.join(c.ideapad, "conservation_mode")
            for name in ("fan_mode", "fn_lock"):
                p = os.path.join(c.ideapad, name)
                if os.path.exists(p):
                    setattr(c, name, p)

        legion = self._first(LEGION_PATTERNS, "rapidcharge")
        if legion:
            c.rapid = os.path.join(legion, "rapidcharge")

        self._detect_power_supply(c)
        self._detect_profile(c)
        self._detect_temp(c)

        # The new charge_types API supersedes conservation_mode/rapidcharge;
        # driving both risks fighting over the same EC bits.
        if c.charge_types_choices:
            c.conservation = None
            c.rapid = None
        self.caps = c
        log.info("detected: %s", {k: v for k, v in c.to_dict().items() if v})
        return c

    def _detect_power_supply(self, c):
        for d in sorted(glob.glob(self._p("sys/class/power_supply/*"))):
            typ = read(os.path.join(d, "type"))
            if typ == "Battery" and c.battery is None:
                if read(os.path.join(d, "scope")) == "Device":
                    continue  # wireless mouse etc.
                c.battery = d
            elif typ == "Mains" and c.ac is None:
                c.ac = d
        if not c.battery:
            return
        b = c.battery
        ct = os.path.join(b, "charge_types")
        if os.path.exists(ct):
            choices, _ = parse_choices(read(ct))
            if any(x in CHARGE_TYPE_TO_MODE for x in choices):
                c.charge_types = ct
                c.charge_types_choices = choices
        for attr, name in (("end_threshold", "charge_control_end_threshold"),
                           ("start_threshold", "charge_control_start_threshold")):
            p = os.path.join(b, name)
            if os.path.exists(p):
                setattr(c, attr, p)
        beh = os.path.join(b, "charge_behaviour")
        if os.path.exists(beh):
            c.behaviour = beh
            c.behaviour_choices, _ = parse_choices(read(beh))

    def _detect_profile(self, c):
        # Kernel >= 6.14 class interface; prefer a Lenovo handler if several.
        dirs = sorted(glob.glob(self._p("sys/class/platform-profile/platform-profile-*")))
        dirs.sort(key=lambda d: "lenovo" not in (read(os.path.join(d, "name")) or ""))
        for d in dirs:
            if os.path.exists(os.path.join(d, "profile")):
                c.profile = os.path.join(d, "profile")
                c.profile_choices = (read(os.path.join(d, "choices")) or "").split()
                c.profile_driver = read(os.path.join(d, "name"))
                return
        legacy = self._p("sys/firmware/acpi/platform_profile")
        if os.path.exists(legacy):
            c.profile = legacy
            c.profile_choices = (read(legacy + "_choices") or "").split()
            c.profile_driver = "acpi"

    def _detect_temp(self, c):
        if c.battery and self.temp_sensor in ("auto", "battery"):
            p = os.path.join(c.battery, "temp")  # tenths of a degree
            if read_int(p) is not None:
                c.temp, c.temp_kind = p, "battery"
                return
            for h in glob.glob(os.path.join(c.battery, "hwmon*", "temp1_input")):
                c.temp, c.temp_kind = h, "battery"
                return
        # No battery cell sensor: fall back to the CPU package sensor. acpitz is
        # last because it means different things on different boards.
        if self.temp_sensor in ("auto", "battery"):
            wanted = ("k10temp", "coretemp", "zenpower", "acpitz")
        else:
            wanted = (self.temp_sensor,)
        found = {}
        for h in sorted(glob.glob(self._p("sys/class/hwmon/hwmon*"))):
            name = read(os.path.join(h, "name"))
            p = os.path.join(h, "temp1_input")
            if name in wanted and name not in found and read_int(p) is not None:
                found[name] = p
        for name in wanted:
            if name in found:
                c.temp, c.temp_kind = found[name], f"proxy:{name}"
                return

    # ------------------------------------------------------------- readings
    def battery_info(self):
        b = self.caps.battery
        if not b:
            return {}
        info = {
            "capacity": read_int(os.path.join(b, "capacity")),
            "status": read(os.path.join(b, "status")),
            "cycle_count": read_int(os.path.join(b, "cycle_count")),
            "model": read(os.path.join(b, "model_name")),
        }
        full = read_int(os.path.join(b, "energy_full")) or read_int(os.path.join(b, "charge_full"))
        design = (read_int(os.path.join(b, "energy_full_design"))
                  or read_int(os.path.join(b, "charge_full_design")))
        if full and design:
            info["health"] = round(100 * full / design, 1)
        power = read_int(os.path.join(b, "power_now"))
        if power is not None:
            info["power_w"] = round(power / 1e6, 2)
        return info

    def on_ac(self):
        if self.caps.ac:
            return read(os.path.join(self.caps.ac, "online")) == "1"
        status = self.battery_info().get("status")
        return status in ("Charging", "Full", "Not charging")

    def temperature(self):
        v = read_int(self.caps.temp) if self.caps.temp else None
        if v is None:
            return None
        if self.caps.temp.endswith("/temp"):
            return v / 10.0
        return v / 1000.0

    # -------------------------------------------------------- charge mode
    def charge_mode(self):
        c = self.caps
        if c.charge_types:
            _, active = parse_choices(read(c.charge_types))
            return CHARGE_TYPE_TO_MODE.get(active)
        if c.conservation and read(c.conservation) == "1":
            return "conservation"
        if c.rapid and read(c.rapid) == "1":
            return "rapid"
        return "standard" if (c.conservation or c.rapid) else None

    def set_charge_mode(self, mode):
        c = self.caps
        if mode == "rapid" and not c.has_rapid:
            mode = "standard"
        if mode == "conservation" and not c.has_conservation:
            raise OSError(95, "conservation mode not supported on this machine")
        if c.charge_types:
            want = MODE_TO_CHARGE_TYPE[mode]
            if want in c.charge_types_choices:
                write(c.charge_types, want)
            return
        # Legacy attributes: firmware treats these as mutually exclusive, so
        # always turn the other one off first.
        if mode == "conservation":
            if c.rapid and read(c.rapid) == "1":
                write(c.rapid, 0)
            write(c.conservation, 1)
        elif mode == "rapid":
            if c.conservation and read(c.conservation) == "1":
                write(c.conservation, 0)
            write(c.rapid, 1)
        else:
            if c.rapid and read(c.rapid) == "1":
                write(c.rapid, 0)
            if c.conservation and read(c.conservation) == "1":
                write(c.conservation, 0)

    def inhibited(self):
        if not self.caps.behaviour:
            return None
        return parse_choices(read(self.caps.behaviour))[1] == "inhibit-charge"

    def set_inhibit(self, on):
        if self.caps.has_inhibit:
            write(self.caps.behaviour, "inhibit-charge" if on else "auto")

    def end_threshold(self):
        return read_int(self.caps.end_threshold) if self.caps.end_threshold else None

    def set_end_threshold(self, value):
        c = self.caps
        start = max(0, value - 5)
        # Order matters: kernels reject start >= end.
        if c.start_threshold and (read_int(c.start_threshold) or 0) >= value:
            write(c.start_threshold, start)
        write(c.end_threshold, value)
        if c.start_threshold:
            write(c.start_threshold, start)

    # -------------------------------------------------------------- profile
    def profile(self):
        return read(self.caps.profile) if self.caps.profile else None

    def set_profile(self, raw):
        if not self.caps.profile:
            raise OSError(95, "no platform_profile interface found")
        if raw not in self.caps.profile_choices:
            raise OSError(22, f"profile {raw!r} not in {self.caps.profile_choices}")
        write(self.caps.profile, raw)


def resolve_profile(name, choices):
    """Map a friendly name (quiet/balance/...) or a raw value to a choice."""
    from . import PROFILE_ALIASES
    name = name.strip().lower()
    if name in choices:
        return name
    for cand in PROFILE_ALIASES.get(name, ()):
        if cand in choices:
            return cand
    raise ValueError(f"unknown profile {name!r}; available: {', '.join(friendly_profiles(choices))}")


def friendly_profile(raw):
    from . import PROFILE_ALIASES
    for friendly, raws in PROFILE_ALIASES.items():
        if raw in raws:
            return friendly
    return raw


def friendly_profiles(choices):
    return [friendly_profile(c) for c in choices]
