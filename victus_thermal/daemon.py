"""victus-thermald main loop.

Safety model:
  * Any exit path (SIGTERM, exception, crash) hands the fans back to the
    firmware curve. The systemd unit repeats that in ExecStopPost for the
    cases Python cannot handle (SIGKILL, OOM, watchdog kill).
  * The loop pings the systemd watchdog every tick; a hung daemon is killed
    and restarted by systemd, which also restores firmware control first.
  * A failing write is logged and retried next tick; it never kills the loop.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import socket
import sys
import tempfile
import time

from . import __version__, config
from .hw import PWM_AUTO, PWM_MANUAL, PWM_MAX, Hardware
from .policy import AUTO, MAX, MODES, Decision, Policy

# RUNTIME_DIR is root-only; CONTROL_DIR is writable by the victus-thermal
# group and holds nothing but the mode request, which the daemon only reads.
RUNTIME_DIR = "/run/victus-thermal"
CONTROL_DIR = os.path.join(RUNTIME_DIR, "control")
STATUS_PATH = os.path.join(RUNTIME_DIR, "status.json")
MODE_PATH = os.path.join(CONTROL_DIR, "mode")
# Persistent user preferences (survive reboots). Same trust model as
# CONTROL_DIR: written by the victus-thermal group, only read by the daemon.
STATE_DIR = "/var/lib/victus-thermal"
AC_QUIET_PATH = os.path.join(STATE_DIR, "ac-quiet")

# Skip pwm writes smaller than this: each one is a WMI round-trip and the
# difference is inaudible.
PWM_WRITE_THRESHOLD = 3

# How often, between control steps, to look for a new mode or profile.
WAKE_CHECK_INTERVAL = 0.2

log = logging.getLogger("victus-thermald")


def sd_notify(message: str) -> None:
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.sendto(message.encode(), addr)
    except OSError:
        pass


def atomic_write(path: str, data: str, mode: int = 0o644) -> None:
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".status-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def read_keyword(path: str) -> str | None:
    """Untrusted input (group-writable): never follow links, read a few
    bytes. Callers accept only known keywords."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        return os.read(fd, 16).decode("ascii", "replace").strip()
    except OSError:
        return None
    finally:
        os.close(fd)


def read_mode(path: str = MODE_PATH) -> str:
    value = read_keyword(path)
    return value if value in MODES else "curve"


def read_ac_quiet(path: str = AC_QUIET_PATH) -> bool | None:
    """The saved preference, or None when unset (use the config default)."""
    return {"on": True, "off": False}.get(read_keyword(path) or "")


def _stamp(path: str) -> tuple | None:
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return st.st_ino, st.st_mtime_ns


class Daemon:
    def __init__(self, hw: Hardware, config_path: str = config.CONFIG_PATH,
                 runtime_dir: str = RUNTIME_DIR, state_dir: str = STATE_DIR):
        self.hw = hw
        self.config_path = config_path
        self.status_path = os.path.join(runtime_dir, "status.json")
        self.mode_path = os.path.join(runtime_dir, "control", "mode")
        self.ac_quiet_path = os.path.join(state_dir, "ac-quiet")
        self.running = True
        self._config_mtime: float | None = None
        cfg = self._load_config(force=True)
        self.policy = Policy(cfg)
        self._last_pwm: int | None = None
        # Firmware fan speed when the curve took over from AUTO; manual pwm
        # never drops below it while the curve stays in control.
        self._handover_floor = 0
        self._last_error: str | None = None
        self._last_log_key: tuple | None = None

    # -- config --

    def _load_config(self, force: bool = False) -> config.Config | None:
        try:
            mtime = os.stat(self.config_path).st_mtime
        except OSError:
            mtime = None
        if not force and mtime == self._config_mtime:
            return None
        self._config_mtime = mtime
        cfg, error = config.load(self.config_path)
        if error:
            log.error("%s", error)
        elif not force:
            log.info("configuration reloaded")
        return cfg

    # -- hardware application --

    def apply(self, d: Decision, current_mode: int | None) -> int | None:
        """Drive the hardware towards the decision. Returns the pwm written
        (or held) in manual mode, else None."""
        if d.kind in (AUTO, MAX):
            target = PWM_AUTO if d.kind == AUTO else PWM_MAX
            if current_mode != target:
                self.hw.set_mode(target)
            self._last_pwm = None
            self._handover_floor = 0
            return None

        switched = current_mode != PWM_MANUAL
        if switched:
            # Taking over from the firmware: never start slower than it runs.
            self._handover_floor = (
                (self.hw.read_pwm() or 0) if current_mode == PWM_AUTO else 0)
            self.hw.set_mode(PWM_MANUAL)
        pwm = max(d.pwm, self._handover_floor)
        if (switched or self._last_pwm is None
                or abs(pwm - self._last_pwm) >= PWM_WRITE_THRESHOLD):
            self.hw.set_pwm(pwm)
            self._last_pwm = pwm
        return self._last_pwm

    # -- one iteration --

    def step(self) -> dict:
        new_cfg = self._load_config()
        if new_cfg is not None:
            self.policy.reconfigure(new_cfg)

        r = self.hw.read()
        manual = self.hw.has_manual_pwm()
        mode = read_mode(self.mode_path)
        saved = read_ac_quiet(self.ac_quiet_path)
        ac_quiet = self.policy.cfg.ac_quiet if saved is None else saved
        d = self.policy.decide(r, mode, manual, ac_quiet=ac_quiet)

        error = None
        applied = None
        if self.hw.hp_hwmon() is None:
            error = "hp-wmi fan interface not found"
        else:
            try:
                applied = self.apply(d, r.pwm_mode)
            except (OSError, ValueError) as exc:
                error = f"fan write failed: {exc}"
                self._last_pwm = None
        if error != self._last_error:
            if error:
                log.error("%s", error)
            elif self._last_error:
                log.info("fan control recovered")
            self._last_error = error
        # Log state changes, not every curve step.
        log_key = (d.kind, mode, r.profile, self.policy.state.emergency)
        if log_key != self._last_log_key:
            log.info("%s", d.reason)
            if self._last_log_key is not None:
                self.hw.refresh_rpm_soon()  # show the effect of the change
            self._last_log_key = log_key

        return {
            "ts": round(time.time(), 1),
            "version": __version__,
            "driver": "victus_s (manual pwm)" if manual else "stock (two-state)",
            "mode": mode,
            "profile": r.profile,
            "on_ac": r.on_ac,
            "temps": {"cpu": r.cpu, "igpu": r.igpu, "dgpu": r.dgpu, "dgpu_state": r.dgpu_state},
            "fans_rpm": list(r.rpm),
            "decision": {"kind": d.kind, "pwm": d.pwm, "duty": d.duty,
                         "curve_temp": d.curve_temp, "reason": d.reason,
                         "applied_pwm": applied, "hold_remaining": d.hold_remaining},
            "emergency": self.policy.state.emergency,
            "ac_quiet": {"enabled": ac_quiet,
                         "active": ac_quiet and r.on_ac is True and manual},
            "error": error,
        }

    def wake_key(self) -> tuple:
        """Changes when the user asks for something: a new fan mode request,
        AC quiet preference or platform profile. Checked between steps so
        those take effect within a fraction of a second instead of a full
        poll interval."""
        return _stamp(self.mode_path), _stamp(self.ac_quiet_path), self.hw.profile()

    # -- lifecycle --

    def stop(self, signum, _frame) -> None:
        log.info("signal %d received, stopping", signum)
        self.running = False

    def run(self) -> int:
        log.info("victus-thermald %s starting", __version__)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.mode_path)  # a forced mode never survives a restart
        sd_notify("READY=1")
        try:
            while self.running:
                start = time.monotonic()
                try:
                    status = self.step()
                    atomic_write(self.status_path, json.dumps(status) + "\n")
                    sd_notify(f"WATCHDOG=1\nSTATUS={status['decision']['reason']}")
                except Exception:  # noqa: BLE001 - the loop must survive anything
                    log.exception("unexpected error in control loop")
                    self.hw.restore_firmware_control()
                    sd_notify("WATCHDOG=1")
                interval = self.policy.cfg.poll_interval
                key = self.wake_key()
                while self.running and time.monotonic() - start < interval:
                    time.sleep(WAKE_CHECK_INTERVAL)
                    if self.wake_key() != key:
                        break
        finally:
            sd_notify("STOPPING=1")
            if self.hw.restore_firmware_control():
                log.info("fans handed back to firmware control")
            else:
                log.error("could not restore firmware fan control")
        return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s",
                        stream=sys.stderr)
    if os.geteuid() != 0:
        log.error("must run as root")
        return 1
    daemon = Daemon(Hardware())
    signal.signal(signal.SIGTERM, daemon.stop)
    signal.signal(signal.SIGINT, daemon.stop)
    return daemon.run()
