"""sysfs access: sensors, power state and the hp-wmi fan interface.

Every path is resolved under `root` so the test-suite can run the real code
against a fake sysfs tree. Nothing here raises on a missing or unreadable
file; readers return None and the policy decides what that means.
"""

from __future__ import annotations

import glob
import os
import subprocess
from dataclasses import dataclass

# hp-wmi pwm1_enable values (drivers/platform/x86/hp/hp-wmi.c)
PWM_MAX = 0
PWM_MANUAL = 1
PWM_AUTO = 2

NVIDIA_VENDOR = "0x10de"
NVIDIA_SMI_TIMEOUT = 2.0


@dataclass(frozen=True)
class Readings:
    cpu: float | None
    igpu: float | None
    dgpu: float | None
    dgpu_state: str
    profile: str | None
    on_ac: bool | None
    rpm: tuple[int | None, int | None]
    pwm_mode: int | None


def _read(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _read_int(path: str) -> int | None:
    value = _read(path)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _milli(value: int | None) -> float | None:
    return None if value is None else round(value / 1000, 1)


class Hardware:
    def __init__(self, root: str = "/", nvidia_smi: str = "nvidia-smi"):
        self.root = root
        self.nvidia_smi = nvidia_smi
        self._hp: str | None = None
        self._cpu: str | None = None
        self._igpu: str | None = None
        self._nvidia: str | None = None

    def _p(self, *parts: str) -> str:
        return os.path.join(self.root, *parts)

    # -- discovery (hwmon numbers change across boots and module reloads) --

    def _hwmon_named(self, name: str) -> str | None:
        for d in sorted(glob.glob(self._p("sys/class/hwmon/hwmon*"))):
            if _read(os.path.join(d, "name")) == name:
                return d
        return None

    def _valid(self, path: str | None, leaf: str) -> bool:
        return path is not None and os.path.exists(os.path.join(path, leaf))

    def hp_hwmon(self) -> str | None:
        if not self._valid(self._hp, "pwm1_enable"):
            self._hp = next(
                (d for d in sorted(glob.glob(self._p("sys/devices/platform/hp-wmi/hwmon/hwmon*")))
                 if os.path.exists(os.path.join(d, "pwm1_enable"))),
                None,
            )
        return self._hp

    def _cpu_hwmon(self) -> str | None:
        if not self._valid(self._cpu, "temp1_input"):
            self._cpu = self._hwmon_named("k10temp")
        return self._cpu

    def _igpu_hwmon(self) -> str | None:
        if not self._valid(self._igpu, "temp1_input"):
            self._igpu = self._hwmon_named("amdgpu")
        return self._igpu

    def _nvidia_dev(self) -> str | None:
        if self._nvidia is None or not os.path.exists(self._nvidia):
            self._nvidia = None
            for d in sorted(glob.glob(self._p("sys/bus/pci/devices/*"))):
                if (_read(os.path.join(d, "vendor")) == NVIDIA_VENDOR
                        and (_read(os.path.join(d, "class")) or "").startswith("0x03")):
                    self._nvidia = d
                    break
        return self._nvidia

    # -- capabilities --

    def has_manual_pwm(self) -> bool:
        """True with the patched driver (Victus S path exposes pwm1)."""
        hp = self.hp_hwmon()
        return hp is not None and os.path.exists(os.path.join(hp, "pwm1"))

    # -- readers --

    def _dgpu(self) -> tuple[float | None, str]:
        """Read the NVIDIA temperature without waking a runtime-suspended
        dGPU: nvidia-smi is only run when the device is already active."""
        dev = self._nvidia_dev()
        if dev is None:
            return None, "absent"
        status = _read(os.path.join(dev, "power", "runtime_status"))
        if status != "active":
            return None, "suspended"
        try:
            out = subprocess.run(
                [self.nvidia_smi, "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=NVIDIA_SMI_TIMEOUT, check=False,
            )
            return float(out.stdout.split()[0]), "active"
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            return None, "active"

    def _on_ac(self) -> bool | None:
        states = [
            _read_int(os.path.join(d, "online"))
            for d in glob.glob(self._p("sys/class/power_supply/*"))
            if _read(os.path.join(d, "type")) == "Mains"
        ]
        states = [s for s in states if s is not None]
        return any(states) if states else None

    def read(self) -> Readings:
        cpu = self._cpu_hwmon()
        igpu = self._igpu_hwmon()
        hp = self.hp_hwmon()
        dgpu, dgpu_state = self._dgpu()
        return Readings(
            cpu=_milli(_read_int(os.path.join(cpu, "temp1_input"))) if cpu else None,
            igpu=_milli(_read_int(os.path.join(igpu, "temp1_input"))) if igpu else None,
            dgpu=dgpu,
            dgpu_state=dgpu_state,
            profile=self.profile(),
            on_ac=self._on_ac(),
            rpm=(
                _read_int(os.path.join(hp, "fan1_input")) if hp else None,
                _read_int(os.path.join(hp, "fan2_input")) if hp else None,
            ),
            pwm_mode=_read_int(os.path.join(hp, "pwm1_enable")) if hp else None,
        )

    def profile(self) -> str | None:
        return _read(self._p("sys/firmware/acpi/platform_profile"))

    def read_pwm(self) -> int | None:
        """Current fan speed as pwm (0-255). The patched driver derives it from
        the measured RPM, so in AUTO it reports what the firmware is doing."""
        hp = self.hp_hwmon()
        return _read_int(os.path.join(hp, "pwm1")) if hp else None

    # -- writers --

    def _write(self, leaf: str, value: int) -> None:
        hp = self.hp_hwmon()
        if hp is None:
            raise OSError("hp-wmi hwmon interface not found")
        with open(os.path.join(hp, leaf), "w") as f:
            f.write(str(value))

    def set_mode(self, mode: int) -> None:
        if mode not in (PWM_MAX, PWM_MANUAL, PWM_AUTO):
            raise ValueError(f"invalid pwm mode {mode}")
        self._write("pwm1_enable", mode)

    def set_pwm(self, pwm: int) -> None:
        """Only valid in manual mode; the driver clamps to the fan table's
        min/max RPM."""
        if not 0 <= pwm <= 255:
            raise ValueError(f"invalid pwm {pwm}")
        self._write("pwm1", pwm)

    def restore_firmware_control(self) -> bool:
        """Best effort hand-back to the firmware curve. Never raises."""
        try:
            self.set_mode(PWM_AUTO)
            return True
        except (OSError, ValueError):
            return False
