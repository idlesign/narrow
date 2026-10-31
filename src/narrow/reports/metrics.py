"""Metric display semantics, independent of chart type and rendering backend."""

from dataclasses import dataclass, field


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
