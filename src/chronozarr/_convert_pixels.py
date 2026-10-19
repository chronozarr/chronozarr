"""What the sampled timesteps say about the pixels: two plan lines, and a refusal.

`plan_conversion` reads three timesteps before anything is written. From those pixels the plan
gets a `fill:` line, a likely fill value that the files do not declare, and a `range:` line, the
value range after scale and offset. A value that looks like a fill could be data, so a weak
finding is only named in the plan, with the option that would mark it. A confident one stops the
conversion, dry run included, and the message gives both answers: `--nodata VALUE` marks it
missing, `--nodata none` keeps every value as data. Any explicit `--nodata` is such an answer, so
it is never refused again.

A value is a likely fill when the store keeps it as data (it is not the declared nodata and the
mask does not hide it) and one of two things holds:

* it lies far outside the other values. The sorted distinct values are cut wherever two
  neighbours are further apart than `GAP_SHARE` of the whole range. The piece with the most
  distinct values is the body; a value in another piece that holds `MIN_SHARE` of the pixels is a
  code (CHIRPS -9999 under rainfall of 0 to 1600; NSIDC 2530 and 2540 above concentrations of 0 to
  1000). Two pieces with the same number of distinct values, as in an image of 0 and 255, name no
  body and no code, and more than `MAX_CODES` codes mean the other piece is data.
* it is one of `KNOWN_FILLS` and holds `KNOWN_SHARE` of the pixels. Such a value can lie inside
  the range (Earth Engine's -3000 among NDVI x 10000), but a measured field rarely puts that
  share on one value.

Zero and the top of an integer type are found only by the first rule, so rainfall with zeros and
8-bit imagery with white pixels are left alone.

A finding is confident, and refused, when its value is one of `KNOWN_FILLS` and holds
`KNOWN_SHARE` of the pixels, or is any other value and holds `REFUSE_SHARE` of them. In the corpus
(scripts/corpus, 17 sources planned; 4 hold an undeclared fill of 0.1 % or more): CHIRPS -9999
holds 48 % of the valid pixels, NSIDC 2540 holds 20 % (south, monthly) and 46 % (north, daily), and
-3000 in a mosaic of Earth Engine tiles holds 3.7 %; all four are refused. NSIDC's second flag,
2530, holds 0.9 % and 3.7 %, and -3000 in one Earth Engine tile 0.3 %: they are named, and 2530 is
listed in the message that its neighbour causes. Every flagged value in the corpus is a fill or a
flag, at every share, so the corpus does not set the 5 %. It is the share at which one value in
twenty pixels is too many for a rare class, and a mistaken refusal costs one option.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import NamedTuple

import numpy as np

from chronozarr._convert_source import SourceInfo, Step

SAMPLE_PIXELS = 250_000  # pixels drawn per band across the sampled timesteps, when there are more
GAP_SHARE = 0.05  # a gap this share of the range of the values ends the body
MIN_SHARE = 0.001  # of the valid pixels, for a value outside the body
KNOWN_SHARE = 0.01  # of the valid pixels, for a value in KNOWN_FILLS
REFUSE_SHARE = 0.05  # of the valid pixels, for a value outside KNOWN_FILLS to stop the conversion
MAX_CODES = 5  # NSIDC's flags are the most seen in one band; more values outside the body are data
KNOWN_FILLS = (-9999, -999, -3000, -32768)


class _Code(NamedTuple):
    """One band's likely fill: its share of the band's valid pixels, whether it lies far outside
    the other values (else it is only a known fill value), and the range of those other values."""

    value: float
    share: float
    isolated: bool
    rest: tuple[float, float]


@dataclass(frozen=True)
class Fill:
    """A value that looks like a fill: its share of the valid pixels in the band that shows it
    most, the bands that show it, why, and the range of the other values when it is isolated."""

    value: float
    share: float
    bands: tuple[str, ...]
    isolated: bool
    rest: tuple[float, float]


def _valid_values(step: Step, info: SourceInfo, band: int, at: slice | np.ndarray) -> np.ndarray:
    """The values of `band` at flat positions `at` that the store counts as valid: not under a
    zero mask and not equal to the store's nodata."""
    values = step.data[band].reshape(-1)[at]
    keep = None
    if step.valid is not None:
        keep = step.valid.reshape(-1)[at] != 0
    if info.nodata is not None:
        unequal = values != info.nodata
        keep = unequal if keep is None else keep & unequal
    return values if keep is None else values[keep]


def _sample(steps: Sequence[Step], info: SourceInfo, band: int) -> np.ndarray:
    """Finite valid values of one band, at most `SAMPLE_PIXELS` pixels drawn over all steps."""
    random = np.random.default_rng(0)
    per_step = SAMPLE_PIXELS // len(steps)
    parts = []
    for step in steps:
        size = step.data.shape[1] * step.data.shape[2]
        at = slice(None) if size <= per_step else random.integers(0, size, per_step)
        parts.append(_valid_values(step, info, band, at))
    values = np.concatenate(parts).astype(np.float64)
    return values[np.isfinite(values)]


def _codes(values: np.ndarray) -> list[_Code]:
    """Every likely fill among one band's valid values, the largest share first."""
    if values.size == 0:
        return []
    distinct, counts = np.unique(values, return_counts=True)
    total = values.size
    found: dict[float, _Code] = {}
    gaps = np.diff(distinct) > GAP_SHARE * (distinct[-1] - distinct[0])
    starts = np.concatenate(([0], np.flatnonzero(gaps) + 1))
    ends = np.concatenate((starts[1:], [len(distinct)]))
    widths = ends - starts
    if len(widths) > 1 and np.count_nonzero(widths == widths.max()) == 1:
        body = int(widths.argmax())
        rest = (float(distinct[starts[body]]), float(distinct[ends[body] - 1]))
        outside = np.ones(len(distinct), dtype=bool)
        outside[starts[body] : ends[body]] = False
        codes = np.flatnonzero(outside & (counts >= MIN_SHARE * total))
        if len(codes) <= MAX_CODES:
            for i in codes:
                found[float(distinct[i])] = _Code(
                    float(distinct[i]), counts[i] / total, True, rest
                )
    for known in KNOWN_FILLS:
        i = int(np.searchsorted(distinct, known))
        if i < len(distinct) and distinct[i] == known and counts[i] >= KNOWN_SHARE * total:
            found.setdefault(
                float(known), _Code(float(known), counts[i] / total, False, (0.0, 0.0))
            )
    return sorted(found.values(), key=lambda code: -code.share)


def likely_fills(steps: Sequence[Step], info: SourceInfo) -> list[Fill]:
    """Likely undeclared fill values in the sampled timesteps, the largest share first."""
    hits: dict[float, list[tuple[str, _Code]]] = {}
    for band in range(info.n_band):
        for code in _codes(_sample(steps, info, band)):
            hits.setdefault(code.value, []).append((info.band_names[band], code))
    fills = []
    for value, shown in hits.items():
        best = max((code for _, code in shown), key=lambda code: (code.isolated, code.share))
        fills.append(
            Fill(value, best.share, tuple(name for name, _ in shown), best.isolated, best.rest)
        )
    return sorted(fills, key=lambda fill: -fill.share)


def refused(fill: Fill) -> bool:
    """Whether the finding is confident enough to stop the conversion instead of being named."""
    return fill.share >= (KNOWN_SHARE if fill.value in KNOWN_FILLS else REFUSE_SHARE)


def _number(value: float) -> str:
    """A number as `--nodata` reads it back: 9999 for 9999.0, the full repr for a huge float."""
    if abs(value) < 1e15 and value == int(value):
        return str(int(value))
    return repr(float(value))


def _percent(share: float) -> str:
    return f"{100 * share:.0f} %" if share >= 0.1 else f"{100 * share:.2g} %"


def _timesteps(count: int) -> str:
    return f"{count} sampled timestep{'' if count == 1 else 's'}"


def _and(items: Sequence[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def _clauses(fills: Sequence[Fill], info: SourceInfo, extent: str) -> list[str]:
    """What the findings are, in one clause for the values far outside the others and one for
    the common fill values. `extent` follows the first share: what the percentage is of."""
    isolated = [fill for fill in fills if fill.isolated]
    common = [fill for fill in fills if not fill.isolated]
    named = {}
    for fill in isolated + common:
        where = ""
        if info.n_band > 1:
            where = (
                " in all bands" if len(fill.bands) == info.n_band else f" in {_and(fill.bands)}"
            )
        named[fill.value] = (
            f"{_number(fill.value)}{where} ({_percent(fill.share)}{'' if named else extent})"
        )
    clauses = []
    if isolated:
        low, high = isolated[0].rest
        clauses.append(
            f"{_and([named[fill.value] for fill in isolated])} "
            f"{'lies' if len(isolated) == 1 else 'lie'} far outside the other values "
            f"({low:.4g} to {high:.4g})"
        )
    if common:
        clauses.append(
            f"{_and([named[fill.value] for fill in common])} "
            f"{'is a common fill value' if len(common) == 1 else 'are common fill values'}"
        )
    return clauses


def _mark(fills: Sequence[Fill], info: SourceInfo) -> str:
    """How to mark the findings missing, and what that costs: `--nodata` takes one value."""
    many = len(fills) > 1
    first = _number(fills[0].value)
    others = [_number(fill.value) for fill in fills[1:]]
    costs = []
    if many:
        verb = "stay" if len(others) > 1 else "stays"
        costs.append(f"a store takes one nodata value, so {_and(others)} {verb} data")
    if info.nodata is not None:
        declared = _number(info.nodata)
        costs.append(f"it replaces the declared nodata {declared}, so {declared} becomes data")
    text = f"If {'they mark' if many else 'it marks'} missing pixels, pass --nodata {first}"
    text += f": {'; '.join(costs)}." if costs else "."
    if many:
        text += (
            f" To mark {'both' if len(fills) == 2 else 'all'}, rewrite {_and(others)} as {first} "
            "in the files first, or give the files a mask (an alpha band or an internal mask)."
        )
    return text


def fill_refusal(fills: Sequence[Fill], n_steps: int, info: SourceInfo) -> str | None:
    """The message that stops a conversion whose sampled pixels hold a confident undeclared fill,
    or None when no finding is confident. Only a conversion without `--nodata` is refused."""
    if not any(refused(fill) for fill in fills):
        return None
    many = len(fills) > 1
    clauses = _clauses(fills, info, f" of the valid pixels in the {_timesteps(n_steps)}")
    return (
        f"{'; '.join(clauses)}, and nothing marks {'them' if many else 'it'} invalid, so the "
        f"store would keep {'them' if many else 'it'} as data. {_mark(fills, info)} "
        f"If {'they are' if many else 'it is'} data, pass --nodata none to keep every value."
    )


def fill_line(
    fills: Sequence[Fill], n_steps: int, info: SourceInfo, nodata: float | int | str | None
) -> str | None:
    """The text of the plan's `fill:` line, or None when there is nothing to say.

    `nodata` is the option as given: "auto", None for `--nodata none`, or the value. Nothing to say
    means no likely fill, and the files declare a nodata or the store has a mask, so the validity
    line already tells what is marked. With neither, "none found" says the check ran. A finding
    under "auto" names the option that marks it; under an explicit option it says the option
    already answered.
    """
    if not fills:
        if info.nodata is None and not info.mask:
            looked = ", ".join(str(value) for value in KNOWN_FILLS)
            return (
                f"none found in {_timesteps(n_steps)} (looked for a value far outside the "
                f"others, and for {looked})"
            )
        return None
    many = len(fills) > 1
    clauses = _clauses(fills, info, " of the valid pixels sampled")
    kept = f"{'; '.join(clauses)}. The store keeps {'them' if many else 'it'} as data"
    if nodata == "auto":
        return f"{kept}. {_mark(fills, info)}"
    if nodata is None:
        return f"{kept}, as --nodata none asks"
    assert not isinstance(nodata, str)  # a number: "auto" is the only string that gets here
    return f"{kept}; a store takes one nodata value and --nodata gave {_number(nodata)}"


def range_line(steps: Sequence[Step], info: SourceInfo) -> str | None:
    """The text of the plan's `range:` line: each band's valid values after scale and offset in
    the sampled timesteps. None when no band declares a scale or offset."""
    if all(band.scale in (None, 1.0) and band.offset in (None, 0.0) for band in info.bands):
        return None
    parts = []
    for index, band in enumerate(info.bands):
        scale = 1.0 if band.scale is None else band.scale
        offset = 0.0 if band.offset is None else band.offset
        extremes = [
            (float(values.min()), float(values.max()))
            for step in steps
            if (values := _valid_values(step, info, index, slice(None))).size
        ]
        units = f" [{band.units}]" if band.units else ""
        if not extremes:
            parts.append(f"{band.name} has no valid pixel{units}")
            continue
        ends = (
            min(low for low, _ in extremes) * scale + offset,
            max(high for _, high in extremes) * scale + offset,
        )
        parts.append(f"{band.name} {min(ends):.4g} to {max(ends):.4g}{units}")
    return (
        f"{', '.join(parts)} (stored * scale + offset, valid pixels of the "
        f"{_timesteps(len(steps))})"
    )
