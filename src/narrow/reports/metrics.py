"""Metric display semantics, independent of chart type and rendering backend."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from math import isfinite
from statistics import median

def calculate_median(values: Sequence[float]) -> float:
    """Read a complete finite sample population, including signed observations."""

    if not values or len(values) > 64:
        raise ValueError("median requires 1..64 finite observations")

    for value in values:

        try:
            valid = type(value) in (int, float) and isfinite(value)

        except OverflowError:
            valid = False

        if not valid:
            raise ValueError("median requires finite numeric observations, not bool")

    result = float(median(values))

    if not isfinite(result):
        ordered = sorted(values)
        middle = len(ordered) // 2
        result = ordered[middle - 1] / 2 + ordered[middle] / 2

    return result

@dataclass(frozen=True)
class MetricSpec:
    """Describe a metric label and its improvement direction; None means neutral."""

    label: str
    higher_is_better: bool | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:

        if not isinstance(self.label, str) or not self.label.strip():
            raise ValueError("Metric label must be a nonempty string")

        if self.higher_is_better is not None and type(self.higher_is_better) is not bool:
            raise ValueError("higher_is_better must be bool or None")

    def format_label(self) -> str:
        """Return plain text with an explicit direction, without reversing an axis."""

        if self.higher_is_better is None:
            return self.label

        direction = "↑ higher is better" if self.higher_is_better else "↓ lower is better"
        return f"{self.label} — {direction}"
