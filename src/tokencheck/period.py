"""Reporting windows.

Every command that reports over a span of time takes the same `--period`:
`1h`, `1d` (the default) or `30d`. Arbitrary `Nh`/`Nd` values are accepted too,
so `--period 6h` or `--period 14d` work without needing a new named choice.

A period is a *trailing* window — `now - P` through `now` — which is what "last
30 days" means colloquially. It is deliberately not calendar-aligned; use the
per-day breakdowns if you want to see day boundaries.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

#: Offered in `--help` and tab completion. Any `Nh`/`Nd` also parses.
CHOICES = ("1h", "1d", "30d")
DEFAULT = "1d"

_PATTERN = re.compile(r"^\s*(\d+)\s*([hd])\s*$", re.IGNORECASE)

MAX_DAYS = 31
_HOUR = 3600
_DAY = 86400


class PeriodError(ValueError):
    """The `--period` value could not be understood."""


@dataclass(frozen=True)
class Period:
    """A trailing reporting window."""

    label: str
    seconds: int

    @property
    def is_hourly(self) -> bool:
        """Short enough that hour-resolution buckets are the useful view."""
        return self.seconds <= _DAY

    @property
    def bucket_width(self) -> str:
        """Bucket size for token-usage reports (Anthropic and OpenAI agree)."""
        return "1h" if self.is_hourly else "1d"

    @property
    def days(self) -> int:
        """Whole days spanned, rounded up — never less than 1."""
        return max(1, -(-self.seconds // _DAY))

    @property
    def hours(self) -> int:
        return max(1, -(-self.seconds // _HOUR))

    def start(self, now: dt.datetime | None = None) -> dt.datetime:
        return self.end(now) - dt.timedelta(seconds=self.seconds)

    def end(self, now: dt.datetime | None = None) -> dt.datetime:
        return (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)

    def range(self, now: dt.datetime | None = None) -> tuple[dt.datetime, dt.datetime]:
        end = self.end(now)
        return end - dt.timedelta(seconds=self.seconds), end

    def bucket_limit(self) -> int:
        """How many buckets the window needs, for APIs that want an explicit limit."""
        return self.hours if self.is_hourly else self.days

    def describe(self) -> str:
        """Human phrasing for report headers."""
        if self.seconds == _HOUR:
            return "last hour"
        if self.seconds == _DAY:
            return "last 24 hours"
        if self.seconds % _DAY == 0:
            return f"last {self.seconds // _DAY} days"
        return f"last {self.hours} hours"


def parse(value: str | None) -> Period:
    """Parse a `--period` value. Empty/None yields the default."""
    raw = (value or DEFAULT).strip()
    match = _PATTERN.match(raw)
    if not match:
        raise PeriodError(
            f"Cannot parse period {value!r}. "
            f"Use one of {', '.join(CHOICES)}, or an explicit span like `6h` or `14d`."
        )

    amount = int(match.group(1))
    unit = match.group(2).lower()
    if amount <= 0:
        raise PeriodError(f"Period must be positive, got {value!r}.")

    seconds = amount * (_HOUR if unit == "h" else _DAY)
    if seconds > MAX_DAYS * _DAY:
        raise PeriodError(
            f"Period {value!r} is longer than the {MAX_DAYS}-day maximum the usage APIs retain."
        )

    return Period(label=f"{amount}{unit}", seconds=seconds)


def from_args(period: str | None, days: int | None) -> Period:
    """Resolve `--period`, honouring the older `--days N` alias when given."""
    if period:
        return parse(period)
    if days and days > 0:
        return parse(f"{days}d")
    return parse(DEFAULT)
