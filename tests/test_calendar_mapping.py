"""The mapping of a cftime axis (360_day, noleap, all_leap, julian) onto Gregorian timestamps.

Each step keeps its calendar date and time of day. A date that Gregorian lacks stops the mapping
unless the caller asks for it to move to the last day of its month. The conversion tests are in
test_calendars.py.
"""

from __future__ import annotations

import numpy as np
import pytest

from chronozarr import schema
from chronozarr._calendar import (
    OPTION,
    RULE_DATE,
    RULE_MONTH_END,
    check_rule,
    provenance_with_note,
    to_gregorian,
)

pytestmark = pytest.mark.unit

cftime = pytest.importorskip("cftime")


def _dates(cls, *ymd_hms: tuple[int, ...]) -> np.ndarray:
    return np.array([cls(*parts) for parts in ymd_hms], dtype=object)


def test_a_360_day_axis_keeps_each_date_and_time_of_day():
    values = _dates(cftime.Datetime360Day, (2000, 1, 16, 12), (2000, 2, 16, 12), (2000, 3, 30, 6))
    mapping = to_gregorian(values, "360_day", RULE_DATE, name="time")
    assert [str(t) for t in mapping.stored.astype("datetime64[s]")] == [
        "2000-01-16T12:00:00",
        "2000-02-16T12:00:00",
        "2000-03-30T06:00:00",
    ]
    assert mapping.moved == ()


def test_spacing_is_reported_in_numbers():
    mapping = to_gregorian(
        _dates(cftime.Datetime360Day, *[(y, m, 16) for y in (2000, 2001) for m in range(1, 13)]),
        "360_day",
        RULE_DATE,
        name="time",
    )
    assert mapping.spacing is not None
    assert "30 days apart in the source are 28 to 31 days apart in the store" in mapping.spacing
    assert "gaps change" in mapping.spacing


def test_a_date_gregorian_lacks_stops_the_conversion_and_is_named():
    values = _dates(cftime.Datetime360Day, (2000, 1, 30), (2000, 2, 30), (2001, 2, 30))
    with pytest.raises(ValueError) as caught:
        to_gregorian(values, "360_day", RULE_DATE, name="time")
    message = str(caught.value)
    assert "2 of 3 dates that Gregorian does not have: 2000-02-30, 2001-02-30" in message
    assert f"{OPTION} {RULE_MONTH_END}" in message


def test_month_end_moves_a_missing_date_to_the_last_day_of_its_month():
    values = _dates(cftime.Datetime360Day, (2000, 1, 30), (2000, 2, 30), (2001, 2, 30, 12))
    mapping = to_gregorian(values, "360_day", RULE_MONTH_END, name="time")
    assert [str(t) for t in mapping.stored.astype("datetime64[s]")] == [
        "2000-01-30T00:00:00",
        "2000-02-29T00:00:00",  # 2000 is a Gregorian leap year
        "2001-02-28T12:00:00",
    ]
    assert mapping.moved == (
        ("2000-02-30", "2000-02-29"),
        ("2001-02-30T12:00:00", "2001-02-28T12:00:00"),
    )


def test_month_end_refuses_a_move_onto_another_step():
    values = _dates(
        cftime.Datetime360Day, (2001, 2, 28), (2001, 2, 29), (2001, 2, 30), (2001, 3, 1)
    )
    with pytest.raises(ValueError, match=r"2001-02-29 and 2001-02-30 both land on 2001-02-28"):
        to_gregorian(values, "360_day", RULE_MONTH_END, name="time")


def test_month_end_refuses_a_move_that_puts_a_step_before_its_predecessor():
    values = _dates(cftime.Datetime360Day, (2001, 2, 28, 12), (2001, 2, 29, 0))
    with pytest.raises(ValueError, match="on or before the previous step"):
        to_gregorian(values, "360_day", RULE_MONTH_END, name="time")


@pytest.mark.parametrize(
    ("cls", "parts", "lacking"),
    [
        (cftime.DatetimeNoLeap, (2000, 2, 28), False),
        (cftime.DatetimeAllLeap, (2001, 2, 29), True),
        (cftime.DatetimeAllLeap, (2000, 2, 29), False),
        (cftime.DatetimeJulian, (1900, 2, 29), True),
        (cftime.DatetimeJulian, (2000, 2, 29), False),
    ],
)
def test_the_other_non_gregorian_calendars_lack_only_dates_past_a_months_end(cls, parts, lacking):
    values = _dates(cls, parts)
    if lacking:
        with pytest.raises(ValueError, match="does not have"):
            to_gregorian(values, "x", RULE_DATE, name="time")
        moved = to_gregorian(values, "x", RULE_MONTH_END, name="time").moved
        assert len(moved) == 1
    else:
        assert to_gregorian(values, "x", RULE_DATE, name="time").moved == ()


def test_refusal_names_the_choice_its_cost_and_the_dates_that_do_not_exist():
    values = _dates(cftime.Datetime360Day, (2000, 1, 30), (2000, 2, 30), (2000, 3, 30))
    with pytest.raises(ValueError) as caught:
        to_gregorian(values, "360_day", None, name="time")
    message = str(caught.value)
    assert "uses the 360_day calendar" in message
    assert f"Pass {OPTION} {RULE_DATE}" in message
    assert "not kept" in message
    assert f"`{RULE_MONTH_END}`" in message
    assert "1 of 3 dates (2000-02-30)" in message


def test_a_command_without_the_option_points_at_the_one_that_has_it():
    values = _dates(cftime.Datetime360Day, (2000, 1, 16), (2000, 2, 16))
    with pytest.raises(
        ValueError, match=r"chronozarr convert \.\.\. --calendar-to-gregorian date"
    ):
        to_gregorian(values, "360_day", None, name="time", can_choose=False)


def test_an_unknown_rule_is_refused():
    with pytest.raises(ValueError, match="calendar_to_gregorian must be one of"):
        check_rule("nearest")


def test_provenance_names_the_file_not_its_directory_and_is_valid():
    mapping = to_gregorian(
        _dates(cftime.Datetime360Day, (2000, 1, 16), (2000, 2, 16)), "360_day", RULE_DATE, name="t"
    )
    provenance = provenance_with_note("/home/jo/data/model.nc", mapping, None)
    assert provenance["sources"] == ["model.nc"]
    assert schema.parse_provenance(provenance) == provenance
    assert "360_day calendar" in provenance["notes"]


def test_provenance_keeps_what_the_caller_wrote():
    mapping = to_gregorian(
        _dates(cftime.Datetime360Day, (2000, 1, 16), (2000, 2, 16)), "360_day", RULE_DATE, name="t"
    )
    mine = {
        "sources": ["CMIP6"],
        "composite": "monthly mean",
        "gap_fill": "none",
        "notes": "mine.",
    }
    merged = provenance_with_note("model.nc", mapping, mine)
    assert merged["sources"] == ["CMIP6"]
    assert merged["notes"].startswith("mine. Time axis: 360_day calendar")
