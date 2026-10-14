"""Fan curve interpolation and hysteresis. Pure logic, no I/O."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class FanCurve:
    """Piecewise-linear map from temperature (degC) to fan duty (0-100 %).

    Below the first point the duty is the first point's duty, above the last
    point it is the last point's duty.
    """

    points: tuple[tuple[float, float], ...]

    def __post_init__(self) -> None:
        if len(self.points) < 2:
            raise ValueError("a fan curve needs at least two points")
        prev_t, prev_d = None, None
        for t, d in self.points:
            if not 0 <= d <= 100:
                raise ValueError(f"duty {d} outside 0-100 %")
            if not 0 <= t <= 110:
                raise ValueError(f"temperature {t} outside 0-110 degC")
            if prev_t is not None:
                if t <= prev_t:
                    raise ValueError("curve temperatures must be strictly increasing")
                if d < prev_d:
                    raise ValueError("curve duty must never decrease as temperature rises")
            prev_t, prev_d = t, d

    @classmethod
    def from_pairs(cls, pairs) -> FanCurve:
        try:
            pts = tuple((float(t), float(d)) for t, d in pairs)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"curve points must be [temperature, duty] pairs: {exc}") from exc
        return cls(pts)

    def duty(self, temp: float) -> float:
        first_t, first_d = self.points[0]
        if temp <= first_t:
            return first_d
        for (t0, d0), (t1, d1) in zip(self.points, self.points[1:], strict=False):
            if temp <= t1:
                return d0 + (d1 - d0) * (temp - t0) / (t1 - t0)
        return self.points[-1][1]


@dataclass
class Hysteresis:
    """Follows rising temperature immediately, falling temperature lagged.

    The held value only drops once the input is more than `band` degrees below
    it, which stops the fans from pulsing when the temperature hovers around a
    curve point.
    """

    band: float
    held: float | None = field(default=None)

    def update(self, temp: float) -> float:
        if self.held is None or temp >= self.held:
            self.held = temp
        elif self.held - temp > self.band:
            self.held = temp + self.band
        return self.held

    def reset(self) -> None:
        self.held = None


@dataclass
class Ema:
    """Exponential moving average. Tctl on Ryzen jumps several degrees for
    sub-second bursts; smoothing keeps the fans from chasing those spikes."""

    alpha: float
    value: float | None = field(default=None)

    def update(self, sample: float) -> float:
        if self.value is None:
            self.value = sample
        else:
            self.value += self.alpha * (sample - self.value)
        return self.value

    def reset(self) -> None:
        self.value = None


def duty_to_pwm(duty: float) -> int:
    """0-100 % -> hwmon pwm 0-255."""
    return max(0, min(255, round(duty * 255 / 100)))
