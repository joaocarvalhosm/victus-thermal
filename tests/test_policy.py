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
    kw.setdefault("engage_delay", 0.0)
    kw.setdefault("startup_grace", 0.0)
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
        p = Policy(Config(smoothing=1.0, hysteresis=5.0, engage_delay=0.0, startup_grace=0.0,
                          curves=cfg().curves))
        self.assertEqual(p.decide(readings(cpu=75), "curve", True).pwm, 166)
        self.assertEqual(p.decide(readings(cpu=72), "curve", True).pwm, 166)


class HoldTest(unittest.TestCase):
    """Fans that the curve has started keep a gentle speed for hold_time."""

    def policy(self, hold=90.0):
        return Policy(cfg(hold_time=hold))

    def test_holds_lowest_speed_after_cooling(self):
        p = self.policy()
        self.assertEqual(p.decide(readings(cpu=75), "curve", True, now=0).kind, MANUAL)
        d = p.decide(readings(cpu=45), "curve", True, now=10)
        self.assertEqual(d.kind, MANUAL)
        self.assertEqual(d.duty, 30)                 # balanced curve's lowest speed
        self.assertAlmostEqual(d.hold_remaining, 80)

    def test_releases_to_firmware_after_hold(self):
        p = self.policy()
        p.decide(readings(cpu=75), "curve", True, now=0)
        self.assertEqual(p.decide(readings(cpu=45), "curve", True, now=91).kind, AUTO)

    def test_each_warm_reading_restarts_the_hold(self):
        p = self.policy()
        p.decide(readings(cpu=75), "curve", True, now=0)
        p.decide(readings(cpu=70), "curve", True, now=80)          # still warm
        self.assertEqual(p.decide(readings(cpu=45), "curve", True, now=150).kind, MANUAL)
        self.assertEqual(p.decide(readings(cpu=45), "curve", True, now=171).kind, AUTO)

    def test_cool_machine_never_engages(self):
        p = self.policy()
        for t in range(0, 300, 2):
            self.assertEqual(p.decide(readings(cpu=45), "curve", True, now=t).kind, AUTO)

    def test_override_cancels_hold(self):
        p = self.policy()
        p.decide(readings(cpu=75), "curve", True, now=0)
        p.decide(readings(cpu=45), "auto", True, now=5)
        self.assertEqual(p.decide(readings(cpu=45), "curve", True, now=6).kind, AUTO)

    def test_emergency_still_wins_during_hold(self):
        p = self.policy()
        p.decide(readings(cpu=75), "curve", True, now=0)
        self.assertEqual(p.decide(readings(cpu=97), "curve", True, now=5).kind, MAX)

    def test_zero_hold_disables_it(self):
        p = self.policy(hold=0)
        p.decide(readings(cpu=75), "curve", True, now=0)
        self.assertEqual(p.decide(readings(cpu=45), "curve", True, now=0.1).kind, AUTO)


def on_battery(**kw):
    r = readings(**kw)
    return Readings(**{**r.__dict__, "on_ac": False})


class AcQuietTest(unittest.TestCase):
    def test_floor_when_cool_on_ac(self):
        d = Policy(cfg()).decide(readings(cpu=40), "curve", True, now=0, ac_quiet=True)
        self.assertEqual((d.kind, d.duty), (MANUAL, 15))

    def test_curve_above_the_floor(self):
        d = Policy(cfg()).decide(readings(cpu=75), "curve", True, now=0, ac_quiet=True)
        self.assertEqual((d.kind, d.duty), (MANUAL, 65))

    def test_no_engage_delay_when_quiet_is_running(self):
        p = Policy(cfg(engage_delay=6.0))
        p.decide(readings(cpu=40), "curve", True, now=0, ac_quiet=True)
        self.assertEqual(p.decide(readings(cpu=75), "curve", True, now=2, ac_quiet=True).duty, 65)

    def test_battery_is_untouched(self):
        d = Policy(cfg()).decide(on_battery(cpu=40), "curve", True, now=0, ac_quiet=True)
        self.assertEqual(d.kind, AUTO)

    def test_unplugging_returns_to_firmware(self):
        p = Policy(cfg())
        p.decide(readings(cpu=40), "curve", True, now=0, ac_quiet=True)
        self.assertEqual(p.decide(on_battery(cpu=40), "curve", True, now=2,
                                  ac_quiet=True).kind, AUTO)

    def test_overrides_and_emergency_win(self):
        p = Policy(cfg())
        self.assertEqual(p.decide(readings(cpu=40), "auto", True, ac_quiet=True).kind, AUTO)
        self.assertEqual(p.decide(readings(cpu=97), "curve", True, ac_quiet=True).kind, MAX)

    def test_firmware_profile_is_respected(self):
        p = Policy(cfg(curves={"balanced": FIRMWARE}))
        self.assertEqual(p.decide(readings(cpu=40), "curve", True, ac_quiet=True).kind, AUTO)


class StartupGraceTest(unittest.TestCase):
    """Right after the daemon starts (boot), the firmware keeps the fans."""

    def test_firmware_during_grace_even_when_hot(self):
        p = Policy(cfg(startup_grace=90.0))
        d = p.decide(readings(cpu=86), "curve", True, now=100)
        self.assertEqual(d.kind, AUTO)
        self.assertIn("startup", d.reason)

    def test_curve_after_grace(self):
        p = Policy(cfg(startup_grace=90.0))
        p.decide(readings(cpu=86), "curve", True, now=100)
        self.assertEqual(p.decide(readings(cpu=86), "curve", True, now=191).kind, MANUAL)

    def test_emergency_still_immediate(self):
        p = Policy(cfg(startup_grace=90.0))
        self.assertEqual(p.decide(readings(cpu=97), "curve", True, now=100).kind, MAX)

    def test_explicit_user_mode_respected(self):
        p = Policy(cfg(startup_grace=90.0, max_profiles=("balanced",)))
        self.assertEqual(p.decide(readings(cpu=50), "max", True, now=100).kind, MAX)

    def test_ac_quiet_waits_for_grace_too(self):
        p = Policy(cfg(startup_grace=90.0))
        self.assertEqual(p.decide(readings(cpu=40), "curve", True, now=100,
                                  ac_quiet=True).kind, AUTO)

    def test_boot_spike_does_not_survive_grace(self):
        # 86 C at boot, settling to 45 C: once the grace ends the smoothed
        # temperature has followed, so the fans are not started for nothing.
        p = Policy(Config(startup_grace=90.0, engage_delay=0.0))
        p.decide(readings(cpu=86), "curve", True, now=0)
        for t in range(2, 92, 2):
            p.decide(readings(cpu=45), "curve", True, now=t)
        self.assertEqual(p.decide(readings(cpu=45), "curve", True, now=92).kind, AUTO)

    def test_grace_survives_config_reload(self):
        p = Policy(cfg(startup_grace=90.0))
        p.decide(readings(cpu=50), "curve", True, now=0)
        p.reconfigure(cfg(startup_grace=90.0, poll_interval=1.0))
        self.assertEqual(p.decide(readings(cpu=86), "curve", True, now=91).kind, MANUAL)


class EngageDelayTest(unittest.TestCase):
    def test_waits_for_sustained_demand(self):
        p = Policy(cfg(engage_delay=6.0))
        kinds = [p.decide(readings(cpu=75), "curve", True, now=t).kind for t in (0, 2, 4, 6)]
        self.assertEqual(kinds, [AUTO, AUTO, AUTO, MANUAL])

    def test_interrupted_demand_starts_over(self):
        p = Policy(cfg(engage_delay=6.0))
        p.decide(readings(cpu=75), "curve", True, now=0)
        p.decide(readings(cpu=45), "curve", True, now=4)
        self.assertEqual(p.decide(readings(cpu=75), "curve", True, now=6).kind, AUTO)

    def test_full_curve_is_immediate(self):
        p = Policy(cfg(engage_delay=6.0))
        self.assertEqual(p.decide(readings(cpu=91), "curve", True, now=0).kind, MAX)

    def test_no_delay_while_already_in_control(self):
        p = Policy(cfg(engage_delay=6.0))
        for t in (0, 2, 4, 6):
            p.decide(readings(cpu=75), "curve", True, now=t)
        self.assertEqual(p.decide(readings(cpu=80), "curve", True, now=8).kind, MANUAL)


class SpikeFilterTest(unittest.TestCase):
    def test_short_spike_does_not_start_the_fans(self):
        p = Policy(Config(startup_grace=0.0))        # shipped defaults otherwise
        for t in range(0, 20, 2):
            p.decide(readings(cpu=45), "curve", True, now=t)
        # a 2-sample burst to 62 C (Tctl spike while opening an app)
        kinds = [p.decide(readings(cpu=62), "curve", True, now=20 + 2 * i).kind
                 for i in range(2)]
        self.assertEqual(kinds, [AUTO, AUTO])

    def test_sustained_heat_starts_the_fans(self):
        p = Policy(Config(startup_grace=0.0))
        for t in range(0, 20, 2):
            p.decide(readings(cpu=45), "curve", True, now=t)
        kinds = [p.decide(readings(cpu=62), "curve", True, now=20 + 2 * i).kind
                 for i in range(10)]
        self.assertIn(MANUAL, kinds)

    def test_emergency_ignores_smoothing(self):
        p = Policy(Config(startup_grace=0.0))
        p.decide(readings(cpu=45), "curve", True, now=0)
        self.assertEqual(p.decide(readings(cpu=96), "curve", True, now=2).kind, MAX)


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
