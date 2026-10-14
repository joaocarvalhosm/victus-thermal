import unittest

from victus_thermal.config import FIRMWARE, Config
from victus_thermal.curve import FanCurve
from victus_thermal.hw import Readings
from victus_thermal.policy import AUTO, MANUAL, MAX, Policy


def readings(cpu=50.0, igpu=None, dgpu=None, profile="balanced"):
    return Readings(cpu=cpu, igpu=igpu, dgpu=dgpu, dgpu_state="suspended",
                    profile=profile, on_ac=True, rpm=(0, 0), pwm_mode=2)


def cfg(**kw):
    curves = {
        "low-power": FanCurve(((60, 0), (90, 100))),
        "balanced": FanCurve(((50, 0), (60, 30), (90, 100))),
        "performance": FanCurve(((40, 40), (80, 100))),
    }
    curves.update(kw.pop("curves", {}))
    # smoothing=1 and hysteresis=0 make single-step expectations exact
    return Config(smoothing=1.0, hysteresis=0.0, curves=curves, **kw)


class EmergencyTest(unittest.TestCase):
    def test_hot_sensor_forces_max_even_against_override(self):
        p = Policy(cfg())
        d = p.decide(readings(cpu=96), "auto", True)
        self.assertEqual(d.kind, MAX)
        self.assertIn("emergency", d.reason)

    def test_any_sensor_triggers(self):
        p = Policy(cfg())
        self.assertEqual(p.decide(readings(cpu=60, dgpu=97), "curve", True).kind, MAX)

    def test_latches_until_release_temperature(self):
        p = Policy(cfg())
        p.decide(readings(cpu=96), "auto", True)
        self.assertEqual(p.decide(readings(cpu=90), "auto", True).kind, MAX)
        self.assertEqual(p.decide(readings(cpu=85), "auto", True).kind, AUTO)


class OverrideTest(unittest.TestCase):
    def test_auto_and_max(self):
        p = Policy(cfg())
        self.assertEqual(p.decide(readings(cpu=80), "auto", True).kind, AUTO)
        self.assertEqual(p.decide(readings(cpu=40), "max", True).kind, MAX)


class CurveTest(unittest.TestCase):
    def test_missing_cpu_sensor_hands_back_to_firmware(self):
        p = Policy(cfg())
        d = p.decide(readings(cpu=None, dgpu=70), "curve", True)
        self.assertEqual(d.kind, AUTO)
        self.assertIn("cpu sensor", d.reason)

    def test_below_curve_is_firmware_control(self):
        self.assertEqual(Policy(cfg()).decide(readings(cpu=45), "curve", True).kind, AUTO)

    def test_manual_pwm_from_curve(self):
        d = Policy(cfg()).decide(readings(cpu=75), "curve", True)
        self.assertEqual(d.kind, MANUAL)
        self.assertAlmostEqual(d.duty, 65)
        self.assertEqual(d.pwm, 166)

    def test_top_of_curve_uses_max_mode(self):
        self.assertEqual(Policy(cfg()).decide(readings(cpu=91), "curve", True).kind, MAX)

    def test_follows_hottest_sensor(self):
        d = Policy(cfg()).decide(readings(cpu=55, dgpu=75), "curve", True)
        self.assertAlmostEqual(d.curve_temp, 75)

    def test_profile_selects_curve(self):
        d = Policy(cfg()).decide(readings(cpu=60, profile="performance"), "curve", True)
        self.assertAlmostEqual(d.duty, 70)

    def test_unknown_profile_uses_balanced(self):
        d = Policy(cfg()).decide(readings(cpu=75, profile="custom"), "curve", True)
        self.assertAlmostEqual(d.duty, 65)

    def test_firmware_curve(self):
        p = Policy(cfg(curves={"balanced": FIRMWARE}))
        self.assertEqual(p.decide(readings(cpu=85), "curve", True).kind, AUTO)

    def test_hysteresis_holds_on_the_way_down(self):
        p = Policy(Config(smoothing=1.0, hysteresis=5.0, curves=cfg().curves))
        self.assertEqual(p.decide(readings(cpu=75), "curve", True).pwm, 166)
        self.assertEqual(p.decide(readings(cpu=72), "curve", True).pwm, 166)


class TwoStateTest(unittest.TestCase):
    """Stock driver: only MAX/AUTO available."""

    def test_switches_to_max_at_threshold_and_releases_below_gap(self):
        p = Policy(cfg(twostate_max_duty=70))
        self.assertEqual(p.decide(readings(cpu=75), "curve", False).kind, AUTO)   # 65 %
        self.assertEqual(p.decide(readings(cpu=80), "curve", False).kind, MAX)    # 76.7 %
        self.assertEqual(p.decide(readings(cpu=72), "curve", False).kind, MAX)    # 58 %: held
        self.assertEqual(p.decide(readings(cpu=65), "curve", False).kind, AUTO)   # 41.7 %


class ReconfigureTest(unittest.TestCase):
    def test_keeps_emergency_latch(self):
        p = Policy(cfg())
        p.decide(readings(cpu=96), "curve", True)
        p.reconfigure(cfg(poll_interval=1.0))
        self.assertEqual(p.decide(readings(cpu=90), "curve", True).kind, MAX)


if __name__ == "__main__":
    unittest.main()
