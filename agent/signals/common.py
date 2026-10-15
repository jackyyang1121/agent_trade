from __future__ import annotations

import math
from datetime import datetime, time
from zoneinfo import ZoneInfo

TAIPEI = ZoneInfo("Asia/Taipei")


def timestamp(value: str, *, session_close: bool = False) -> datetime:
    """Date-only bars become Taiwan closing time; cutoffs become end of day.

    Naive full timestamps are rejected: their availability cannot be compared
    safely across providers. No wall-clock default is used in decision logic.
    """
    if not value:
        raise ValueError("signal requires an explicit as-of timestamp")
    raw = str(value)
    if len(raw) == 10:
        day = datetime.strptime(raw, "%Y-%m-%d").date()
        return datetime.combine(day, time(13, 30) if session_close else time.max, TAIPEI)
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("signal timestamp must contain a timezone")
    return parsed


def finite(value, default=None):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def grade(value, default: float = 0.0) -> float:
    labels = {"none": 0.0, "low": 0.25, "weak": 0.25, "medium": 0.5,
              "moderate": 0.5, "high": 0.75, "strong": 0.75, "very_high": 1.0}
    number = finite(value)
    return max(0.0, min(1.0, number if number is not None else labels.get(str(value).lower(), default)))
