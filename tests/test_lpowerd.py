import copy
import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lpowerd import policy  # noqa: E402
from lpowerd.hardware import Capabilities, Hardware, parse_choices, resolve_profile  # noqa: E402
from lpowerd.state import DEFAULTS, Store  # noqa: E402


def make_tree(root, *, charge_types=True, legacy=False, inhibit=False, threshold=False):
    def w(rel, val):
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(val + "\n")

    vpc = "sys/devices/pci0000:00/0000:00:14.3/PNP0C09:00/VPC2004:00"
    w(f"{vpc}/conservation_mode", "0")
    w(f"{vpc}/fan_mode", "0")
    os.makedirs(os.path.join(root, "sys/bus/platform/devices"), exist_ok=True)
    os.symlink(os.path.join(root, vpc), os.path.join(root, "sys/bus/platform/devices/VPC2004:00"))
    bat = "sys/class/power_supply/BAT1"
    w(f"{bat}/type", "Battery")
    w(f"{bat}/capacity", "58")
    w(f"{bat}/status", "Charging")
    w(f"{bat}/energy_full", "54000000")
    w(f"{bat}/energy_full_design", "60000000")
    if charge_types:
        w(f"{bat}/charge_types", "Fast [Standard] Long_Life")
    if inhibit:
        w(f"{bat}/charge_behaviour", "[auto] inhibit-charge")
    if threshold:
        w(f"{bat}/charge_control_end_threshold", "100")
        w(f"{bat}/charge_control_start_threshold", "0")
    w("sys/class/power_supply/ACAD/type", "Mains")
    w("sys/class/power_supply/ACAD/online", "1")
    w("sys/class/power_supply/mouse/type", "Battery")
    w("sys/class/power_supply/mouse/scope", "Device")
    pp = "sys/class/platform-profile/platform-profile-0"
    w(f"{pp}/name", "lenovo-wmi-gamezone")
    w(f"{pp}/choices", "low-power balanced performance max-power custom")
    w(f"{pp}/profile", "balanced")
    w("sys/class/hwmon/hwmon2/name", "acpitz")
    w("sys/class/hwmon/hwmon2/temp1_input", "61000")
    if legacy:
        w("sys/bus/platform/drivers/legion/PNP0C09:00/rapidcharge", "0")
    return root


class FakeHW(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_detect_charge_types(self):
        make_tree(self.root)
        hw = Hardware(self.root)
        c = hw.caps
        self.assertTrue(c.ideapad.endswith("VPC2004:00"))
        self.assertTrue(c.battery.endswith("BAT1"))  # mouse skipped
        self.assertTrue(c.has_conservation and c.has_rapid)
        self.assertIsNone(c.conservation)  # charge_types wins
        self.assertEqual(c.profile_driver, "lenovo-wmi-gamezone")
        self.assertEqual(c.temp_kind, "proxy:acpitz")
        self.assertEqual(hw.temperature(), 61.0)
        self.assertEqual(hw.charge_mode(), "standard")
        self.assertEqual(hw.battery_info()["health"], 90.0)
        hw.set_charge_mode("conservation")
        with open(c.charge_types) as f:
            self.assertEqual(f.read(), "Long_Life")

    def test_prefers_cpu_sensor_over_acpitz(self):
        make_tree(self.root)
        d = os.path.join(self.root, "sys/class/hwmon/hwmon6")
        os.makedirs(d)
        for name, val in (("name", "k10temp"), ("temp1_input", "49875")):
            with open(os.path.join(d, name), "w") as f:
                f.write(val)
        hw = Hardware(self.root)
        self.assertEqual(hw.caps.temp_kind, "proxy:k10temp")
        self.assertAlmostEqual(hw.temperature(), 49.875)

    def test_legacy_exclusive(self):
        make_tree(self.root, charge_types=False, legacy=True)
        hw = Hardware(self.root)
        self.assertTrue(hw.caps.conservation and hw.caps.rapid)
        hw.set_charge_mode("conservation")
        hw.set_charge_mode("rapid")
        with open(hw.caps.conservation) as f:
            self.assertEqual(f.read().strip(), "0")
        self.assertEqual(hw.charge_mode(), "rapid")

    def test_profile_resolution(self):
        ch = ["low-power", "balanced", "performance", "max-power"]
        self.assertEqual(resolve_profile("quiet", ch), "low-power")
        self.assertEqual(resolve_profile("Extreme", ch), "max-power")
        self.assertEqual(resolve_profile("balanced", ch), "balanced")
        with self.assertRaises(ValueError):
            resolve_profile("turbo", ch)

    def test_parse_choices(self):
        self.assertEqual(parse_choices("Fast Standard [Long_Life]"),
                         (["Fast", "Standard", "Long_Life"], "Long_Life"))

    def test_store_roundtrip(self):
        p = os.path.join(self.root, "s", "state.json")
        s = Store(p)
        s.update(charge_limit=90)
        self.assertEqual(Store(p)["charge_limit"], 90)
        self.assertEqual(Store(p)["thermal"]["enabled"], False)


def caps(**kw):
    c = Capabilities(charge_types="x", charge_types_choices=["Fast", "Standard", "Long_Life"],
                     temp="t", temp_kind="proxy:acpitz")
    for k, v in kw.items():
        setattr(c, k, v)
    return c


NOON = datetime(2026, 10, 27, 12, 0).astimezone()


def run(settings=None, c=None, rt=None, **kw):
    s = copy.deepcopy(DEFAULTS)
    for k, v in (settings or {}).items():
        s[k] = v
    args = dict(now=NOON, capacity=50, on_ac=True, temp=50.0)
    args.update(kw)
    return policy.decide(s, c or caps(), rt or policy.Runtime(), **args)


class Policy(unittest.TestCase):
    def test_defaults_full(self):
        d, _ = run()
        self.assertEqual(d.mode, "standard")
        self.assertFalse(d.poll)

    def test_conservation(self):
        d, _ = run({"charge_limit": 80})
        self.assertEqual(d.mode, "conservation")

    def test_rapid(self):
        d, _ = run({"rapid_charge": True})
        self.assertEqual(d.mode, "rapid")

    def test_soft_limit_hysteresis(self):
        s = {"charge_limit": 90}
        d, rt = run(s, capacity=85)
        self.assertEqual(d.mode, "standard")
        self.assertTrue(d.poll)
        d, rt = run(s, rt=rt, capacity=90)
        self.assertEqual(d.mode, "conservation")
        d, rt = run(s, rt=rt, capacity=88)
        self.assertEqual(d.mode, "conservation")
        d, rt = run(s, rt=rt, capacity=87)
        self.assertEqual(d.mode, "standard")

    def test_below_floor_without_inhibit(self):
        d, _ = run({"charge_limit": 60})
        self.assertEqual(d.mode, "conservation")
        self.assertTrue(d.warnings)

    def test_below_floor_with_inhibit(self):
        c = caps(behaviour="b", behaviour_choices=["auto", "inhibit-charge"])
        d, rt = run({"charge_limit": 60}, c, capacity=61)
        self.assertTrue(d.inhibit)

    def test_hw_threshold(self):
        d, _ = run({"charge_limit": 70}, caps(end_threshold="e"))
        self.assertEqual(d.end_threshold, 70)
        self.assertFalse(d.poll)

    def test_schedule(self):
        s = {"charge_limit": 80, "schedule": {"start": "11:00", "end": "13:00"}}
        d, _ = run(s)
        self.assertEqual(d.target, 100)
        self.assertEqual(d.mode, "standard")
        self.assertEqual(d.next_boundary.hour, 13)
        s["schedule"] = {"start": "23:00", "end": "06:00"}
        d, _ = run(s)
        self.assertEqual(d.mode, "conservation")
        self.assertEqual(d.next_boundary.hour, 23)
        d, _ = run(s, now=datetime(2026, 10, 27, 2, 0).astimezone())
        self.assertEqual(d.target, 100)

    def test_thermal_hysteresis(self):
        s = {"rapid_charge": True, "thermal": {"enabled": True, "sensor": "auto",
                                               "high": 80, "low": 70}}
        d, rt = run(s, temp=79)
        self.assertEqual(d.mode, "rapid")
        d, rt = run(s, rt=rt, temp=81, capacity=50)
        self.assertTrue(rt.thermal_tripped)
        self.assertEqual(d.mode, "standard")
        d, rt = run(s, rt=rt, temp=75, capacity=85)
        self.assertEqual(d.mode, "conservation")  # stops charging above floor
        d, rt = run(s, rt=rt, temp=70)
        self.assertFalse(rt.thermal_tripped)
        self.assertEqual(d.mode, "rapid")
        self.assertTrue(d.poll)

    def test_thermal_ignored_on_battery(self):
        s = {"thermal": {"enabled": True, "sensor": "auto", "high": 80, "low": 70}}
        d, rt = run(s, temp=95, on_ac=False)
        self.assertEqual(d.mode, "standard")
        self.assertFalse(d.poll)

    def test_full_once(self):
        rt = policy.Runtime(full_once=True)
        d, rt = run({"charge_limit": 80}, rt=rt, capacity=90)
        self.assertEqual(d.mode, "standard")
        d, rt = run({"charge_limit": 80}, rt=rt, capacity=100)
        self.assertFalse(rt.full_once)
        self.assertEqual(d.mode, "conservation")


if __name__ == "__main__":
    unittest.main()
