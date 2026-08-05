"""
The signal contract.

Every signal emits the same five things: the raw value it observed, that value
normalised to 0-100, its weight, its contribution to the score, and one sentence
of plain English saying why. A signal that could not be computed says so and is
dropped from the weighted denominator rather than scoring zero, because a zero
is a false decline and a false decline is worse than an abstention.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..policy import SIGNAL_SPEC

OK = "ok"
UNAVAILABLE = "unavailable"


@dataclass
class Signal:
    key: str
    raw: Any
    normalized: float | None
    reason: str
    codes: list[str] = field(default_factory=list)
    status: str = OK
    detail: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.key not in SIGNAL_SPEC:
            raise KeyError(f"{self.key} is not declared in the policy")

    @property
    def category(self) -> str:
        return SIGNAL_SPEC[self.key][0]

    @property
    def weight(self) -> int:
        return SIGNAL_SPEC[self.key][1]

    @property
    def label(self) -> str:
        return SIGNAL_SPEC[self.key][2]

    @property
    def contribution(self) -> float:
        if self.status != OK or self.normalized is None:
            return 0.0
        return round(self.normalized / 100.0 * self.weight, 3)

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "category": self.category,
            "raw": self.raw,
            "normalized": self.normalized,
            "weight": self.weight,
            "contribution": self.contribution,
            "reason": self.reason,
            "codes": self.codes,
            "status": self.status,
            "detail": self.detail,
        }


_RESERVED = {"key", "raw", "normalized", "reason", "codes", "status", "detail"}


def ok(key: str, raw: Any, normalized: float, reason: str,
       codes: list[str] | None = None, **detail) -> Signal:
    # A detail kwarg that shadows a field name would raise TypeError here and be
    # swallowed upstream as "unavailable", silently deleting a signal from the
    # policy. Fail loudly in tests instead.
    assert not (_RESERVED & set(detail)), f"{key}: detail keys shadow the signal contract"
    return Signal(
        key=key,
        raw=raw,
        normalized=max(0.0, min(100.0, float(normalized))),
        reason=reason,
        codes=codes or [],
        status=OK,
        detail=detail,
    )


def unavailable(key: str, reason: str, raw: Any = None, **detail) -> Signal:
    return Signal(
        key=key,
        raw=raw,
        normalized=None,
        reason=reason,
        codes=[],
        status=UNAVAILABLE,
        detail=detail,
    )


def band(value: float, thresholds: list[tuple[float, float]], default: float) -> float:
    """
    Map a raw value through ascending (threshold, score) pairs.
    Returns the score of the first threshold the value falls under.
    """
    for limit, score in thresholds:
        if value < limit:
            return score
    return default
