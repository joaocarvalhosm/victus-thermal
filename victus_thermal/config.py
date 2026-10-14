"""Configuration loading and validation.

The daemon must never crash or run unsafe settings because of a bad config
file: every field is validated, and an invalid file falls back to the
built-in defaults as a whole (with the reason logged), never field by field.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field

from .curve import FanCurve

CONFIG_PATH = "/etc/victus-thermal/config.toml"

PROFILES = ("low-power", "balanced", "performance")

# "firmware" in place of a curve leaves that profile entirely to the HP
# firmware fan curve (only the emergency override still applies).
FIRMWARE = "firmware"

DEFAULT_CURVES = {
    "low-power": ((60, 0), (70, 30), (80, 55), (90, 80), (95, 100)),
    "balanced": ((55, 0), (60, 30), (70, 45), (80, 70), (88, 100)),
    "performance": ((45, 35), (60, 50), (70, 70), (80, 90), (85, 100)),
}


@dataclass(frozen=True)
class Config:
    poll_interval: float = 2.0
    emergency_temp: float = 95.0
    emergency_release: float = 85.0
    hysteresis: float = 4.0
    smoothing: float = 0.5
    # Two-state fallback (stock driver without manual pwm): force MAX when the
    # curve asks for at least this duty, release below it minus 15 %.
    twostate_max_duty: float = 70.0
    curves: dict[str, FanCurve | str] = field(
        default_factory=lambda: {p: FanCurve(pts) for p, pts in DEFAULT_CURVES.items()}
    )


def _number(raw: dict, key: str, default: float, lo: float, hi: float) -> float:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{key} must be a number")
    if not lo <= value <= hi:
        raise ValueError(f"{key}={value} outside {lo}-{hi}")
    return float(value)


def parse(raw: dict) -> Config:
    """Build a Config from a parsed TOML document. Raises ValueError."""
    known = {
        "poll_interval", "emergency_temp", "emergency_release", "hysteresis",
        "smoothing", "twostate_max_duty", "curves",
    }
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"unknown settings: {', '.join(sorted(unknown))}")

    d = Config()
    emergency_temp = _number(raw, "emergency_temp", d.emergency_temp, 70, 100)
    emergency_release = _number(raw, "emergency_release", d.emergency_release, 50, 100)
    if emergency_release >= emergency_temp:
        raise ValueError("emergency_release must be below emergency_temp")

    curves: dict[str, FanCurve | str] = dict(d.curves)
    raw_curves = raw.get("curves", {})
    if not isinstance(raw_curves, dict):
        raise ValueError("[curves] must be a table")
    for name, value in raw_curves.items():
        if name not in PROFILES:
            raise ValueError(f"unknown curve '{name}' (expected one of {', '.join(PROFILES)})")
        if value == FIRMWARE:
            curves[name] = FIRMWARE
        else:
            try:
                curves[name] = FanCurve.from_pairs(value)
            except ValueError as exc:
                raise ValueError(f"curve '{name}': {exc}") from exc

    return Config(
        poll_interval=_number(raw, "poll_interval", d.poll_interval, 0.5, 10),
        emergency_temp=emergency_temp,
        emergency_release=emergency_release,
        hysteresis=_number(raw, "hysteresis", d.hysteresis, 0, 15),
        smoothing=_number(raw, "smoothing", d.smoothing, 0.05, 1),
        twostate_max_duty=_number(raw, "twostate_max_duty", d.twostate_max_duty, 20, 100),
        curves=curves,
    )


def load(path: str = CONFIG_PATH) -> tuple[Config, str | None]:
    """Return (config, error). On any problem the defaults are returned
    together with a description of what was wrong."""
    try:
        with open(path, "rb") as f:
            return parse(tomllib.load(f)), None
    except FileNotFoundError:
        return Config(), None
    except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
        return Config(), f"{path}: {exc}; using built-in defaults"
