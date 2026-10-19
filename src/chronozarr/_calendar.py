"""Time axes on a CF calendar other than Gregorian, mapped onto the timestamps a store holds.

A store keeps time as int64 milliseconds on the proleptic Gregorian calendar (spec 2.2). Climate
models write 360_day, noleap (365_day), all_leap (366_day) and julian calendars, which xarray
decodes to cftime objects. There is one honest route: each step keeps its calendar date and time
of day (year, month, day, hour, ...) as a Gregorian timestamp. The elapsed time between steps is
not kept, because every month of a 360_day calendar has 30 days and a Gregorian month does not;
the mapping reports the change in numbers. A date that Gregorian lacks (Feb 30 of a 360_day
calendar, Feb 29 of a julian century year) stops the conversion unless the caller picks
`date-or-month-end`, which moves it to the last day of its month. Either way the store records the
calendar, the rule and the dates that moved in its provenance (spec 4.5).
"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy as np
import xarray as xr

from chronozarr._install import extra_hint
from chronozarr.store import redact_url

OPTION = "--calendar-to-gregorian"
RULE_DATE = "date"
RULE_MONTH_END = "date-or-month-end"
RULES = (RULE_DATE, RULE_MONTH_END)
# CF calendar names that xarray decodes to numpy datetimes without cftime.
GREGORIAN_NAMES = frozenset({"standard", "gregorian", "proleptic_gregorian"})
EXAMPLES = 3
_MONTH_DAYS = np.array([31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31], dtype=np.int64)
_US_PER_DAY = 86_400 * 1_000_000


class MissingCftimeError(ValueError):
    """xarray cannot decode a time coordinate because cftime is not installed."""


def check_rule(rule: str | None) -> None:
    if rule is not None and rule not in RULES:
        raise ValueError(
            f"calendar_to_gregorian must be one of {list(RULES)} or None, got {rule!r}"
        )


def explain_missing_cftime(exc: ValueError, reopen: Callable[[], xr.Dataset], where: str) -> None:
    """Raise `MissingCftimeError` when `exc` is xarray failing on a calendar that needs cftime.

    xarray's own message ("unable to decode time units ... installing cftime") names no chronozarr
    extra and dumps the variable. `reopen` opens the same source with `decode_times=False`, so the
    calendar can be read from the attributes. Returns when cftime is installed or no time variable
    has a calendar that needs it: the caller then keeps the error of xarray.
    """
    if importlib.util.find_spec("cftime") is not None:
        return
    try:
        raw = reopen()
    except (ValueError, KeyError, OSError):
        return
    for name, variable in raw.variables.items():
        calendar = variable.attrs.get("calendar")
        units = variable.attrs.get("units")
        if (
            isinstance(calendar, str)
            and isinstance(units, str)
            and " since " in units
            and calendar.lower() not in GREGORIAN_NAMES
        ):
            raise MissingCftimeError(
                f"{where}: the time coordinate {str(name)!r} uses the {calendar} calendar, which "
                f"needs cftime: {extra_hint('cftime')}"
            ) from exc


def is_cftime(values: np.ndarray) -> bool:
    """True for the object array xarray makes of dates that numpy datetimes cannot hold."""
    return (
        values.dtype == object
        and values.size > 0
        and type(values.flat[0]).__module__.split(".")[0] == "cftime"
    )


def calendar_name(time: xr.DataArray) -> str:
    """The calendar as the source wrote it (`365_day`, not cftime's `noleap`), else cftime's."""
    written = time.encoding.get("calendar")
    return str(written) if written else str(time.values.flat[0].calendar)


def _days_in_month(year: np.ndarray, month: np.ndarray) -> np.ndarray:
    leap = (year % 4 == 0) & ((year % 100 != 0) | (year % 400 == 0))
    return _MONTH_DAYS[month - 1] + ((month == 2) & leap)


def _span(seconds: np.ndarray) -> str:
    """The smallest to the largest gap, in the largest unit that keeps the smallest at 1+."""
    low, high = float(seconds.min()), float(seconds.max())
    name, size = next(
        (n, s)
        for n, s in (("day", 86400.0), ("hour", 3600.0), ("minute", 60.0), ("second", 1.0))
        if low >= s or s == 1.0
    )
    low, high = round(low / size, 6), round(high / size, 6)
    if low == high:
        return f"{low:g} {name}{'' if low == 1 else 's'}"
    return f"{low:g} to {high:g} {name}s"


def _text(value: str) -> str:
    """A date as `YYYY-MM-DD`, with the time of day only when it is not midnight."""
    return value.removesuffix("T00:00:00")


@dataclass(frozen=True)
class _Mapped:
    """Every step of `values` placed on a Gregorian date, a date past its month's end moved."""

    values: np.ndarray
    stored: np.ndarray  # datetime64[ms], in the order of `values`
    order: np.ndarray  # indices that sort `values` by their own calendar
    missing: tuple[int, ...]  # indices whose date Gregorian lacks
    texts: tuple[str, ...]  # the source dates, as written in its calendar

    @classmethod
    def of(cls, values: np.ndarray) -> _Mapped:
        year, month, day, hour, minute, second, micro = (
            np.array([getattr(v, name) for v in values.flat], dtype=np.int64)
            for name in ("year", "month", "day", "hour", "minute", "second", "microsecond")
        )
        last = _days_in_month(year, month)
        first = ((year - 1970) * 12 + (month - 1)).astype("datetime64[M]").astype("datetime64[D]")
        microseconds = (
            first.astype("datetime64[us]").astype(np.int64)
            + (np.minimum(day, last) - 1) * _US_PER_DAY
            + ((hour * 60 + minute) * 60 + second) * 1_000_000
            + micro
        )
        return cls(
            values=values.ravel(),
            stored=microseconds.astype("datetime64[us]").astype("datetime64[ms]"),
            order=np.lexsort((micro, second, minute, hour, day, month, year)),
            missing=tuple(int(i) for i in np.nonzero(day > last)[0]),
            texts=tuple(
                _text(f"{y:04d}-{m:02d}-{d:02d}T{h:02d}:{mi:02d}:{s:02d}")
                for y, m, d, h, mi, s in zip(year, month, day, hour, minute, second, strict=True)
            ),
        )

    def stored_text(self, i: int) -> str:
        return _text(str(np.datetime_as_string(self.stored[i], unit="s")))

    def missing_examples(self) -> str:
        shown = ", ".join(self.texts[i] for i in self.missing[:EXAMPLES])
        extra = len(self.missing) - EXAMPLES
        return f"{shown}, and {extra} more" if extra > 0 else shown

    def clashes(self) -> list[int]:
        """Positions in source order where a step lands on or before the one that precedes it."""
        ordered = self.stored[self.order].astype(np.int64)
        return [int(i) for i in np.nonzero(np.diff(ordered) <= 0)[0]]

    def spacing(self) -> str | None:
        """How the gaps between consecutive steps differ in the source's calendar and the store."""
        if len(self.order) < 2:
            return None
        ordered = self.values[self.order]
        source = np.array([(b - a).total_seconds() for a, b in pairwise(ordered)])
        in_store = np.diff(self.stored[self.order].astype(np.int64)) / 1000.0
        changed = int(np.count_nonzero(np.abs(source - in_store) > 0.001))
        return (
            f"steps {_span(source)} apart in the source are {_span(in_store)} apart in the store "
            f"({changed} of {len(source)} gaps change)"
        )


@dataclass(frozen=True)
class CalendarMapping:
    """What was done to a time axis: the source calendar, the rule, the dates that moved."""

    calendar: str
    rule: str
    stored: np.ndarray  # datetime64[ms], in the order of the source
    n_time: int
    moved: tuple[tuple[str, str], ...]  # (source date, stored date) of every date that moved
    spacing: str | None

    def describe(self) -> str:
        head = (
            f"{self.calendar} calendar, converted to Gregorian by keeping each step's date and "
            f"time of day ({OPTION} {self.rule})"
        )
        if self.spacing is not None:
            head += f"; {self.spacing}"
        if not self.moved:
            return f"{head}; every date exists in Gregorian, none moved"
        shown = ", ".join(f"{a} to {b}" for a, b in self.moved[:EXAMPLES])
        more = f", and {len(self.moved) - EXAMPLES} more" if len(self.moved) > EXAMPLES else ""
        return (
            f"{head}; {len(self.moved)} of {self.n_time} dates do not exist in Gregorian and "
            f"moved to the last day of their month ({shown}{more})"
        )

    def plan_line(self) -> str:
        return f"calendar:   {self.describe()}"

    def note(self) -> str:
        return f"Time axis: {self.describe()}."


def to_gregorian(
    values: np.ndarray,
    calendar: str,
    rule: str | None,
    *,
    name: str,
    can_choose: bool = True,
) -> CalendarMapping:
    """Gregorian timestamps for the cftime `values` of the time coordinate `name`, by `rule`.

    `rule` None refuses, with a message that names the choices and what they cost for these
    dates. `can_choose` False is for a command with no option to offer: the message then points
    at the command that has one.
    """
    check_rule(rule)
    where = f"the time coordinate {name!r}"
    mapped = _Mapped.of(values)
    n_time, missing = len(mapped.values), len(mapped.missing)
    if rule is None:
        raise ValueError(_refusal(where, calendar, mapped, can_choose))
    if rule == RULE_DATE and missing:
        raise ValueError(
            f"{where} ({calendar} calendar) has {missing} of {n_time} dates that Gregorian does "
            f"not have: {mapped.missing_examples()}. {OPTION} {RULE_MONTH_END} moves each to the "
            "last day of its month and records which in the store's provenance; nothing was "
            "written"
        )
    # Without a moved date the mapping keeps the order; equal source dates are the caller's check.
    clashes = mapped.clashes() if mapped.missing else []
    if clashes:
        shown = "; ".join(
            f"{mapped.texts[mapped.order[i]]} and {mapped.texts[mapped.order[i + 1]]} both land "
            f"on {mapped.stored_text(mapped.order[i + 1])}"
            for i in clashes[:EXAMPLES]
        )
        raise ValueError(
            f"{where}: moving the dates that Gregorian lacks to the last day of their month puts "
            f"{len(clashes)} step(s) on or before the previous step: {shown}. Gregorian has fewer "
            "days than this calendar there, so drop one step of each pair first (for example "
            "with xarray) and convert the result"
        )
    return CalendarMapping(
        calendar=calendar,
        rule=rule,
        stored=mapped.stored,
        n_time=n_time,
        moved=tuple((mapped.texts[i], mapped.stored_text(i)) for i in mapped.missing),
        spacing=mapped.spacing(),
    )


def _refusal(where: str, calendar: str, mapped: _Mapped, can_choose: bool) -> str:
    """The message for a source on a calendar that the caller has not chosen how to map."""
    if not can_choose:
        return (
            f"{where} uses the {calendar} calendar, and a store holds Gregorian timestamps. This "
            "command does not convert calendars, because it cannot record the conversion in the "
            f"store's provenance. Build the whole series with `chronozarr convert ... {OPTION} "
            f"{RULE_DATE}`"
        )
    spacing = None if mapped.clashes() else mapped.spacing()
    cost = ""
    if spacing is not None:
        cost = (
            f" The time elapsed between steps is not kept: {spacing}. Use it when the dates "
            "matter more than that."
        )
    lacking = ""
    if mapped.missing:
        lacking = (
            f" {len(mapped.missing)} of {len(mapped.values)} dates ({mapped.missing_examples()}) "
            f"do not exist in Gregorian: `{RULE_DATE}` stops on them and `{RULE_MONTH_END}` moves "
            "each to the last day of its month."
        )
    return (
        f"{where} uses the {calendar} calendar, and a store holds Gregorian timestamps. Pass "
        f"{OPTION} {RULE_DATE} to keep each step's date and time of day as its Gregorian "
        f"timestamp; the store then records the source calendar in its provenance.{cost}{lacking}"
    )


def provenance_with_note(
    source: str, mapping: CalendarMapping, existing: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Provenance (spec 4.5) that carries the calendar note, added to `existing` when given."""
    note = mapping.note()
    if existing is not None:
        merged = dict(existing)
        merged["notes"] = f"{existing['notes']} {note}" if existing.get("notes") else note
        return merged
    # A local path would put the user's directory names into a store that is then published.
    label = redact_url(source) if source.startswith(("http://", "https://")) else Path(source).name
    return {"sources": [label], "composite": "none", "gap_fill": "none", "notes": note}
