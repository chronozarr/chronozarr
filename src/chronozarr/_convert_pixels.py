"""What the sampled timesteps say about the pixels, as two plan lines.

`plan_conversion` reads three timesteps before anything is written. From those pixels the plan
gets a `fill:` line, a likely fill value that the files do not declare, and a `range:` line, the
value range after scale and offset. Neither changes what is written or refuses a conversion: a
value that looks like a fill could be data, so the plan names it and the option that would mark it.

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


def fill_line(steps: Sequence[Step], info: SourceInfo) -> str | None:
    """The text of the plan's `fill:` line, or None when there is nothing to say.

    Nothing to say means no likely fill, and the files declare a nodata or the store has a mask,
    so the validity line already tells what is marked. With neither, "none found" says the
    check ran.
    """
    fills = likely_fills(steps, info)
    if not fills:
        if info.nodata is None and not info.mask:
            looked = ", ".join(str(value) for value in KNOWN_FILLS)
            return (
                f"none found in {_timesteps(len(steps))} (looked for a value far outside the "
                f"others, and for {looked})"
            )
        return None
    isolated = [fill for fill in fills if fill.isolated]
    common = [fill for fill in fills if not fill.isolated]
    named = {}
    for fill in isolated + common:
        where = ""
        if info.n_band > 1:
            where = (
                " in all bands" if len(fill.bands) == info.n_band else f" in {_and(fill.bands)}"
            )
        extent = "" if named else " of the valid pixels sampled"
        named[fill.value] = f"{_number(fill.value)}{where} ({_percent(fill.share)}{extent})"
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
    many = len(fills) > 1
    fix = (
        f"If {'they mark' if many else 'it marks'} missing pixels, "
        f"pass --nodata {_number(fills[0].value)}"
    )
    if many:
        others = [_number(fill.value) for fill in fills[1:]]
        verb = "stay" if len(others) > 1 else "stays"
        fix += f"; it takes one value, so {_and(others)} {verb} data"
    if info.nodata is not None:
        fix += f"; it replaces the declared nodata {_number(info.nodata)}"
    return f"{'; '.join(clauses)}. The store keeps {'them' if many else 'it'} as data. {fix}"


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
