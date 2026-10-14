"""Fan decision logic. Pure: takes readings and state, returns a decision.

Order of precedence, highest first:
  1. Emergency: any sensor >= emergency_temp forces MAX until every sensor is
     back at or below emergency_release. Overrides everything, user too.
  2. User override ("auto" / "max") from victusctl.
  3. Missing CPU sensor: hand control to the firmware, never guess.
  4. The active platform profile's curve, fed by the hottest sensor
     (smoothed, then hysteresis on the way down).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .config import FIRMWARE, Config
from .curve import Ema, Hysteresis, duty_to_pwm
from .hw import Readings

AUTO, MAX, MANUAL = "auto", "max", "manual"
MODES = ("curve", AUTO, MAX)

TWOSTATE_RELEASE_GAP = 15.0


@dataclass(frozen=True)
class Decision:
    kind: str
    pwm: int | None
    reason: str
    duty: float | None = None
    curve_temp: float | None = None
    hold_remaining: float | None = None


@dataclass
class PolicyState:
    smoothing: Ema
    hysteresis: Hysteresis
    emergency: bool = False
    twostate_max: bool = False
    hold_until: float | None = None    # set while the curve is in control
    want_since: float | None = None    # curve has asked for fans since

    @classmethod
    def for_config(cls, cfg: Config) -> PolicyState:
        return cls(Ema(cfg.smoothing), Hysteresis(cfg.hysteresis))


@dataclass
class Policy:
    cfg: Config
    state: PolicyState = field(init=False)

    def __post_init__(self) -> None:
        self.state = PolicyState.for_config(self.cfg)

    def reconfigure(self, cfg: Config) -> None:
        if cfg != self.cfg:
            self.cfg = cfg
            emergency = self.state.emergency
            self.state = PolicyState.for_config(cfg)
            self.state.emergency = emergency

    def _sensors(self, r: Readings) -> dict[str, float]:
        return {k: v for k, v in (("cpu", r.cpu), ("igpu", r.igpu), ("dgpu", r.dgpu))
                if v is not None}

    def decide(self, r: Readings, mode: str, manual_supported: bool,
               now: float | None = None, ac_quiet: bool = False) -> Decision:
        """`ac_quiet`: the user's AC quiet preference; it only applies while
        r.on_ac is True and the driver supports manual pwm."""
        cfg, st = self.cfg, self.state
        now = time.monotonic() if now is None else now
        sensors = self._sensors(r)

        if sensors and max(sensors.values()) >= cfg.emergency_temp:
            st.emergency = True
        elif st.emergency and (not sensors or max(sensors.values()) <= cfg.emergency_release):
            st.emergency = False
        if st.emergency:
            hot = max(sensors, key=sensors.get) if sensors else "?"
            return Decision(MAX, None, f"emergency: {hot} {sensors.get(hot)}C")

        if mode in (AUTO, MAX):
            st.hold_until = st.want_since = None
            if mode == AUTO:
                return Decision(AUTO, None, "override: firmware control")
            return Decision(MAX, None, "override: max")

        if r.cpu is None:
            st.smoothing.reset()
            st.hysteresis.reset()
            st.hold_until = None
            return Decision(AUTO, None, "cpu sensor unavailable: firmware control")

        profile = r.profile if r.profile in cfg.curves else "balanced"
        curve = cfg.curves[profile]
        if curve == FIRMWARE:
            st.hold_until = None
            return Decision(AUTO, None, f"{profile}: firmware curve")

        smoothed = st.smoothing.update(max(sensors.values()))
        temp = st.hysteresis.update(smoothed)
        duty = curve.duty(temp)

        if not manual_supported:
            if duty >= cfg.twostate_max_duty:
                st.twostate_max = True
            elif duty < cfg.twostate_max_duty - TWOSTATE_RELEASE_GAP:
                st.twostate_max = False
            kind = MAX if st.twostate_max else AUTO
            return Decision(kind, None, f"{profile} (two-state): {duty:.0f}% at {temp:.1f}C",
                            duty, temp)

        if ac_quiet and r.on_ac is True:
            # On AC the fans never stop: at least ac_quiet_duty, the curve
            # above it. Already in manual control, so no engage delay.
            floor = cfg.ac_quiet_duty
            st.want_since = None
            if duty <= floor:
                st.hold_until = None
                return Decision(MANUAL, duty_to_pwm(floor),
                                f"{profile}: AC quiet {floor:.0f}% at {temp:.1f}C", floor, temp)
            st.hold_until = now + cfg.hold_time
            if duty >= 100:
                return Decision(MAX, None, f"{profile}: curve max at {temp:.1f}C", duty, temp)
            return Decision(MANUAL, duty_to_pwm(duty),
                            f"{profile}: {duty:.0f}% at {temp:.1f}C", duty, temp)

        if duty <= 0:
            st.want_since = None
            if st.hold_until is not None and now < st.hold_until:
                hold = curve.min_active_duty()
                remaining = st.hold_until - now
                return Decision(MANUAL, duty_to_pwm(hold),
                                f"{profile}: holding {hold:.0f}% for {remaining:.0f}s",
                                hold, temp, remaining)
            st.hold_until = None
            return Decision(AUTO, None, f"{profile}: below curve at {temp:.1f}C", duty, temp)
        if st.hold_until is None and duty < 100:
            # Not in control yet: only take over from the firmware for heat
            # that lasts, not for a spike. Judge that on the smoothed
            # temperature without hysteresis, which would otherwise hold the
            # peak of a spike and make it look sustained.
            if curve.duty(smoothed) <= 0:
                st.want_since = None
                return Decision(AUTO, None, f"{profile}: below curve at {smoothed:.1f}C",
                                0.0, smoothed)
            if st.want_since is None:
                st.want_since = now
            waited = now - st.want_since
            if waited < cfg.engage_delay:
                return Decision(AUTO, None,
                                f"{profile}: warming {duty:.0f}% at {temp:.1f}C "
                                f"({waited:.0f}/{cfg.engage_delay:.0f}s)", duty, temp)
        st.hold_until = now + cfg.hold_time
        if duty >= 100:
            return Decision(MAX, None, f"{profile}: curve max at {temp:.1f}C", duty, temp)
        return Decision(MANUAL, duty_to_pwm(duty), f"{profile}: {duty:.0f}% at {temp:.1f}C",
                        duty, temp)
