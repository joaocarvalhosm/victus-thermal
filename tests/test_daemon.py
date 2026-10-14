"""Runs the real Hardware + Daemon code against a fake sysfs tree."""

import json
import os
import shutil
import stat
import tempfile
import unittest

from victus_thermal.daemon import Daemon
from victus_thermal.hw import Hardware

HP = "sys/devices/platform/hp-wmi/hwmon/hwmon7"


class FakeSysfs:
    def __init__(self, manual_pwm=True):
        self.root = tempfile.mkdtemp(prefix="victus-sysfs-")
        self.files = {
            "sys/class/hwmon/hwmon6/name": "k10temp",
            "sys/class/hwmon/hwmon6/temp1_input": "50000",
            "sys/class/hwmon/hwmon10/name": "amdgpu",
            "sys/class/hwmon/hwmon10/temp1_input": "45000",
            f"{HP}/pwm1_enable": "2",
            f"{HP}/fan1_input": "0",
            f"{HP}/fan2_input": "0",
            "sys/firmware/acpi/platform_profile": "balanced",
            "sys/class/power_supply/ACAD/type": "Mains",
            "sys/class/power_supply/ACAD/online": "1",
            "sys/class/power_supply/BAT0/type": "Battery",
            "sys/bus/pci/devices/0000:01:00.0/vendor": "0x10de",
            "sys/bus/pci/devices/0000:01:00.0/class": "0x030000",
            "sys/bus/pci/devices/0000:01:00.0/power/runtime_status": "suspended",
        }
        if manual_pwm:
            self.files[f"{HP}/pwm1"] = "0"
        for rel, value in self.files.items():
            self.set(rel, value)
        self.run_dir = os.path.join(self.root, "run")
        self.mode_file = os.path.join(self.run_dir, "control", "mode")
        os.makedirs(os.path.dirname(self.mode_file))
        self.config = os.path.join(self.root, "config.toml")

    def set(self, rel, value):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(str(value) + "\n")

    def get(self, rel):
        with open(os.path.join(self.root, rel)) as f:
            return f.read().strip()

    def cleanup(self):
        shutil.rmtree(self.root)


class DaemonTest(unittest.TestCase):
    def make(self, manual_pwm=True, config_text=None):
        fs = FakeSysfs(manual_pwm)
        self.addCleanup(fs.cleanup)
        if config_text is not None:
            with open(fs.config, "w") as f:
                f.write(config_text)
        daemon = Daemon(Hardware(root=fs.root, nvidia_smi="/nonexistent/nvidia-smi"),
                        config_path=fs.config, runtime_dir=fs.run_dir)
        return fs, daemon

    def test_cool_machine_stays_on_firmware(self):
        fs, d = self.make()
        status = d.step()
        self.assertEqual(fs.get(f"{HP}/pwm1_enable"), "2")
        self.assertEqual(status["decision"]["kind"], "auto")
        self.assertEqual(status["temps"]["dgpu_state"], "suspended")
        self.assertTrue(status["on_ac"])

    def test_warm_machine_gets_manual_pwm(self):
        fs, d = self.make()
        fs.set("sys/class/hwmon/hwmon6/temp1_input", "75000")
        status = d.step()
        self.assertEqual(fs.get(f"{HP}/pwm1_enable"), "1")
        self.assertEqual(status["decision"]["kind"], "manual")
        self.assertEqual(fs.get(f"{HP}/pwm1"), str(status["decision"]["pwm"]))

    def test_small_pwm_changes_are_not_written(self):
        fs, d = self.make(config_text="smoothing = 1.0\nhysteresis = 0\n")
        fs.set("sys/class/hwmon/hwmon6/temp1_input", "75000")
        d.step()
        first = fs.get(f"{HP}/pwm1")
        fs.set("sys/class/hwmon/hwmon6/temp1_input", "75300")
        d.step()
        self.assertEqual(fs.get(f"{HP}/pwm1"), first)

    def test_emergency_is_max(self):
        fs, d = self.make()
        fs.set("sys/class/hwmon/hwmon6/temp1_input", "97000")
        self.assertEqual(d.step()["decision"]["kind"], "max")
        self.assertEqual(fs.get(f"{HP}/pwm1_enable"), "0")

    def test_two_state_fallback_on_stock_driver(self):
        fs, d = self.make(manual_pwm=False, config_text="smoothing = 1.0\n")
        fs.set("sys/class/hwmon/hwmon6/temp1_input", "86000")
        status = d.step()
        self.assertIn("two-state", status["driver"])
        self.assertEqual(fs.get(f"{HP}/pwm1_enable"), "0")

    def test_mode_override_file(self):
        fs, d = self.make()
        with open(fs.mode_file, "w") as f:
            f.write("max\n")
        self.assertEqual(d.step()["mode"], "max")
        self.assertEqual(fs.get(f"{HP}/pwm1_enable"), "0")

    def test_garbage_mode_file_means_curve(self):
        fs, d = self.make()
        with open(fs.mode_file, "w") as f:
            f.write("rm -rf /\n")
        self.assertEqual(d.step()["mode"], "curve")

    def test_mode_file_symlink_is_not_followed(self):
        fs, d = self.make()
        target = os.path.join(fs.root, "elsewhere")
        with open(target, "w") as f:
            f.write("max\n")
        os.symlink(target, fs.mode_file)
        self.assertEqual(d.step()["mode"], "curve")

    def test_forced_mode_is_cleared_on_start(self):
        fs, d = self.make()
        with open(fs.mode_file, "w") as f:
            f.write("max\n")
        d.step = lambda: setattr(d, "running", False) or {"decision": {"reason": "x"}}
        d.run()
        self.assertFalse(os.path.exists(fs.mode_file))

    def test_invalid_config_uses_defaults_and_keeps_running(self):
        with self.assertLogs("victus-thermald", level="ERROR") as logs:
            fs, d = self.make(config_text="emergency_temp = 300\n")
        self.assertIn("emergency_temp", logs.output[0])
        self.assertEqual(d.policy.cfg.emergency_temp, 95.0)
        d.step()

    def test_missing_fan_interface_is_reported_not_fatal(self):
        fs, d = self.make()
        shutil.rmtree(os.path.join(fs.root, "sys/devices/platform/hp-wmi"))
        with self.assertLogs("victus-thermald", level="ERROR"):
            status = d.step()
        self.assertIn("not found", status["error"])

    def test_write_failure_is_reported_not_fatal(self):
        fs, d = self.make()
        fs.set("sys/class/hwmon/hwmon6/temp1_input", "75000")
        os.chmod(os.path.join(fs.root, HP, "pwm1_enable"), stat.S_IRUSR)
        if os.access(os.path.join(fs.root, HP, "pwm1_enable"), os.W_OK):
            self.skipTest("running as root: permission bits are not enforced")
        with self.assertLogs("victus-thermald", level="ERROR"):
            status = d.step()
        self.assertIn("write failed", status["error"])

    def test_run_restores_firmware_control_on_exit(self):
        fs, d = self.make()
        fs.set("sys/class/hwmon/hwmon6/temp1_input", "97000")
        calls = {"n": 0}
        real_step = d.step

        def step_then_stop():
            calls["n"] += 1
            result = real_step()
            d.running = False
            return result

        d.step = step_then_stop
        d.run()
        self.assertEqual(calls["n"], 1)
        self.assertEqual(fs.get(f"{HP}/pwm1_enable"), "2")
        with open(os.path.join(fs.run_dir, "status.json")) as f:
            self.assertEqual(json.load(f)["decision"]["kind"], "max")

    def test_run_survives_unexpected_exception(self):
        fs, d = self.make()
        fs.set(f"{HP}/pwm1_enable", "0")

        def boom():
            d.running = False
            raise RuntimeError("simulated bug")

        d.step = boom
        with self.assertLogs("victus-thermald", level="ERROR"):
            d.run()
        self.assertEqual(fs.get(f"{HP}/pwm1_enable"), "2")

    def test_hwmon_renumbering_is_followed(self):
        fs, d = self.make()
        d.step()
        new = "sys/devices/platform/hp-wmi/hwmon/hwmon9"
        os.rename(os.path.join(fs.root, HP), os.path.join(fs.root, new))
        fs.set("sys/class/hwmon/hwmon6/temp1_input", "97000")
        d.step()
        self.assertEqual(fs.get(f"{new}/pwm1_enable"), "0")


if __name__ == "__main__":
    unittest.main()
