"""victusctl turbo against temporary request and profile files."""

import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from victus_thermal import cli


class TurboTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.mode = os.path.join(self.dir.name, "mode")
        self.profile = os.path.join(self.dir.name, "platform_profile")
        self._write(self.profile, "balanced")
        self.profiles_set = []
        patches = [
            mock.patch.object(cli, "MODE_PATH", self.mode),
            mock.patch.object(cli, "PROFILE_PATH", self.profile),
            mock.patch.object(cli, "_set_profile", self._fake_set_profile),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.dir.cleanup)

    @staticmethod
    def _write(path, value):
        with open(path, "w") as f:
            f.write(value + "\n")

    def _fake_set_profile(self, name):
        # power-profiles-daemon writes the kernel profile
        self.profiles_set.append(name)
        self._write(self.profile, name)
        return True

    def _run(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["turbo", *argv])
        return rc, out.getvalue().strip()

    def _mode(self):
        with open(self.mode) as f:
            return f.read().strip()

    def test_toggle_turns_on_then_off(self):
        self.assertEqual(self._run(), (0, "turbo: off"))

        self.assertEqual(self._run("toggle"), (0, "turbo: on"))
        self.assertEqual(self.profiles_set, ["performance"])
        self.assertEqual(self._mode(), "max")
        self.assertEqual(self._run(), (0, "turbo: on"))

        self.assertEqual(self._run("toggle"), (0, "turbo: off"))
        self.assertEqual(self.profiles_set, ["performance", "balanced"])
        self.assertEqual(self._mode(), "curve")

    def test_performance_without_max_is_not_turbo(self):
        self._write(self.profile, "performance")
        self.assertEqual(self._run("toggle"), (0, "turbo: on"))
        self.assertEqual(self._mode(), "max")

    def test_max_waits_for_the_profile(self):
        # the profile never changes: max must not be requested
        with mock.patch.object(cli, "_set_profile", return_value=True), \
                mock.patch.object(cli, "_wait_profile", return_value=False), \
                contextlib.redirect_stderr(io.StringIO()):
            rc, _ = self._run("on")
        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(self.mode))


if __name__ == "__main__":
    unittest.main()
