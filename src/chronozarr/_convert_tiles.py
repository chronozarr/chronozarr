"""Decide whether files that share a date are tiles of one grid, and where each tile sits.

Earth Engine writes an image larger than its file limit as several files, named
`<name>-<row offset>-<column offset>.tif`, each a part of one grid. A series of such exports holds
several files per date, and the date alone cannot say whether files that share it are parts of one
image or copies. The grids say it: files are tiles of one grid when they have one CRS and one pixel
size, their origins are whole pixels apart (one pixel lattice, within the tolerance `_same_grid`
allows), and no two files of a date share a pixel. The store's grid is then the smallest one that
holds every file of every date; pixels that no file of a date covers are invalid on that date.
Files of one date that cover the same pixels are copies; files that overlap in part are refused,
because which value a shared pixel takes is the user's decision. This module reads no pixels.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from chronozarr._convert_discover import show_time
from chronozarr._convert_preflight import Problem
from chronozarr._convert_source import Grid, _crs_label, _same_crs, _same_grid

OPTION = "--mosaic-tiles"

# Files that are not on one pixel lattice can only be joined by resampling, the user's choice.
REGRID_FIX = (
    "join the files of each date onto one grid yourself, with a resampling you choose (for "
    "example gdalwarp -tap -tr XRES YRES -r nearest TILES... joined.tif), and convert the "
    "joined files; or export the tiles again on one grid (in Earth Engine, pass crs and "
    "crsTransform to Export.image)"
)
# Files that share pixels can only be joined by a rule for those pixels, the user's choice.
OVERLAP_JOIN = (
    "join the files of each date yourself with a rule you choose for the pixels they share (for "
    "example gdal_merge.py, where the last file wins), and convert the joined files"
)


@dataclass(frozen=True)
class SameDateFixes:
    """The parts of the fixes for files that share a date that depend on the command: how to
    join tiles of one grid (a sentence), and how else the files can get distinct times (a clause
    after "or", or None when the command has no such way)."""

    join: str
    distinct: str | None


CONVERT_FIXES = SameDateFixes(
    join=f"pass {OPTION} to join the tiles of each date into one timestep",
    distinct="if the names carry a time of day pass --date-pattern with %H%M%S, or write a "
    "manifest with distinct datetimes",
)


def _keep_one(fixes: SameDateFixes) -> str:
    """Keep one of several images of one date, or give them distinct times."""
    keep = "keep one of them: move the others out of the folder (or narrow the glob to the files"
    keep += " you want)"
    return keep if fixes.distinct is None else f"{keep}; or {fixes.distinct}"


@dataclass(frozen=True)
class Window:
    """Where a file sits in the grid of the store, in pixels."""

    row: int
    col: int
    height: int
    width: int

    def describe(self) -> str:
        return (
            f"rows {self.row}..{self.row + self.height - 1}, "
            f"columns {self.col}..{self.col + self.width - 1}"
        )


@dataclass(frozen=True)
class TileLayout:
    """Files that tile one grid: the grid that holds them all and the tiles of every date.

    `tiles[t]` are the (uri, window) of timestep t, in file order; no two of a date overlap.
    `uncovered[t]` is the number of pixels of the grid that no tile of timestep t covers; those are
    invalid.
    """

    grid: Grid
    times: tuple[np.datetime64, ...]
    tiles: tuple[tuple[tuple[str, Window], ...], ...]
    uncovered: tuple[int, ...]

    @property
    def n_files(self) -> int:
        return sum(len(tiles) for tiles in self.tiles)

    def footprint(self) -> str | None:
        """Why the store must mark pixels invalid that no file declares invalid, or None."""
        gaps = sum(1 for n in self.uncovered if n)
        if not gaps:
            return None
        return f"the tiles leave pixels uncovered on {gaps} of {len(self.times)} dates"

    def note(self) -> str:
        """The plan line: how many tiles make each date, and whether they leave gaps."""
        counts = sorted({len(tiles) for tiles in self.tiles})
        per = str(counts[0]) if len(counts) == 1 else f"{counts[0]} to {counts[-1]}"
        grid = f"{self.grid.height} x {self.grid.width} px"
        parts = [
            f"{self.n_files} files are tiles of one {grid} grid, {per} per date, and the tiles "
            "of each date are joined into one timestep"
        ]
        gaps = [n for n in self.uncovered if n]
        if gaps:
            share = max(gaps) / (self.grid.height * self.grid.width)
            parts.append(
                f"on {len(gaps)} dates the tiles leave pixels uncovered (up to {share:.0%} of "
                "the grid), and those pixels are invalid"
            )
        else:
            parts.append("every date covers the whole grid")
        return "tiles:      " + "; ".join(parts)

    def listing(self) -> list[str]:
        """The `files:` lines of the dry run: every tile with its date and place in the grid."""
        rows = [
            (show_time(time), window.describe(), uri)
            for time, tiles in zip(self.times, self.tiles, strict=True)
            for uri, window in tiles
        ]
        width = max(len(place) for _, place, _ in rows)
        return [
            f"files:      {self.n_files}, dated from their names and placed by their grids",
            *(f"  {day}  {place.ljust(width)}  {uri}" for day, place, uri in rows),
        ]


def _name(uri: str) -> str:
    return uri.rsplit("/", 1)[-1]


def _names(uris: Sequence[str]) -> str:
    """'a', 'a and b', 'a, b and c', or 'a, b, c and 2 more', by file name."""
    names = [_name(uri) for uri in uris]
    if len(names) > 3:
        return ", ".join(names[:3]) + f" and {len(names) - 3} more"
    if len(names) > 1:
        return ", ".join(names[:-1]) + f" and {names[-1]}"
    return names[0]


def _off_lattice(grid: Grid, first: Grid, first_uri: str) -> str:
    """Why `grid` is not on the pixel lattice of `first`."""
    name = _name(first_uri)
    if not _same_crs(grid.crs, first.crs):
        return (
            f"has CRS {_crs_label(grid.crs)}; {name} has {_crs_label(first.crs)}, and files join "
            "as tiles only on one grid"
        )
    a, _, c, _, e, f = grid.transform
    a0, _, c0, _, e0, f0 = first.transform
    tolerance = 1e-6 * abs(a0)
    if abs(a - a0) > tolerance or abs(e - e0) > tolerance:
        return (
            f"has pixels of {a:.10g} x {-e:.10g}; {name} has {a0:.10g} x {-e0:.10g}, and files "
            "join as tiles only on one grid"
        )
    across, down = (c - c0) / a0, (f - f0) / e0
    return (
        f"is off the pixel grid of {name} by {abs(across - round(across)):.3g} px across and "
        f"{abs(down - round(down)):.3g} px down, and files join as tiles only on one pixel grid"
    )


def _place(
    files: Sequence[tuple[np.datetime64, str, Grid]],
) -> tuple[list[tuple[np.datetime64, str, Window]], list[Problem]]:
    """Each file's window on the pixel lattice of the first, in pixels from its origin (negative
    above or left of it), and a problem for each file that is not on that lattice."""
    _, first_uri, first = files[0]
    a, _, c, _, e, f = first.transform
    placed: list[tuple[np.datetime64, str, Window]] = []
    off: list[Problem] = []
    for time, uri, grid in files:
        col = round((grid.transform[2] - c) / a)
        row = round((grid.transform[5] - f) / e)
        there = (a, 0.0, c + col * a, 0.0, e, f + row * e)
        if _same_grid(grid, Grid(first.crs, there, grid.height, grid.width)):
            placed.append((time, uri, Window(row, col, grid.height, grid.width)))
        else:
            off.append(Problem(uri, _off_lattice(grid, first, first_uri), REGRID_FIX))
    return placed, off


def _copies(
    placed: Sequence[tuple[np.datetime64, str, Window]], fixes: SameDateFixes
) -> list[Problem]:
    """A problem for every file that covers the same pixels as another file of its date."""
    by_place: dict[tuple[np.datetime64, Window], list[str]] = defaultdict(list)
    for time, uri, window in placed:
        by_place[(time, window)].append(uri)
    problems = []
    for (time, _), uris in by_place.items():
        for uri in uris if len(uris) > 1 else ():
            others = _names([u for u in uris if u != uri])
            problems.append(
                Problem(
                    uri,
                    f"has the same date {show_time(time)} as {others} and covers the same "
                    "pixels, so they are copies or images of one area, not tiles of one; a store "
                    "has one timestep per time",
                    _keep_one(fixes),
                )
            )
    return problems


def _shared_pixels(a: Window, b: Window) -> int:
    rows = min(a.row + a.height, b.row + b.height) - max(a.row, b.row)
    cols = min(a.col + a.width, b.col + b.width) - max(a.col, b.col)
    return max(rows, 0) * max(cols, 0)


def _overlaps(placed: Sequence[tuple[np.datetime64, str, Window]]) -> list[Problem]:
    """A problem for every file that shares pixels with an earlier file of its date and does not
    cover the same pixels (those are copies)."""
    by_time: dict[np.datetime64, list[tuple[str, Window]]] = defaultdict(list)
    for time, uri, window in placed:
        by_time[time].append((uri, window))
    problems = []
    for time, files in by_time.items():
        for i, (uri, window) in enumerate(files):
            earlier = [(u, w) for u, w in files[:i] if w != window and _shared_pixels(w, window)]
            if not earlier:
                continue
            shared = sum(_shared_pixels(w, window) for _, w in earlier)
            problems.append(
                Problem(
                    uri,
                    f"overlaps {_names([u for u, _ in earlier])} by {shared} px on "
                    f"{show_time(time)}, and tiles of one image do not overlap",
                    f"{OVERLAP_JOIN}; or keep one of them",
                )
            )
    return problems


def _layout(placed: Sequence[tuple[np.datetime64, str, Window]], first: Grid) -> TileLayout:
    """The smallest grid on the lattice of `first` that holds every placed file, and the tiles of
    each date in it."""
    top = min(w.row for _, _, w in placed)
    left = min(w.col for _, _, w in placed)
    height = max(w.row + w.height for _, _, w in placed) - top
    width = max(w.col + w.width for _, _, w in placed) - left
    a, _, c, _, e, f = first.transform
    grid = Grid(first.crs, (a, 0.0, c + left * a, 0.0, e, f + top * e), height, width)
    by_time: dict[np.datetime64, list[tuple[str, Window]]] = defaultdict(list)
    for time, uri, w in placed:
        by_time[time].append((uri, Window(w.row - top, w.col - left, w.height, w.width)))
    times = tuple(sorted(by_time))
    tiles = tuple(tuple(by_time[time]) for time in times)
    return TileLayout(
        grid=grid,
        times=times,
        tiles=tiles,
        # the tiles of a date do not overlap, so what they cover is the sum of their areas
        uncovered=tuple(
            height * width - sum(w.height * w.width for _, w in date) for date in tiles
        ),
    )


def lay_out(
    files: Sequence[tuple[np.datetime64, str, Grid]], fixes: SameDateFixes = CONVERT_FIXES
) -> TileLayout | list[Problem]:
    """The tiles of `files` (time, uri, grid; in time order, at least one) on one grid, or a
    problem for every file that keeps them from being tiles of one: a file off the pixel lattice
    of the first, files of one date that cover the same pixels, and files of one date that share
    some pixels."""
    placed, off = _place(files)
    problems = [*off, *_copies(placed, fixes), *_overlaps(placed)]
    if problems:
        return problems
    return _layout(placed, files[0][2])


def same_date_problems(
    entries: Sequence[tuple[np.datetime64, str]],
    grids: Mapping[str, Grid],
    fixes: SameDateFixes = CONVERT_FIXES,
) -> tuple[list[Problem], frozenset[str]]:
    """The problems of files that share a date, for a conversion without `--mosaic-tiles`, each
    with the fix that is true of those files; and the files whose own grid is no problem, because
    joining the tiles would place them.

    `entries` are every dated file (time, uri) in time order; `grids` the grids of the files that
    could be opened; `fixes` the wording of the command (`encode` and `append` have no option to
    join tiles). Files of one date are offered the join only when they are on one pixel lattice
    with the files they share it with and share no pixel: then keeping one would store only part
    of the area. Files on the same pixels are copies, and keeping one is the fix.
    """
    by_time: dict[np.datetime64, list[str]] = defaultdict(list)
    for time, uri in entries:
        by_time[time].append(uri)
    shared = {time: uris for time, uris in by_time.items() if len(uris) > 1}
    opened = [(time, uri, grids[uri]) for time, uri in entries if uri in grids]
    if not shared or not opened:
        return [], frozenset()
    placed, off = _place(opened)
    off_uris = {p.uri for p in off}
    copies = {p.uri: p for p in _copies(placed, fixes)}
    overlapping = {p.uri for p in _overlaps(placed)}
    on_lattice = [p for p in placed if p[1] not in copies]
    size = ""
    if on_lattice:
        grid = _layout(on_lattice, opened[0][2]).grid
        size = f"{grid.height} x {grid.width} px"
    different = "if they are different images, keep one (move the others out of the folder)"
    if fixes.distinct is not None:
        different += f", or {fixes.distinct}"
    problems: list[Problem] = []
    offered = False
    for time, uris in shared.items():
        day = show_time(time)
        for uri in uris:
            if uri in copies:
                problems.append(copies[uri])
                continue
            same = f"has the same date {day} as {_names([u for u in uris if u != uri])}"
            if any(u not in grids for u in uris):
                problems.append(
                    Problem(
                        uri,
                        f"{same}; a store has one timestep per time",
                        "fix the files of this date that cannot be opened (above), then run "
                        "again: the check then says whether they are tiles of one grid or copies",
                    )
                )
            elif any(u in off_uris for u in uris):
                problems.append(
                    Problem(
                        uri,
                        f"{same}; a store has one timestep per time",
                        f"{different}; if they are parts of one image, they are not on one pixel "
                        "grid (their CRS, pixel size or pixel alignment differ), so "
                        f"{REGRID_FIX}",
                    )
                )
            elif any(u in overlapping for u in uris):
                problems.append(
                    Problem(
                        uri,
                        f"{same}; a store has one timestep per time",
                        f"{different}; if they are parts of one image, they overlap, so "
                        f"{OVERLAP_JOIN}",
                    )
                )
            elif any(u in copies for u in uris):
                problems.append(
                    Problem(
                        uri,
                        f"{same}; a store has one timestep per time",
                        "leave out the copies reported for the other files of this date; the "
                        f"files left are tiles of one grid: then {fixes.join}",
                    )
                )
            else:
                offered = True
                problems.append(
                    Problem(
                        uri,
                        f"{same}, and together they are tiles of one {size} grid, each a "
                        "different part of it, as Earth Engine splits a large export into files",
                        f"{fixes.join}; keeping one file per date would store only part of the "
                        "area",
                    )
                )
    excused = frozenset(uri for _, uri, _ in on_lattice) if offered else frozenset()
    return problems, excused
