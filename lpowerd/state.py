"""Persistent user settings (survive reboot, suspend and daemon restarts)."""

import copy
import json
import logging
import os
import tempfile

log = logging.getLogger(__name__)

DEFAULTS = {
    "version": 1,
    "charge_limit": 100,          # 100 = no limit
    "rapid_charge": False,
    "conservation_level": 80,     # firmware's fixed conservation cap
    "profile": None,              # raw platform_profile value, None = unmanaged
    "schedule": None,             # {"start": "HH:MM", "end": "HH:MM"} -> charge to 100% inside
    "thermal": {
        "enabled": False,
        "sensor": "auto",         # auto | battery | <hwmon name>
        "high": None,             # None = default for the sensor kind
        "low": None,
    },
    # max-power/custom are advertised by the kernel but not offered by Lenovo's
    # own software on many models (e.g. LOQ); hidden unless set to true here.
    "advanced_profiles": False,
    "poll_interval": 60,          # seconds, only while a policy needs it on AC
}

# Default trip points: real battery cells vs. EC/ACPI thermal zone proxies.
THERMAL_DEFAULTS = {"battery": (45.0, 40.0), "proxy": (85.0, 75.0)}


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(out.get(k), dict) and isinstance(v, dict):
            out[k] = _merge(out[k], v)
        elif k in out:
            out[k] = v
    return out


class Store:
    def __init__(self, path):
        self.path = path
        self.data = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self):
        try:
            with open(self.path) as f:
                self.data = _merge(DEFAULTS, json.load(f))
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log.warning("ignoring unreadable state %s: %s", self.path, e)

    def save(self):
        d = os.path.dirname(self.path) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".state-")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.data, f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            os.unlink(tmp)
            raise

    def __getitem__(self, k):
        return self.data[k]

    def update(self, **kw):
        self.data.update(kw)
        self.save()
