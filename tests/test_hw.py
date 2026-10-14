"""Timing behaviour of the expensive reads (BIOS WMI fan RPM, nvidia-smi)."""

import unittest

from victus_thermal import hw
from victus_thermal.hw import DgpuSampler


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class DgpuSamplerTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.calls = 0
        self.answer = (60.0, 0.0)
        self.sampler = DgpuSampler(self._query, self.clock)

    def _query(self):
        self.calls += 1
        return self.answer

    def at(self, t, status="active"):
        self.clock.now = 1000.0 + t
        return self.sampler.sample(status)

    def test_suspended_gpu_is_never_queried(self):
        for t in range(0, 300, 2):
            self.assertEqual(self.at(t, "suspended"), (None, "suspended"))
        self.assertEqual(self.calls, 0)

    def test_short_wake_up_is_left_alone(self):
        # woken briefly by someone else: we must not extend its awake time
        for t in range(0, int(hw.DGPU_WAKE_GRACE), 2):
            self.assertEqual(self.at(t), (None, "waking"))
        self.at(hw.DGPU_WAKE_GRACE, "suspended")
        self.assertEqual(self.calls, 0)

    def test_idle_awake_gpu_is_queried_rarely(self):
        for t in range(0, 300, 2):
            self.at(t)
        expected = 1 + (300 - 2 - hw.DGPU_WAKE_GRACE) // hw.DGPU_IDLE_INTERVAL
        self.assertEqual(self.calls, expected)

    def test_busy_gpu_is_queried_often(self):
        self.answer = (75.0, 90.0)
        for t in range(0, 120, 1):
            temp, state = self.at(t)
        self.assertEqual((temp, state), (75.0, "active"))
        self.assertGreaterEqual(self.calls, (120 - hw.DGPU_WAKE_GRACE) // hw.DGPU_BUSY_INTERVAL)

    def test_returns_last_value_between_queries(self):
        self.at(0)
        self.assertEqual(self.at(hw.DGPU_WAKE_GRACE), (60.0, "active"))
        self.answer = (99.0, 0.0)
        self.assertEqual(self.at(hw.DGPU_WAKE_GRACE + 1), (60.0, "active"))

    def test_sleep_resets_everything(self):
        self.at(0)
        self.at(hw.DGPU_WAKE_GRACE)
        self.at(hw.DGPU_WAKE_GRACE + 1, "suspended")
        # woke again: a fresh grace period, no stale temperature
        self.assertEqual(self.at(hw.DGPU_WAKE_GRACE + 2), (None, "waking"))

    def test_failed_query_gives_no_temperature(self):
        self.answer = None
        self.at(0)
        self.assertEqual(self.at(hw.DGPU_WAKE_GRACE), (None, "active"))


class RpmCacheTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.reads = 0
        self.h = hw.Hardware(root="/nonexistent", clock=self.clock)
        real = hw._read_int

        def counting(path):
            if path.endswith("_input") and "fan" in path:
                self.reads += 1
                return 2000
            return real(path)

        self._orig = hw._read_int
        hw._read_int = counting

    def tearDown(self):
        hw._read_int = self._orig

    def test_rpm_read_at_most_every_interval(self):
        for t in range(0, 60, 2):
            self.clock.now = 1000.0 + t
            self.assertEqual(self.h._fans("/fake"), (2000, 2000))
        self.assertEqual(self.reads, 2 * (60 // int(hw.RPM_INTERVAL)))

    def test_refresh_soon_after_change(self):
        self.h._fans("/fake")
        self.h.refresh_rpm_soon(delay=3)
        self.clock.now += 2
        self.h._fans("/fake")
        self.assertEqual(self.reads, 2)          # not yet
        self.clock.now += 1
        self.h._fans("/fake")
        self.assertEqual(self.reads, 4)          # 3 s later: re-read


if __name__ == "__main__":
    unittest.main()
