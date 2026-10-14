import os
import tempfile
import unittest

from victus_thermal import config
from victus_thermal.curve import FanCurve


class ParseTest(unittest.TestCase):
    def test_empty_document_gives_defaults(self):
        self.assertEqual(config.parse({}), config.Config())

    def test_overrides_values_and_curves(self):
        cfg = config.parse({
            "poll_interval": 1,
            "curves": {"balanced": [[50, 0], [80, 100]], "low-power": "firmware"},
        })
        self.assertEqual(cfg.poll_interval, 1.0)
        self.assertEqual(cfg.curves["balanced"], FanCurve(((50, 0), (80, 100))))
        self.assertEqual(cfg.curves["low-power"], config.FIRMWARE)
        # untouched profiles keep their defaults
        self.assertEqual(cfg.curves["performance"], config.Config().curves["performance"])

    def test_rejects_invalid_documents(self):
        bad = [
            {"poll_intervall": 2},                              # typo
            {"poll_interval": 0},                               # too fast
            {"poll_interval": True},                            # bool is not a number
            {"emergency_temp": 120},                            # above silicon limit
            {"emergency_temp": 90, "emergency_release": 92},    # inverted band
            {"curves": {"turbo": [[50, 0], [80, 100]]}},        # unknown profile
            {"curves": {"balanced": [[80, 0], [50, 100]]}},     # bad curve
            {"curves": [1, 2]},                                 # not a table
            {"ac_quiet": "yes"},                                # not a bool
            {"ac_quiet_duty": 80},                              # not quiet
            {"max_profiles": "performance"},                    # not a list
            {"max_profiles": ["turbo"]},                        # unknown profile
        ]
        for doc in bad:
            with self.subTest(doc=doc), self.assertRaises(ValueError):
                config.parse(doc)


class LoadTest(unittest.TestCase):
    def _write(self, text):
        fd, path = tempfile.mkstemp(suffix=".toml")
        with os.fdopen(fd, "w") as f:
            f.write(text)
        self.addCleanup(os.unlink, path)
        return path

    def test_missing_file_is_silent_defaults(self):
        cfg, err = config.load("/nonexistent/victus.toml")
        self.assertEqual(cfg, config.Config())
        self.assertIsNone(err)

    def test_broken_toml_falls_back_with_error(self):
        cfg, err = config.load(self._write("poll_interval = [\n"))
        self.assertEqual(cfg, config.Config())
        self.assertIn("built-in defaults", err)

    def test_invalid_value_falls_back_whole_file(self):
        path = self._write('poll_interval = 1\nemergency_temp = 200\n')
        cfg, err = config.load(path)
        self.assertEqual(cfg.poll_interval, 2.0)   # not partially applied
        self.assertIn("emergency_temp", err)

    def test_shipped_default_config_is_valid(self):
        here = os.path.dirname(__file__)
        cfg, err = config.load(os.path.join(here, "..", "packaging", "config.toml"))
        self.assertIsNone(err)
        self.assertEqual(cfg, config.Config())


if __name__ == "__main__":
    unittest.main()
