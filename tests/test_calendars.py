"""`convert` and `encode` on NetCDF and Zarr time series on CF calendars that are not Gregorian.

A store holds Gregorian timestamps (spec 2.2). The commands map a cftime axis onto them only when
asked (`--calendar-to-gregorian`), keep each step's calendar date and time of day, stop at a date
Gregorian lacks unless told to move it to the end of its month, and record all of it in the
store's provenance. The mapping rules are in test_calendar_mapping.py.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import xarray as xr
from click.testing import CliRunner

import chronozarr
from chronozarr.cli import main
from chronozarr.convert import convert, plan_conversion

pytestmark = pytest.mark.unit

cftime = pytest.importorskip("cftime")
pytest.importorskip("rasterio")

OPTIONS: dict[str, Any] = {"chunk_size": 16}
CRS = "EPSG:32631"
HEIGHT, WIDTH = 20, 24
MID_MONTHS = [15.0 + 30 * i for i in range(15)]  # 360_day: 2000-01-16, 2000-02-16, ... 2001-03-16


def _values(n_time: int) -> np.ndarray:
    rng = np.random.default_rng(3)
    return rng.integers(1, 60000, size=(n_time, HEIGHT, WIDTH), dtype=np.uint16)


def _source(offsets: list[float], calendar: str) -> xr.Dataset:
    """A (time, y, x) uint16 dataset whose time axis is stored as CF numbers, not decoded."""
    time = ("time", offsets, {"units": "days since 2000-01-01", "calendar": calendar})
    return xr.Dataset(
        {"tas": (("time", "y", "x"), _values(len(offsets)), {"crs": CRS, "units": "K"})},
        coords={
            "time": time,
            "y": 4_000_000.0 - 10.0 * np.arange(HEIGHT),
            "x": 500_000.0 + 10.0 * np.arange(WIDTH),
        },
    )


def _band_source(offsets: list[float], calendar: str) -> xr.Dataset:
    """(time, band, y, x) as `encode` reads it."""
    ds = _source(offsets, calendar)
    return xr.Dataset({"tas": ds["tas"].expand_dims(band=["tas"], axis=1)})


def _write_zarr(path, ds: xr.Dataset) -> str:
    ds.to_zarr(path, zarr_format=2, consolidated=True, encoding={"tas": {"_FillValue": None}})
    return str(path)


def _write_netcdf(path, ds: xr.Dataset) -> str:
    pytest.importorskip("h5netcdf")
    ds.to_netcdf(path, engine="h5netcdf", encoding={"tas": {"_FillValue": None}})
    return str(path)


def _stored_times(store) -> list[str]:
    return list(chronozarr.open_store(store).attrs.times)


def _run(*args: str):
    return CliRunner().invoke(main, list(args))


def test_convert_refuses_a_360_day_netcdf_and_names_the_choice(tmp_path):
    source = _write_netcdf(tmp_path / "model.nc", _source(MID_MONTHS, "360_day"))
    result = _run("convert", source, str(tmp_path / "out"), "--variable", "tas", "--dry-run")
    assert result.exit_code == 1
    assert "uses the 360_day calendar" in result.output
    assert "--calendar-to-gregorian date" in result.output
    assert "object, not datetimes" not in result.output
    assert not (tmp_path / "out").exists()


def test_the_dry_run_plan_says_what_the_option_does_to_the_dates(tmp_path):
    source = _write_netcdf(tmp_path / "model.nc", _source(MID_MONTHS, "360_day"))
    result = _run(
        "convert", source, str(tmp_path / "out"), "--variable", "tas",
        "--calendar-to-gregorian", "date", "--dry-run",
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    line = next(line for line in result.output.splitlines() if line.startswith("calendar:"))
    assert "360_day calendar" in line
    assert "30 days apart in the source are 28 to 31 days apart in the store" in line
    assert "none moved" in line
    assert "(2000-01-16 .. 2001-03-16)" in result.output
    assert not (tmp_path / "out").exists()


def test_convert_a_360_day_netcdf_writes_gregorian_dates_and_records_the_calendar(tmp_path):
    ds = _source(MID_MONTHS, "360_day")
    source = _write_netcdf(tmp_path / "model.nc", ds)
    out = tmp_path / "out"
    report = convert(source, out, variable="tas", calendar_to_gregorian="date", **OPTIONS)
    assert report.plan.source.notes[0].startswith("calendar:   360_day calendar")
    store = chronozarr.open_store(out)
    assert _stored_times(out)[:3] == [
        "2000-01-16T00:00:00Z",
        "2000-02-16T00:00:00Z",
        "2000-03-16T00:00:00Z",
    ]
    assert len(store.times) == len(MID_MONTHS)
    assert np.array_equal(store.to_xarray().values[:, 0], ds["tas"].values)
    provenance = store.attrs.provenance
    assert provenance is not None
    assert provenance["sources"] == ["model.nc"]
    assert "360_day calendar" in provenance["notes"]
    assert "--calendar-to-gregorian date" in provenance["notes"]
    assert chronozarr.validate(out) == []
    info = _run("info", str(out))
    assert info.exit_code == 0, info.output
    assert "notes:     Time axis: 360_day calendar, converted to Gregorian" in info.output


def test_convert_a_noleap_zarr_keeps_the_half_day(tmp_path):
    # noleap, days since 2000-01-01: Jan 15 12:00, Feb 13 00:00, Mar 15 12:00
    source = _write_zarr(tmp_path / "model.zarr", _source([14.5, 43.0, 73.5], "noleap"))
    out = tmp_path / "out"
    convert(source, out, variable="tas", calendar_to_gregorian="date", **OPTIONS)
    assert _stored_times(out) == [
        "2000-01-15T12:00:00Z",
        "2000-02-13T00:00:00Z",
        "2000-03-15T12:00:00Z",
    ]
    assert chronozarr.validate(out) == []


def test_the_calendar_is_named_as_the_source_wrote_it(tmp_path):
    source = _write_zarr(tmp_path / "model.zarr", _source([14.5, 43.0], "365_day"))
    result = _run("convert", source, str(tmp_path / "out"), "--variable", "tas", "--dry-run")
    assert result.exit_code == 1
    assert "uses the 365_day calendar" in result.output


def test_a_missing_date_stops_convert_until_month_end_is_chosen(tmp_path):
    offsets = [29.0, 59.0, 89.0, 419.0]  # 360_day: Jan 30, Feb 30, Mar 30 of 2000; Feb 30 of 2001
    source = _write_netcdf(tmp_path / "daily.nc", _source(offsets, "360_day"))
    out = tmp_path / "out"
    with pytest.raises(ValueError, match="2 of 4 dates that Gregorian does not have"):
        convert(source, out, variable="tas", calendar_to_gregorian="date", **OPTIONS)
    assert not out.exists()
    convert(source, out, variable="tas", calendar_to_gregorian="date-or-month-end", **OPTIONS)
    assert _stored_times(out) == [
        "2000-01-30T00:00:00Z",
        "2000-02-29T00:00:00Z",
        "2000-03-30T00:00:00Z",
        "2001-02-28T00:00:00Z",
    ]
    provenance = chronozarr.open_store(out).attrs.provenance
    assert provenance is not None
    assert "2 of 4 dates do not exist in Gregorian" in provenance["notes"]
    assert "2000-02-30 to 2000-02-29" in provenance["notes"]
    assert "2001-02-30 to 2001-02-28" in provenance["notes"]
    assert chronozarr.validate(out) == []


def test_a_gregorian_axis_is_unchanged_and_the_option_is_ignored(tmp_path):
    source = _write_zarr(
        tmp_path / "model.zarr", _source([0.0, 31.0, 60.0], "proleptic_gregorian")
    )
    out = tmp_path / "out"
    report = convert(source, out, variable="tas", calendar_to_gregorian="date", **OPTIONS)
    assert report.plan.source.notes == ()
    assert chronozarr.open_store(out).attrs.provenance is None
    assert _stored_times(out)[2] == "2000-03-01T00:00:00Z"


def test_the_option_belongs_to_zarr_and_netcdf_input(tmp_path):
    manifest = tmp_path / "m.csv"
    manifest.write_text("uri,datetime\na.tif,2024-01-01\n")
    with pytest.raises(ValueError, match="--calendar-to-gregorian apply to Zarr and NetCDF"):
        plan_conversion(manifest, calendar_to_gregorian="date", sample=False)
    with pytest.raises(ValueError, match="calendar_to_gregorian must be one of"):
        plan_conversion(manifest, calendar_to_gregorian="nearest", sample=False)


def test_convert_adds_the_calendar_to_the_provenance_the_caller_passes(tmp_path):
    source = _write_zarr(tmp_path / "model.zarr", _source([14.5, 43.0], "noleap"))
    out = tmp_path / "out"
    mine = {"sources": ["CMIP6 CESM2"], "composite": "monthly mean", "gap_fill": "none"}
    convert(source, out, variable="tas", calendar_to_gregorian="date", provenance=mine, **OPTIONS)
    provenance = chronozarr.open_store(out).attrs.provenance
    assert provenance is not None
    assert provenance["sources"] == ["CMIP6 CESM2"]
    assert provenance["composite"] == "monthly mean"
    assert provenance["notes"].startswith("Time axis: noleap calendar")


def test_encode_takes_the_option_and_records_the_calendar(tmp_path):
    source = _write_zarr(tmp_path / "model.zarr", _band_source(MID_MONTHS[:4], "360_day"))
    refused = _run("encode", source, str(tmp_path / "a"), "--variable", "tas", "--crs", CRS)
    assert refused.exit_code == 1
    assert "--calendar-to-gregorian date" in refused.output
    out = tmp_path / "b"
    done = _run(
        "encode", source, str(out), "--variable", "tas", "--crs", CRS,
        "--calendar-to-gregorian", "date", "--chunk-size", "16",
    )  # fmt: skip
    assert done.exit_code == 0, done.output
    assert _stored_times(out)[:2] == ["2000-01-16T00:00:00Z", "2000-02-16T00:00:00Z"]
    provenance = chronozarr.open_store(out).attrs.provenance
    assert provenance is not None
    assert provenance["sources"] == ["model.zarr"]
    assert "360_day calendar" in provenance["notes"]
    assert chronozarr.validate(out) == []


def test_append_refuses_a_calendar_it_cannot_record(tmp_path):
    source = _write_zarr(tmp_path / "model.zarr", _band_source(MID_MONTHS[:2], "360_day"))
    result = _run("append", str(tmp_path / "store"), source, "--variable", "tas")
    assert result.exit_code == 1
    assert "does not convert calendars" in result.output
    assert "chronozarr convert ... --calendar-to-gregorian date" in result.output


def test_python_encode_of_a_cftime_axis_points_at_the_commands():
    times = np.array([cftime.Datetime360Day(2000, 1, 16), cftime.Datetime360Day(2000, 2, 16)])
    da = xr.DataArray(
        _values(2)[:, None],
        dims=("time", "band", "y", "x"),
        coords={"time": times, "band": ["tas"]},
        attrs={"crs": CRS, "transform": (10.0, 0.0, 500000.0, 0.0, -10.0, 4000000.0)},
    )
    with pytest.raises(
        ValueError, match=r"cftime dates on the 360_day calendar.*--calendar-to-gregorian"
    ):
        chronozarr.encode(da, "unused", chunk_size=16)


def test_the_netcdf_extra_brings_cftime_and_a_zarr_user_has_an_extra_of_their_own():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    extras = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"][
        "optional-dependencies"
    ]
    assert any(spec.startswith("cftime") for spec in extras["netcdf"])
    assert [spec.split(">")[0] for spec in extras["cftime"]] == ["cftime"]
