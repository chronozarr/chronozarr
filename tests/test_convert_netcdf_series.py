"""`convert` on NetCDF files that together form one time series.

NOAA, NASA and ESGF deliver a series as one file per day, month or decade. A directory or quoted
glob of them is one source, dated by the time coordinate inside each file.
"""

from __future__ import annotations

import tracemalloc
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from click.testing import CliRunner

import chronozarr
from chronozarr._convert_netcdf_files import find_netcdf_files
from chronozarr._convert_xarray import _OpenFiles
from chronozarr.cli import main
from chronozarr.convert import PreflightError, convert, plan_conversion
from tests.fixtures.netcdf_days import FILL, day_ok, day_values, write_days, write_series

pytestmark = pytest.mark.unit

pytest.importorskip("rasterio")
pytest.importorskip("h5netcdf")

OPTIONS: dict[str, Any] = {"chunk_size": 16}
SERIES: dict[str, Any] = {"variable": "sst", "crs": "EPSG:4326"}


def _stored(out: Path) -> np.ndarray:
    return chronozarr.open_store(out).to_xarray().values


def _expected(days: list[int]) -> np.ndarray:
    """The packed integers of `days` as a store holds them: north-up, one band."""
    return np.stack([day_values(d)[::-1] for d in days])[:, np.newaxis]


def _dates(out: Path) -> list[str]:
    return [str(t)[:10] for t in chronozarr.open_store(out).attrs.times]


def _problems(folder: Path, **options: Any) -> dict[str, str]:
    """What `plan_conversion` reports per file name, as one text each."""
    with pytest.raises(PreflightError) as caught:
        plan_conversion(folder, sample=False, **{**SERIES, **options})
    found: dict[str, list[str]] = {}
    for problem in caught.value.problems:
        found.setdefault(Path(problem.uri).name, []).append(problem.detail)
    return {name: "; ".join(details) for name, details in found.items()}


# --- one dated file per step ------------------------------------------------------------------


def test_a_folder_of_daily_files_converts_as_one_time_series(tmp_path):
    write_series(tmp_path / "days", [0, 1, 2, 3, 4])
    out = tmp_path / "store"
    report = convert(tmp_path / "days", out, **SERIES, **OPTIONS)
    assert report.plan.source.kind == "NetCDF files"
    assert _dates(out) == [f"2020-01-0{d + 1}" for d in range(5)]
    # The stored values stay the packed integers; the band carries scale and offset.
    assert np.array_equal(_stored(out), _expected([0, 1, 2, 3, 4]))
    store = chronozarr.open_store(out)
    band = store.attrs.bands[0]
    assert (band.name, band.scale, band.offset, band.units) == ("sst", 0.01, 0.0, "Celsius")
    assert store.attrs.nodata == FILL
    assert chronozarr.validate(out) == []


def test_files_are_ordered_by_their_time_not_their_name(tmp_path):
    folder = tmp_path / "days"
    folder.mkdir()
    # The names sort the other way round: a.nc is the last day.
    write_days(folder / "a.nc", [3])
    write_days(folder / "b.nc", [2])
    write_days(folder / "c.nc", [1])
    out = tmp_path / "store"
    convert(folder, out, **SERIES, **OPTIONS)
    assert _dates(out) == ["2020-01-02", "2020-01-03", "2020-01-04"]
    assert np.array_equal(_stored(out), _expected([1, 2, 3]))


def test_a_recursive_glob_finds_the_files_in_subfolders(tmp_path):
    write_series(tmp_path / "raw" / "202001", [0, 1])
    write_series(tmp_path / "raw" / "202002", [31, 32])
    out = tmp_path / "store"
    report = convert(str(tmp_path / "raw" / "**" / "*.nc"), out, **SERIES, **OPTIONS)
    assert report.plan.n_time == 4
    assert _dates(out) == ["2020-01-01", "2020-01-02", "2020-02-01", "2020-02-02"]
    # A folder is not searched recursively: the one above holds no files of its own.
    with pytest.raises(ValueError, match="no raster files found"):
        plan_conversion(tmp_path / "raw", sample=False, **SERIES)


def test_a_glob_that_matches_one_file_is_a_series_of_one(tmp_path):
    write_series(tmp_path / "days", [0, 1])
    out = tmp_path / "store"
    report = convert(str(tmp_path / "days" / "*0.nc"), out, **SERIES, **OPTIONS)
    assert report.plan.source.kind == "NetCDF files"
    assert np.array_equal(_stored(out), _expected([0]))


def test_files_that_hold_several_steps_each_are_joined_in_time_order(tmp_path):
    folder = tmp_path / "months"
    folder.mkdir()
    write_days(folder / "feb.nc", [31, 32, 33])
    write_days(folder / "jan.nc", [0, 1, 2])
    out = tmp_path / "store"
    convert(folder, out, **SERIES, **OPTIONS)
    assert np.array_equal(_stored(out), _expected([0, 1, 2, 31, 32, 33]))


def test_the_dry_run_lists_the_files_in_time_order_and_names_the_squeezed_dimension(tmp_path):
    folder = tmp_path / "days"
    folder.mkdir()
    write_days(folder / "late.nc", [1])
    write_days(folder / "early.nc", [0])
    args = ["convert", str(folder), str(tmp_path / "store"), "--variable", "sst"]
    result = CliRunner().invoke(main, [*args, "--crs", "EPSG:4326", "--dry-run"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == "source:     NetCDF files, 2 timesteps (2020-01-01 .. 2020-01-02)"
    assert any(line.startswith("files:      2 NetCDF files") for line in lines)
    listed = [line for line in lines if line.startswith("  2020-")]
    assert [Path(line.split()[-1]).name for line in listed] == ["early.nc", "late.nc"]
    assert "squeezed:   zlev (length 1, value 0 meters)" in lines
    assert not (tmp_path / "store").exists()


def test_the_listing_is_only_for_the_dry_run(tmp_path):
    write_series(tmp_path / "days", [0, 1])
    args = ["convert", str(tmp_path / "days"), str(tmp_path / "store"), "--variable", "sst"]
    result = CliRunner().invoke(main, [*args, "--crs", "EPSG:4326", "--chunk-size", "16"])
    assert result.exit_code == 0, result.output
    assert any(
        line.startswith("files:      2 NetCDF files") for line in result.output.splitlines()
    )
    assert not any(line.startswith("  2020-") for line in result.output.splitlines())


# --- files that cannot be one series -------------------------------------------------------------


def test_two_files_with_the_same_time_are_refused_and_named(tmp_path):
    folder = tmp_path / "days"
    folder.mkdir()
    write_days(folder / "one.nc", [0])
    write_days(folder / "copy_of_one.nc", [0])
    write_days(folder / "three.nc", [1])
    found = _problems(folder)
    assert list(found) == ["one.nc"]
    assert "overlap" in found["one.nc"] and "copy_of_one.nc" in found["one.nc"]


def test_files_whose_time_spans_interleave_are_refused_and_named(tmp_path):
    folder = tmp_path / "months"
    folder.mkdir()
    write_days(folder / "a.nc", [0, 2, 4])
    write_days(folder / "b.nc", [1, 3, 5])
    found = _problems(folder)
    assert list(found) == ["b.nc"]
    assert "overlap" in found["b.nc"] and "a.nc" in found["b.nc"]


def test_a_file_that_repeats_a_time_is_refused(tmp_path):
    folder = tmp_path / "days"
    folder.mkdir()
    write_days(folder / "a.nc", [0, 1])
    write_days(folder / "b.nc", [2, 2])
    assert "duplicate" in _problems(folder)["b.nc"]


def test_differences_are_reported_per_file_with_the_overlaps(tmp_path):
    folder = tmp_path / "days"
    folder.mkdir()
    write_days(folder / "d0.nc", [0])
    write_days(folder / "d1.nc", [1], lon0=100.375)
    write_days(folder / "d2.nc", [2])
    write_days(folder / "d3.nc", [3], scale=0.1)
    write_days(folder / "d4.nc", [4], width=9)
    write_days(folder / "d5.nc", [2])
    with pytest.raises(PreflightError) as caught:
        plan_conversion(folder, sample=False, **SERIES)
    error = caught.value
    by_file = {Path(p.uri).name: p.detail for p in error.problems}
    assert list(by_file) == ["d1.nc", "d3.nc", "d4.nc", "d5.nc"]  # in file order
    assert "another grid" in by_file["d1.nc"] and "d0.nc" in by_file["d1.nc"]
    assert "x runs 100.375" in by_file["d1.nc"]
    assert "0.1" in by_file["d3.nc"] and "0.01" in by_file["d3.nc"] and "scale" in by_file["d3.nc"]
    assert "x has 9 values, not 8" in by_file["d4.nc"]
    assert "overlap" in by_file["d5.nc"] and "d2.nc" in by_file["d5.nc"]
    assert error.n_sources == 6
    assert str(error).startswith("4 problem(s) in 4 of 6 source file(s); nothing was written:")
    assert "fix: " in str(error)


def test_a_file_without_the_variable_or_with_another_type_is_reported(tmp_path):
    folder = tmp_path / "days"
    folder.mkdir()
    write_days(folder / "d0.nc", [0])
    write_days(folder / "d1.nc", [1], variable="analysed_sst")
    write_days(folder / "d2.nc", [2], dtype="int32")
    found = _problems(folder)
    assert "no variable 'sst'" in found["d1.nc"] and "analysed_sst" in found["d1.nc"]
    assert "int32" in found["d2.nc"] and "int16" in found["d2.nc"]


def test_a_file_that_is_not_netcdf_is_reported_with_its_name(tmp_path):
    folder = tmp_path / "days"
    folder.mkdir()
    write_days(folder / "d0.nc", [0])
    (folder / "d1.nc").write_bytes(b"this is not a NetCDF file")
    found = _problems(folder)
    assert list(found) == ["d1.nc"]


def test_files_that_differ_in_dimensions_or_units_or_bands_are_reported(tmp_path):
    folder = tmp_path / "days"
    folder.mkdir()
    write_days(folder / "d0.nc", [0])
    write_days(folder / "d1.nc", [1], zlev=False)
    write_days(folder / "d2.nc", [2], units="Kelvin")
    found = _problems(folder)
    assert "dimensions" in found["d1.nc"] and "zlev" in found["d1.nc"]
    assert "units 'Kelvin'" in found["d2.nc"]


def test_files_that_declare_different_fill_values_are_refused_unless_nodata_is_given(tmp_path):
    folder = tmp_path / "days"
    folder.mkdir()
    write_days(folder / "d0.nc", [0])
    write_days(folder / "d1.nc", [1], fill=-9999)
    assert "nodata -9999" in _problems(folder)["d1.nc"]
    plan = plan_conversion(folder, sample=False, nodata=-999, **SERIES)
    assert plan.source.info.nodata == -999


def test_a_series_needs_one_variable_to_be_chosen(tmp_path):
    folder = tmp_path / "days"
    folder.mkdir()
    write_days(folder / "d0.nc", [0], also=("err",))
    with pytest.raises(ValueError, match=r"d0.nc has variables \['sst', 'err'\]; choose one"):
        plan_conversion(folder, crs="EPSG:4326", sample=False)


def test_no_matching_files_is_a_clear_error(tmp_path):
    (tmp_path / "days").mkdir()
    with pytest.raises(ValueError, match=r"no NetCDF files found matching '.*nothing\*\.nc'"):
        plan_conversion(str(tmp_path / "days" / "nothing*.nc"), variable="sst", sample=False)


def test_date_pattern_does_not_apply_to_netcdf_files(tmp_path):
    write_series(tmp_path / "days", [0, 1])
    with pytest.raises(ValueError, match="NetCDF files are dated by their time coordinate"):
        plan_conversion(tmp_path / "days", date_pattern="%Y%m%d", sample=False, **SERIES)


def test_a_mask_variable_is_read_from_each_file_in_time_order(tmp_path):
    folder = tmp_path / "days"
    folder.mkdir()
    for name, day in (("c.nc", 0), ("a.nc", 2), ("b.nc", 1)):
        write_days(folder / name, [day], ok=True)
    out = tmp_path / "store"
    convert(folder, out, mask_var="ok", **SERIES, **OPTIONS)
    store = chronozarr.open_store(out)
    assert _dates(out) == ["2020-01-01", "2020-01-02", "2020-01-03"]
    for t, day in enumerate([0, 1, 2]):
        plane = store.read_mask(t)
        assert plane is not None
        assert np.array_equal(plane.astype(bool), day_ok(day)[::-1].astype(bool))


def test_a_file_without_the_mask_variable_is_reported(tmp_path):
    folder = tmp_path / "days"
    folder.mkdir()
    write_days(folder / "d0.nc", [0], ok=True)
    write_days(folder / "d1.nc", [1])
    found = _problems(folder, mask_var="ok")
    assert list(found) == ["d1.nc"]
    assert "mask variable 'ok' is not in" in found["d1.nc"]


def test_resume_reuses_the_steps_that_an_interrupted_run_staged(tmp_path):
    write_series(tmp_path / "days", [0, 1, 2, 3, 4])
    work, out = tmp_path / "work", tmp_path / "store"

    def interrupt(done: int, total: int) -> None:
        if done == 2:
            raise KeyboardInterrupt

    options: dict[str, Any] = {**SERIES, **OPTIONS, "work_dir": work, "read_ahead": 1}
    with pytest.raises(KeyboardInterrupt):
        convert(tmp_path / "days", out, progress=interrupt, **options)
    report = convert(tmp_path / "days", out, resume=True, **options)
    assert (report.n_reused, report.n_staged) == (2, 3)
    assert np.array_equal(_stored(out), _expected([0, 1, 2, 3, 4]))


# --- reading ---------------------------------------------------------------------------------


def test_open_files_are_few_and_a_file_in_use_stays_open(tmp_path):
    files = [str(p) for p in write_series(tmp_path / "days", list(range(5)))]
    cache = _OpenFiles(files, limit=2)
    with cache.use(0) as first, cache.use(1), cache.use(2):
        assert len(cache._open) == 3  # all in use: none may be closed
        assert first["sst"].shape[0] == 1  # still readable
    # Over the limit, an idle file is closed when it is let go: 2, the only idle one at the time.
    assert sorted(cache._open) == [0, 1]
    with cache.use(4):
        pass
    assert sorted(cache._open) == [1, 4]  # then the least recently used one


# --- finding the files ---------------------------------------------------------------------------


def test_find_netcdf_files_in_a_folder_and_a_glob(tmp_path):
    write_series(tmp_path / "days", [1, 0])
    (tmp_path / "days" / "notes.txt").write_text("x")
    expected = ["oisst.20200000.nc", "oisst.20200001.nc"]
    assert [Path(p).name for p in find_netcdf_files(str(tmp_path / "days")) or []] == expected
    glob_text = str(tmp_path / "days" / "*.nc")
    assert [Path(p).name for p in find_netcdf_files(glob_text) or []] == expected


def test_netcdf_suffixes_are_matched_without_regard_to_case(tmp_path):
    folder = tmp_path / "days"
    folder.mkdir()
    write_days(folder / "A.NC", [0])
    write_days(folder / "b.nc4", [1])
    write_days(folder / "c.cdf", [2])
    assert len(find_netcdf_files(str(folder)) or []) == 3


@pytest.mark.parametrize(
    "files",
    [["scene.tif"], ["scene.tif", "day.nc"], ["readme.txt"], []],
    ids=["rasters", "rasters and NetCDF", "neither", "empty"],
)
def test_a_folder_that_holds_rasters_or_nothing_is_not_a_netcdf_series(tmp_path, files):
    folder = tmp_path / "mixed"
    folder.mkdir()
    for name in files:
        (folder / name).write_bytes(b"")
    assert find_netcdf_files(str(folder)) is None


def test_other_kinds_of_source_are_not_a_netcdf_series(tmp_path):
    (tmp_path / "store.zarr").mkdir()
    (tmp_path / "store.zarr" / "zarr.json").write_text("{}")
    write_days(tmp_path / "one.nc", [0])
    for text in (
        str(tmp_path / "store.zarr"),
        str(tmp_path / "one.nc"),
        str(tmp_path / "m.csv"),
        "s3://bucket/prefix/",
        "https://example.org/day.nc",
        str(tmp_path / "missing"),
        str(tmp_path / "*.tif"),
    ):
        assert find_netcdf_files(text) is None, text


# --- memory -----------------------------------------------------------------------------------


def _peak_of_planning(folder: Path) -> int:
    tracemalloc.start()
    try:
        plan_conversion(folder, sample=False, **SERIES)
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_planning_a_series_does_not_keep_every_file_in_memory(tmp_path):
    small, large = tmp_path / "small", tmp_path / "large"
    write_series(small, list(range(20)))
    write_series(large, list(range(160)))
    _peak_of_planning(small)  # imports and caches settle on the first call
    peak_small = _peak_of_planning(small)
    peak_large = _peak_of_planning(large)
    # 8 times the files: the peak is a few open files, not one per file kept.
    assert peak_large < peak_small * 1.5 + 200_000, (peak_small, peak_large)


# --- climate-model calendars --------------------------------------------------------------------


def test_files_on_a_noleap_calendar_are_one_series_with_one_calendar_note(tmp_path):
    pytest.importorskip("cftime")
    folder = tmp_path / "decades"
    folder.mkdir()
    later = [365 * 10 + 14, 365 * 10 + 45]
    write_days(folder / "tas_2010s.nc", later, start="2000-01-01", calendar="noleap")
    write_days(folder / "tas_2000s.nc", [14, 45, 73], start="2000-01-01", calendar="noleap")
    with pytest.raises(ValueError, match="uses the noleap calendar"):
        plan_conversion(folder, sample=False, **SERIES)
    out = tmp_path / "store"
    options: dict[str, Any] = {**SERIES, "calendar_to_gregorian": "date"}
    report = convert(folder, out, **options, **OPTIONS)
    assert list(chronozarr.open_store(out).attrs.times) == [
        "2000-01-15T12:00:00Z",
        "2000-02-15T12:00:00Z",
        "2000-03-15T12:00:00Z",
        "2010-01-15T12:00:00Z",
        "2010-02-15T12:00:00Z",
    ]
    assert report.plan.source.notes[0].startswith("calendar:   noleap calendar")
    provenance = chronozarr.open_store(out).attrs.provenance
    assert provenance is not None and "noleap calendar" in provenance["notes"]


def test_files_on_different_calendars_are_reported(tmp_path):
    pytest.importorskip("cftime")
    folder = tmp_path / "decades"
    folder.mkdir()
    write_days(folder / "a.nc", [14], start="2000-01-01", calendar="noleap")
    write_days(folder / "b.nc", [45], start="2000-01-01", calendar="360_day")
    found = _problems(folder, calendar_to_gregorian="date")
    assert list(found) == ["b.nc"]
    assert "360_day" in found["b.nc"] and "noleap" in found["b.nc"]
