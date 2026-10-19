"""Read the dates of a raster whose bands are dates, from the band descriptions.

Earth Engine's `ee.ImageCollection.toBands()` stacks the images of a collection into one image
whose bands are named `<image id>_<band>`, and the image id of a dated composite holds the date:
`2020_01_01_NDVI`. GDAL keeps those names as band descriptions. This module decides whether the
descriptions of one file describe a time series, and which bands belong to which date. It reads
no pixels and opens no file: the caller hands it the descriptions.

A file is a series when every description holds exactly one date, read as a file name's date is
(`discover`), and every date has the same set of bands. The part of a description besides its date
names the band: `NDVI` in `2020_01_01_NDVI`. Several bands per date (`2020_01_01_NDVI`,
`2020_01_01_EVI`) are the bands of one timestep, in the order the file first lists them.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from chronozarr._convert_discover import (
    DATE_MARK,
    _compile_pattern,
    find_dates,
    mask_dates,
    show_time,
)
from chronozarr._convert_preflight import Problem

OPTION = "--bands-are-dates"


class NotBandDates(ValueError):
    """The descriptions do not describe a series; `problems` say why, each with a remedy."""

    def __init__(self, problems: Sequence[Problem]) -> None:
        self.problems = tuple(problems)
        super().__init__("; ".join(p.detail for p in self.problems))


@dataclass(frozen=True)
class BandDates:
    """The series that the band descriptions of one file describe.

    `times` are ascending. `names` are the band names of every timestep, in the order the file
    first lists them. `numbers[t]` and `descriptions[t]` are the 1-based file band numbers and the
    descriptions of timestep t's bands, in `names` order.
    """

    times: tuple[np.datetime64, ...]
    names: tuple[str, ...]
    numbers: tuple[tuple[int, ...], ...]
    descriptions: tuple[tuple[str, ...], ...]
    named_by_index: bool  # the descriptions hold nothing but the date, so the band is named `1`

    @property
    def n_bands(self) -> int:
        return sum(len(numbers) for numbers in self.numbers)

    def _sentence(self) -> str:
        first, last = self.descriptions[0][0], self.descriptions[-1][0]
        return (
            f"{self.n_bands} bands read as {len(self.times)} dates from their descriptions "
            f"('{first}' to '{last}')"
        )

    def note(self) -> str:
        """The plan line: what was read, and what the bands are named."""
        if self.named_by_index:
            named = (
                "the descriptions hold nothing but the date, so the band is named "
                f"'{self.names[0]}'"
            )
        elif len(self.names) == 1:
            named = f"the band is named '{self.names[0]}', the part they share"
        else:
            quoted = ", ".join(f"'{name}'" for name in self.names)
            named = (
                f"each date has {len(self.names)} bands, named by what follows the date: {quoted}"
            )
        return f"{self._sentence()}; {named}"

    def offer(self) -> str:
        """The remedy for a file that was not given the option: what the option would read."""
        if len(self.names) == 1:
            made = f"one band named {self.names[0]}"
        else:
            made = f"the bands {', '.join(self.names)}"
        return f"pass {OPTION} to read the file as a series: its {self._sentence()}, as {made}"

    def listing(self) -> list[str]:
        """One row per timestep for the dry run: its date, its band numbers and descriptions."""
        bands = [
            f"{'band' if len(numbers) == 1 else 'bands'} {', '.join(map(str, numbers))}"
            for numbers in self.numbers
        ]
        width = max(len(text) for text in bands)
        return [
            f"  {show_time(time)}  {text.ljust(width)}  {', '.join(descriptions)}"
            for time, text, descriptions in zip(self.times, bands, self.descriptions, strict=True)
        ]


def _list(items: Sequence[str], limit: int = 3) -> str:
    """'a', 'a and b', 'a, b and c', or 'a, b, c and 2 more'."""
    if len(items) > limit:
        return ", ".join(items[:limit]) + f" and {len(items) - limit} more"
    if len(items) > 1:
        return ", ".join(items[:-1]) + f" and {items[-1]}"
    return items[0]


def _holding(count: int, what: str) -> str:
    """'1 band description holds what', '2 band descriptions hold what'."""
    plural = count != 1
    return (
        f"{count} band description{'s' if plural else ''} {'hold' if plural else 'holds'} {what}"
    )


def _band(number: int, description: str) -> str:
    return f"band {number} '{description}'"


def _name_of(key: str) -> str:
    """The band name in the text that stays when the date is taken out of a description."""
    text = re.sub(r"([ _.:/-])\1+", r"\1", key.replace(DATE_MARK, ""))
    return text.strip(" _-.:/")


def read_band_dates(
    uri: str,
    numbers: Sequence[int],
    descriptions: Sequence[str | None],
    date_pattern: str | None = None,
) -> BandDates:
    """The series described by the band descriptions of the file `uri`.

    `numbers` are the 1-based file band numbers of the data bands and `descriptions` their
    descriptions (None or empty for none). `date_pattern` is the `--date-pattern` that says where
    the date is in a description; without it the default forms are read, as in file names.
    Raises `NotBandDates` with every problem found, each as a `Problem` of `uri`.
    """
    pattern = None if date_pattern is None else _compile_pattern(date_pattern)
    if len(numbers) < 2:
        raise NotBandDates(
            [
                Problem(
                    uri,
                    f"it has {len(numbers)} band{'s' if len(numbers) != 1 else ''}, and "
                    f"{OPTION} needs a band for each date, so at least two",
                    "leave the option out and give the file one date in its name, or write one "
                    "file per date",
                )
            ]
        )
    readings: list[tuple[int, str, np.datetime64]] = []  # position, description, date
    missing: list[int] = []
    undated: list[str] = []
    several: list[str] = []
    unreal: list[str] = []
    for position, (number, description) in enumerate(zip(numbers, descriptions, strict=True)):
        if not description:
            missing.append(number)
            continue
        found, invalid = find_dates(description, pattern)
        if invalid:
            unreal.append(f"{_band(number, description)} ('{invalid[0]}')")
        elif not found:
            undated.append(_band(number, description))
        elif len(found) > 1:
            several.append(_band(number, description))
        else:
            ((moment, _),) = found.items()
            readings.append((position, description, moment))
    problems = _description_problems(uri, pattern is not None, date_pattern, missing, undated)
    problems += _date_problems(uri, date_pattern, several, unreal)
    if problems:
        raise NotBandDates(problems)

    by_date: dict[np.datetime64, dict[str, int]] = {}
    repeats: list[Problem] = []
    for position, description, moment in readings:
        bands = by_date.setdefault(moment, {})
        key = mask_dates(description, pattern)
        if key in bands:
            earlier = bands[key]
            repeats.append(
                Problem(
                    uri,
                    f"{_band(numbers[earlier], descriptions[earlier] or '')} and "
                    f"{_band(numbers[position], description)} are the same date, "
                    f"{show_time(moment)}, and name the same band; a store has one timestep per "
                    "time",
                    "give the bands different names, or take one out of the file with "
                    "gdal_translate -b",
                )
            )
        else:
            bands[key] = position
    if repeats:
        raise NotBandDates(_capped(repeats, "pairs of bands that repeat a date"))
    if len(by_date) < 2:
        (only,) = by_date
        raise NotBandDates(
            [
                Problem(
                    uri,
                    f"all the bands are the same date, {show_time(only)}; a series needs at "
                    "least two dates",
                    "leave the option out and give the file its date in its name, or fix the "
                    "descriptions",
                )
            ]
        )
    order = list(dict.fromkeys(mask_dates(d, pattern) for _, d, _ in readings))
    names = tuple(_name_of(key) for key in order)
    uneven = _uneven_dates(uri, by_date, order, names)
    if uneven:
        raise NotBandDates(uneven)
    named_by_index = len(order) == 1 and names[0] == ""
    if named_by_index:
        names = ("1",)
    else:
        unnamed = _unnamed_bands(uri, numbers, by_date, order, names)
        if unnamed:
            raise NotBandDates(unnamed)
    times = tuple(sorted(by_date))
    return BandDates(
        times,
        names,
        tuple(tuple(numbers[by_date[time][key]] for key in order) for time in times),
        tuple(tuple(descriptions[by_date[time][key]] or "" for key in order) for time in times),
        named_by_index,
    )


def _description_problems(
    uri: str,
    has_pattern: bool,
    date_pattern: str | None,
    missing: list[int],
    undated: list[str],
) -> list[Problem]:
    problems = []
    if missing:
        what = (
            f"band {missing[0]} has"
            if len(missing) == 1
            else (f"bands {_list([str(n) for n in missing])} have")
        )
        problems.append(
            Problem(
                uri,
                f"{what} no description, so no date",
                "give every band a description that holds its date, as Earth Engine's "
                "toBands() does (2020_01_01_NDVI), or write one file per date with "
                "gdal_translate -b and convert the folder",
            )
        )
    if undated:
        what = f"no date that --date-pattern '{date_pattern}' reads" if has_pattern else "no date"
        problems.append(
            Problem(
                uri,
                f"{_holding(len(undated), what)}: {_list(undated)}",
                "write the date in each description as YYYYMMDD, YYYY-MM-DD or YYYY-MM, or pass "
                "--date-pattern with the text around the date (for example 'ndvi_%Y%m%d')",
            )
        )
    return problems


def _date_problems(
    uri: str, date_pattern: str | None, several: list[str], unreal: list[str]
) -> list[Problem]:
    problems = []
    if several:
        problems.append(
            Problem(
                uri,
                f"{_holding(len(several), 'several dates')}: {_list(several)}",
                "pass --date-pattern with the text around the date to use (for example "
                "'composite_%Y%m%d')",
            )
        )
    if unreal:
        problems.append(
            Problem(
                uri,
                f"--date-pattern '{date_pattern}' matches text that is not a real date: "
                f"{_list(unreal)}",
                "correct the descriptions, or take those bands out of the file with "
                "gdal_translate -b",
            )
        )
    return problems


def _capped(problems: list[Problem], what: str, limit: int = 3) -> list[Problem]:
    """The first `limit` problems, and one more that counts the rest."""
    if len(problems) <= limit:
        return problems
    last = problems[limit - 1]
    return [
        *problems[: limit - 1],
        Problem(last.uri, f"and {len(problems) - limit + 1} more {what}", last.fix),
    ]


def _uneven_dates(
    uri: str,
    by_date: dict[np.datetime64, dict[str, int]],
    order: list[str],
    names: tuple[str, ...],
) -> list[Problem]:
    """A problem when some dates lack a band that others have: a timestep holds the same bands."""
    wrong = [
        f"{show_time(time)} has "
        f"{_list([name for key, name in zip(order, names, strict=True) if key in bands])}"
        for time, bands in sorted(by_date.items())
        if len(bands) != len(order)
    ]
    if not wrong:
        return []
    return [
        Problem(
            uri,
            f"the dates do not all have the same bands: {_list(wrong)} "
            f"(together the bands are {_list([n or 'an unnamed one' for n in names])})",
            "a store holds the same bands for every date: take the extra bands out of the file "
            "with gdal_translate -b, or add the missing ones",
        )
    ]


def _unnamed_bands(
    uri: str,
    numbers: Sequence[int],
    by_date: dict[np.datetime64, dict[str, int]],
    order: list[str],
    names: tuple[str, ...],
) -> list[Problem]:
    """Problems when several bands per date are not told apart by the text besides the date."""
    first = by_date[min(by_date)]
    if "" in names:
        number = numbers[first[order[names.index("")]]]
        return [
            Problem(
                uri,
                f"the description of band {number} holds nothing but the date, so the bands of a "
                "date cannot be told apart",
                "name each band after its variable, as Earth Engine does (2020_01_01_NDVI)",
            )
        ]
    repeated = [name for name in dict.fromkeys(names) if names.count(name) > 1]
    if repeated:
        return [
            Problem(
                uri,
                f"two parts of the descriptions would both name a band '{repeated[0]}'",
                "write the variable in the same place in every description",
            )
        ]
    return []
