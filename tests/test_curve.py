import unittest

from victus_thermal.curve import Ema, FanCurve, Hysteresis, duty_to_pwm


class FanCurveTest(unittest.TestCase):
    def setUp(self):
        self.curve = FanCurve(((50, 0), (70, 40), (90, 100)))

    def test_clamps_below_and_above(self):
        self.assertEqual(self.curve.duty(20), 0)
        self.assertEqual(self.curve.duty(105), 100)

    def test_interpolates_between_points(self):
        self.assertAlmostEqual(self.curve.duty(60), 20)
        self.assertAlmostEqual(self.curve.duty(80), 70)

    def test_exact_points(self):
        self.assertEqual(self.curve.duty(70), 40)
        self.assertEqual(self.curve.duty(90), 100)

    def test_rejects_unsafe_shapes(self):
        bad = [
            ((50, 0),),                    # single point
            ((50, 0), (50, 10)),           # repeated temperature
            ((60, 0), (50, 10)),           # temperatures out of order
            ((50, 60), (70, 40)),          # duty falls as it heats up
            ((50, 0), (70, 120)),          # duty above 100 %
            ((50, -5), (70, 40)),          # negative duty
            ((50, 0), (150, 100)),         # absurd temperature
        ]
        for points in bad:
            with self.subTest(points=points), self.assertRaises(ValueError):
                FanCurve(points)

    def test_from_pairs_rejects_garbage(self):
        with self.assertRaises(ValueError):
            FanCurve.from_pairs([[50, "fast"], [70, 40]])
        with self.assertRaises(ValueError):
            FanCurve.from_pairs([50, 70])


class HysteresisTest(unittest.TestCase):
    def test_rises_immediately(self):
        h = Hysteresis(band=4)
        self.assertEqual(h.update(60), 60)
        self.assertEqual(h.update(75), 75)

    def test_falls_only_past_band(self):
        h = Hysteresis(band=4)
        h.update(80)
        self.assertEqual(h.update(77), 80)   # inside the band: hold
        self.assertEqual(h.update(76), 80)   # exactly the band: hold
        self.assertEqual(h.update(70), 74)   # past it: follow, lagged by band

    def test_reset(self):
        h = Hysteresis(band=4)
        h.update(80)
        h.reset()
        self.assertEqual(h.update(50), 50)


class EmaTest(unittest.TestCase):
    def test_first_sample_passes_through(self):
        self.assertEqual(Ema(0.5).update(70), 70)

    def test_smooths_spike(self):
        e = Ema(0.5)
        e.update(60)
        self.assertEqual(e.update(80), 70)

    def test_alpha_one_is_identity(self):
        e = Ema(1.0)
        e.update(60)
        self.assertEqual(e.update(80), 80)


class DutyToPwmTest(unittest.TestCase):
    def test_range(self):
        self.assertEqual(duty_to_pwm(0), 0)
        self.assertEqual(duty_to_pwm(100), 255)
        self.assertEqual(duty_to_pwm(50), 128)

    def test_clamps(self):
        self.assertEqual(duty_to_pwm(150), 255)
        self.assertEqual(duty_to_pwm(-10), 0)


if __name__ == "__main__":
    unittest.main()
