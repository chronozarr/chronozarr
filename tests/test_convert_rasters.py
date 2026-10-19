"""`convert` from rasters that GDAL reads but that are not GeoTIFFs: BIL and FLT with their header
sidecars, ASCII grids, zips that hold one raster, HDF5 subdatasets and HDF4 without a driver."""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from click.testing import CliRunner

import chronozarr
from chronozarr import _convert_discover as discovery
from chronozarr._convert_discover import (
    RASTER_SUFFIXES,
    discover,
    is_discovery_source,
    list_rasters,
)
from chronozarr.cli import main
from chronozarr.convert import PreflightError, convert, plan_conversion, write_manifest
from tests.synthetic import CRS, TRANSFORM, make_truth

pytestmark = pytest.mark.unit

rasterio = pytest.importorskip("rasterio")
from rasterio.transform import Affine  # noqa: E402  (after importorskip)

N_TIME, HEIGHT, WIDTH = 4, 40, 50
DAYS = ["20240101", "20240102", "20240103", "20240104"]
OPTIONS: dict[str, Any] = {"chunk_size": 16}
# The extent of the 40 x 50 px, 10 m grid of TRANSFORM: west, south, east, north.
BOUNDS = (746090.0, 2540040.0, 746590.0, 2540440.0)


@pytest.fixture
def truth() -> np.ndarray:
    """(time, 1, y, x) float32 fields; the declared nodata, -9999, is not among the values."""
    return make_truth(N_TIME, 1, HEIGHT, WIDTH).astype(np.float32)


def stored(path: Path) -> np.ndarray:
    return chronozarr.open_store(path).to_xarray().values


def run(*args: str):
    return CliRunner().invoke(main, list(args), catch_exceptions=False)


def touch(*paths: Path) -> None:
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")


def write_bil(path: Path, field: np.ndarray, *, nodata: float = -9999.0) -> Path:
    """A BIL file with its .hdr, .prj and .aux.xml next to it, as PRISM delivers them."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        path,
        "w",
        driver="EHdr",
        height=field.shape[0],
        width=field.shape[1],
        count=1,
        dtype="float32",
        crs=CRS,
        transform=Affine(*TRANSFORM),
        nodata=nodata,
    ) as dst:
        dst.write(field[np.newaxis])
    path.with_suffix(".info.txt").write_text("provider notes")
    path.with_suffix(".stn.csv").write_text("station,value\n")
    path.with_suffix(".xml").write_text("<metadata/>")
    return path


def bil_series(folder: Path, truth: np.ndarray, suffix: str = ".bil") -> Path:
    for t, day in enumerate(DAYS):
        bil = write_bil(folder / f"prism_ppt_{day}.bil", truth[t, 0])
        if suffix != ".bil":  # a .flt is the same raster with the same .hdr
            bil.rename(bil.with_suffix(suffix))
    return folder


def zip_raster(zip_path: Path, raster: Path) -> Path:
    """A zip of `raster` and every file that shares its name stem, the way PRISM zips a day."""
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for part in sorted(raster.parent.glob(f"{raster.stem}.*")):
            archive.write(part, part.name)
    return zip_path


def write_h5(path: Path, variables: dict[str, np.ndarray]) -> Path:
    h5py = pytest.importorskip("h5py")
    path.parent.mkdir(parents=True, exist_ok=True)
    with h5py.File(path, "w") as handle:
        for name, array in variables.items():
            handle.create_dataset(name, data=array)
    return path


def h5_series(folder: Path, truth: np.ndarray) -> Path:
    """Daily HDF5 files with two groups, as SMAP delivers them: subdatasets and no raster band."""
    for t, day in enumerate(DAYS):
        write_h5(
            folder / f"SMAP_L3_{day}_R1.h5",
            {
                "Data_AM/soil": truth[t, 0],
                "Data_AM/other": truth[t, 0] * 2,
                "Data_PM/soil": truth[t, 0] * 3,
                "plain": truth[t, 0] * 4,
            },
        )
    return folder


# --- what counts as a raster source ---------------------------------------------------------


def test_a_directory_lists_raster_files_of_every_format_and_skips_sidecars(tmp_path):
    raster_names = [f"r{i}{suffix}" for i, suffix in enumerate((*RASTER_SUFFIXES, ".zip"))]
    sidecars = [
        "p_20200101.hdr",
        "p_20200101.prj",
        "p_20200101.bil.aux.xml",
        "p_20200101.stx",
        "p_20200101.xml",
        "p_20200101.info.txt",
        "p_20200101.stn.csv",
        "notes.txt",
        "cube.nc",
        "image.png",
    ]
    touch(*(tmp_path / name for name in [*raster_names, *sidecars]))
    found = [Path(p).name for p in list_rasters(str(tmp_path))]
    assert found == sorted(raster_names)
    for suffix in (".bil", ".flt", ".img", ".grib2", ".grb2", ".grb", ".h5", ".he5", ".hdf"):
        assert suffix in RASTER_SUFFIXES


def test_suffixes_are_matched_without_regard_to_case(tmp_path):
    touch(tmp_path / "A_20240101.BIL", tmp_path / "B_20240102.Grib2", tmp_path / "C_20240103.ZIP")
    assert [Path(p).name for p in list_rasters(str(tmp_path))] == [
        "A_20240101.BIL",
        "B_20240102.Grib2",
        "C_20240103.ZIP",
    ]


def test_a_single_raster_or_zip_is_a_discovery_source_and_netcdf_is_not(tmp_path):
    for name in ("p.bil", "p.flt", "p.grib2", "p.h5", "p.hdf", "p.jp2", "p.vrt", "p.zip"):
        touch(tmp_path / name)
        assert is_discovery_source(str(tmp_path / name)), name
    assert is_discovery_source(str(tmp_path / "*.bil"))
    assert not is_discovery_source(str(tmp_path / "cube.nc"))
    assert not is_discovery_source(str(tmp_path / "days" / "*.nc"))


def test_nothing_found_says_what_was_looked_for(tmp_path):
    touch(tmp_path / "readme.txt")
    pattern = r"no raster files found in .*Looked for \.tif, \.tiff, \.bil.*\.grib2.*\.h5.*\.zip"
    with pytest.raises(ValueError, match=pattern) as error:
        list_rasters(str(tmp_path))
    message = str(error.value)
    assert "quote a glob" in message
    assert "not searched recursively" in message
    assert "manifest" in message
    with pytest.raises(ValueError, match=r"no raster files found matching .*\*\.bil'"):
        list_rasters(str(tmp_path / "*.bil"))


def test_an_s3_prefix_lists_every_raster_format_and_skips_sidecars(monkeypatch):
    keys = ["d/a_20240101.bil", "d/a_20240101.hdr", "d/b_20240102.GRIB2", "d/c.txt", "d/d.zip"]
    monkeypatch.setattr(discovery, "_list_s3_keys", lambda bucket, prefix: keys)
    assert list_rasters("s3://bkt/d/") == [
        "s3://bkt/d/a_20240101.bil",
        "s3://bkt/d/b_20240102.GRIB2",
    ]
    monkeypatch.setattr(discovery, "_list_s3_keys", lambda bucket, prefix: ["d/c.txt"])
    with pytest.raises(ValueError, match=r"no raster files found under s3://bkt/d/.*\.bil"):
        list_rasters("s3://bkt/d/")


# --- BIL and FLT with sidecars --------------------------------------------------------------


@pytest.mark.parametrize("suffix", [".bil", ".flt"])
def test_a_directory_of_bil_or_flt_files_converts_bit_exact(tmp_path, truth, suffix):
    folder = bil_series(tmp_path / "raw", truth, suffix)
    out = tmp_path / "store"
    report = convert(folder, out, **OPTIONS)
    assert report.plan.discovered
    assert report.plan.source.kind == "dated rasters"
    assert np.array_equal(stored(out), truth)
    store = chronozarr.open_store(out)
    assert [str(t)[:10] for t in store.times] == [f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in DAYS]
    assert store.attrs.crs == CRS
    assert chronozarr.validate(out) == []


def test_bil_nodata_comes_from_the_hdr_and_the_plan_names_the_files(tmp_path, truth):
    folder = bil_series(tmp_path / "raw", truth)
    plans = []
    convert(folder, tmp_path / "store", dry_run=True, on_plan=plans.append, **OPTIONS)
    lines = plans[0].lines(list_files=True)
    assert "source:     dated rasters, 4 timesteps (2024-01-01 .. 2024-01-04)" in lines
    assert f"  2024-01-02  {folder / 'prism_ppt_20240102.bil'}" in lines
    assert plans[0].source.info.nodata == -9999
    assert not (tmp_path / "store").exists()


def test_a_series_of_geotiffs_is_still_called_dated_geotiffs(tmp_path, truth):
    folder = tmp_path / "tifs"
    folder.mkdir()
    for t, day in enumerate(DAYS):
        with rasterio.open(
            folder / f"x_{day}.tif",
            "w",
            driver="GTiff",
            height=HEIGHT,
            width=WIDTH,
            count=1,
            dtype="float32",
            crs=CRS,
            transform=Affine(*TRANSFORM),
        ) as dst:
            dst.write(truth[t])
    assert plan_conversion(folder, sample=False).source.kind == "dated GeoTIFFs"


def test_an_ascii_grid_with_its_prj_converts(tmp_path, truth):
    folder = tmp_path / "asc"
    folder.mkdir()
    for t, day in enumerate(DAYS):
        with rasterio.open(
            folder / f"dem_{day}.asc",
            "w",
            driver="AAIGrid",
            height=HEIGHT,
            width=WIDTH,
            count=1,
            dtype="float32",
            crs=CRS,
            transform=Affine(*TRANSFORM),
            nodata=-9999,
        ) as dst:
            dst.write(truth[t])
    out = tmp_path / "store"
    convert(folder, out, **OPTIONS)
    assert chronozarr.open_store(out).attrs.crs == CRS
    assert np.array_equal(stored(out), truth)


def test_write_manifest_lists_the_rasters_and_the_manifest_converts_the_same(tmp_path, truth):
    folder = bil_series(tmp_path / "raw", truth)
    manifest = tmp_path / "found.csv"
    write_manifest(discover(str(folder)).entries, manifest)
    assert manifest.read_text().count(".bil") == N_TIME
    out = tmp_path / "store"
    convert(manifest, out, **OPTIONS)
    assert np.array_equal(stored(out), truth)


def test_encode_points_a_raster_file_at_convert(tmp_path, truth):
    folder = bil_series(tmp_path / "raw", truth)
    result = run("encode", str(folder / "prism_ppt_20240101.bil"), str(tmp_path / "o"))
    assert result.exit_code == 1
    assert "Use `chronozarr convert" in result.output
    assert "GeoTIFF" not in result.output.split("Use `chronozarr convert")[0]


# --- zips that hold one raster --------------------------------------------------------------


@pytest.fixture
def zips(tmp_path, truth) -> Path:
    """One zip per day, each with a .bil and its sidecars, written out of date order."""
    staging = bil_series(tmp_path / "staging", truth)
    folder = tmp_path / "zips"
    for t in (2, 0, 3, 1):
        zip_raster(folder / f"prism_ppt_{DAYS[t]}.zip", staging / f"prism_ppt_{DAYS[t]}.bil")
    return folder


def test_a_directory_of_zips_converts_bit_exact_dated_from_the_zip_name(tmp_path, zips, truth):
    out = tmp_path / "store"
    report = convert(zips, out, **OPTIONS)
    assert report.plan.source.kind == "dated rasters"
    assert np.array_equal(stored(out), truth)
    assert str(chronozarr.open_store(out).times[0])[:10] == "2024-01-01"
    assert chronozarr.validate(out) == []


def test_the_dry_run_names_the_zips_not_a_vsizip_path(tmp_path, zips):
    plans = []
    convert(zips, tmp_path / "store", dry_run=True, on_plan=plans.append, **OPTIONS)
    lines = plans[0].lines(list_files=True)
    assert f"  2024-01-03  {zips / 'prism_ppt_20240103.zip'}" in lines


def test_a_glob_of_zips_and_a_single_zip_work(tmp_path, zips, truth):
    assert plan_conversion(str(zips / "*.zip"), sample=False).n_time == N_TIME
    one = plan_conversion(zips / "prism_ppt_20240102.zip", sample=False)
    assert one.n_time == 1


def test_a_manifest_of_zips_converts(tmp_path, zips, truth):
    manifest = tmp_path / "found.csv"
    write_manifest(discover(str(zips)).entries, manifest)
    assert manifest.read_text().count(".zip") == N_TIME
    out = tmp_path / "store"
    convert(manifest, out, **OPTIONS)
    assert np.array_equal(stored(out), truth)


def test_a_zip_without_a_date_in_its_name_is_dated_from_the_raster_inside(tmp_path, truth):
    staging = bil_series(tmp_path / "staging", truth)
    folder = tmp_path / "zips"
    for t, day in enumerate(DAYS):
        zip_raster(folder / f"download_{t}.zip", staging / f"prism_ppt_{day}.bil")
    found = discover(str(folder))
    assert found.problems == []
    assert [str(e.time)[:10] for e in found.entries] == [f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in DAYS]
    out = tmp_path / "store"
    convert(folder, out, **OPTIONS)
    assert np.array_equal(stored(out), truth)


def test_a_zip_with_no_date_in_its_name_or_its_raster_is_reported(tmp_path, truth):
    undated = write_bil(tmp_path / "staging" / "undated.bil", truth[0, 0])
    zip_path = zip_raster(tmp_path / "zips" / "download.zip", undated)
    found = discover(str(tmp_path / "zips"))
    assert found.undated == [str(zip_path)]
    (problem,) = found.problems
    assert problem.detail == "no date in the name of the zip or of the raster inside it"


def test_a_zip_made_on_a_mac_does_not_count_its_resource_forks(tmp_path, truth):
    staging = bil_series(tmp_path / "staging", truth)
    zip_path = zip_raster(
        tmp_path / "zips" / "prism_ppt_20240101.zip", staging / "prism_ppt_20240101.bil"
    )
    with zipfile.ZipFile(zip_path, "a") as archive:
        archive.writestr("__MACOSX/._prism_ppt_20240101.bil", b"fork")
        archive.writestr("__MACOSX/", b"")
    assert plan_conversion(tmp_path / "zips", sample=False).n_time == 1


def test_a_zip_with_two_rasters_names_them_and_says_what_to_do(tmp_path, truth):
    staging = bil_series(tmp_path / "staging", truth)
    zip_path = tmp_path / "zips" / "prism_ppt_20240101.zip"
    zip_path.parent.mkdir()
    with zipfile.ZipFile(zip_path, "w") as archive:
        for name in ("prism_ppt_20240101", "prism_ppt_20240102"):
            for part in staging.glob(f"{name}.*"):
                archive.write(part, part.name)
    with pytest.raises(PreflightError) as error:
        plan_conversion(tmp_path / "zips", sample=False)
    (problem,) = error.value.problems
    assert problem.uri == str(zip_path)
    assert "holds 2 rasters" in problem.detail
    assert "prism_ppt_20240101.bil" in problem.detail
    assert "prism_ppt_20240102.bil" in problem.detail
    assert "Unzip it" in problem.detail


def test_a_zip_with_no_raster_or_no_zip_data_is_a_problem_with_a_cause(tmp_path):
    folder = tmp_path / "zips"
    folder.mkdir()
    with zipfile.ZipFile(folder / "a_20240101.zip", "w") as archive:
        archive.writestr("readme.txt", "hello")
    (folder / "b_20240102.zip").write_bytes(b"not a zip")
    with pytest.raises(PreflightError) as error:
        plan_conversion(folder, sample=False)
    by_name = {Path(p.uri).name: p.detail for p in error.value.problems}
    assert "holds no raster" in by_name["a_20240101.zip"]
    assert "not a readable zip" in by_name["b_20240102.zip"]


# --- subdatasets: HDF5 and other containers -------------------------------------------------


def test_a_container_without_variable_lists_its_subdatasets_once(tmp_path, truth):
    folder = h5_series(tmp_path / "h5", truth)
    with pytest.raises(ValueError, match="holds 4 subdatasets") as error:
        plan_conversion(folder, sample=False)
    assert not isinstance(error.value, PreflightError)
    message = str(error.value)
    assert "--variable NAME" in message
    assert message.count("SMAP_L3_") == 1  # the list is shown once, not for every file
    assert "Data_AM/" in message
    assert "other, soil" in message
    assert "Data_PM/" in message
    assert "[40x50]" in message
    assert "plain" in message


@pytest.mark.parametrize("name", ["Data_AM/soil", "/Data_AM/soil"])
def test_variable_picks_the_subdataset_and_names_the_band_after_it(tmp_path, truth, name):
    folder = h5_series(tmp_path / "h5", truth)
    out = tmp_path / "store"
    report = convert(folder, out, variable=name, crs=CRS, bounds=BOUNDS, **OPTIONS)
    assert report.plan.source.info.band_names == ("soil",)
    assert np.array_equal(stored(out), truth)
    assert chronozarr.open_store(out).attrs.crs == CRS
    assert chronozarr.validate(out) == []


def test_variable_may_leave_out_the_group_when_the_last_part_is_unique(tmp_path, truth):
    folder = h5_series(tmp_path / "h5", truth)
    out = tmp_path / "store"
    convert(folder, out, variable="other", crs=CRS, bounds=BOUNDS, **OPTIONS)
    assert np.array_equal(stored(out), truth * 2)
    assert chronozarr.open_store(out).bands == ("other",)


def test_variable_that_names_two_subdatasets_lists_both(tmp_path, truth):
    folder = h5_series(tmp_path / "h5", truth)
    with pytest.raises(ValueError, match="matches 2 subdatasets") as error:
        plan_conversion(folder, variable="soil", crs=CRS, bounds=BOUNDS, sample=False)
    assert "Data_AM/soil" in str(error.value)
    assert "Data_PM/soil" in str(error.value)


def test_variable_that_matches_nothing_lists_what_there_is(tmp_path, truth):
    folder = h5_series(tmp_path / "h5", truth)
    with pytest.raises(ValueError, match="matches none of the 4 subdatasets") as error:
        plan_conversion(folder, variable="rain", crs=CRS, bounds=BOUNDS, sample=False)
    assert "Data_AM/" in str(error.value)


def test_a_variable_missing_from_one_file_names_that_file(tmp_path, truth):
    folder = h5_series(tmp_path / "h5", truth)
    write_h5(
        folder / "SMAP_L3_20240105_R1.h5",
        {"Data_AM/other": truth[0, 0], "Data_AM/third": truth[0, 0]},
    )
    with pytest.raises(
        ValueError, match=r"matches none of the 2 subdatasets of .*SMAP_L3_20240105_R1\.h5"
    ) as error:
        plan_conversion(folder, variable="Data_AM/soil", crs=CRS, bounds=BOUNDS, sample=False)
    assert not isinstance(error.value, PreflightError)


def test_subdatasets_without_georeferencing_say_to_pass_bounds_and_crs(tmp_path, truth):
    folder = h5_series(tmp_path / "h5", truth)
    with pytest.raises(PreflightError, match="--bounds west,south,east,north"):
        plan_conversion(folder, variable="Data_AM/soil", sample=False)


def test_variable_with_a_plain_raster_series_is_refused(tmp_path, truth):
    folder = bil_series(tmp_path / "raw", truth)
    with pytest.raises(ValueError, match=r"has no subdatasets.*--variable") as error:
        plan_conversion(folder, variable="soil", sample=False)
    assert not isinstance(error.value, PreflightError)


def test_a_manifest_takes_variable_too(tmp_path, truth):
    folder = h5_series(tmp_path / "h5", truth)
    manifest = tmp_path / "found.csv"
    write_manifest(discover(str(folder)).entries, manifest)
    out = tmp_path / "store"
    convert(manifest, out, variable="Data_PM/soil", crs=CRS, bounds=BOUNDS, **OPTIONS)
    assert np.array_equal(stored(out), truth * 3)


def test_resume_is_refused_for_another_variable(tmp_path, truth):
    folder = h5_series(tmp_path / "h5", truth)
    options: dict[str, Any] = {"crs": CRS, "bounds": BOUNDS, "sample": False}
    one = plan_conversion(folder, variable="Data_AM/soil", **options)
    two = plan_conversion(folder, variable="Data_PM/soil", **options)
    assert one.source.fingerprint() != two.source.fingerprint()


def test_cli_variable_help_covers_subdatasets_and_the_error_is_a_cli_error(tmp_path, truth):
    folder = h5_series(tmp_path / "h5", truth)
    result = run("convert", str(folder), str(tmp_path / "o"), "--dry-run")
    assert result.exit_code == 1
    assert "holds 4 subdatasets" in result.output
    assert "Traceback" not in result.output
    assert "subdataset" in run("convert", "--help").output


def test_cli_converts_a_subdataset_with_crs_and_bounds(tmp_path, truth):
    folder = h5_series(tmp_path / "h5", truth)
    out = tmp_path / "store"
    bounds = ",".join(str(v) for v in BOUNDS)
    result = run(
        "convert", str(folder), str(out), "--variable", "Data_AM/soil", "--crs", CRS,
        "--bounds", bounds, "--chunk-size", "16",
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "source:     dated rasters, 4 timesteps" in result.output
    assert np.array_equal(stored(out), truth)


# --- HDF4 ------------------------------------------------------------------------------------


def hdf4_missing() -> bool:
    with rasterio.Env() as env:
        return "HDF4" not in env.drivers()


@pytest.mark.skipif(not hdf4_missing(), reason="this GDAL has an HDF4 driver")
def test_an_hdf4_file_says_the_driver_is_missing_and_names_a_route(tmp_path):
    folder = tmp_path / "modis"
    folder.mkdir()
    for doy in (1, 32):
        # an HDF4 file starts with these four bytes
        (folder / f"MOD13A3.A2020{doy:03d}.h09v05.061.hdf").write_bytes(
            b"\x0e\x03\x13\x01" + b"0" * 64
        )
    with pytest.raises(PreflightError) as error:
        plan_conversion(folder, date_pattern="MOD13A3.A%Y%j", sample=False)
    assert len(error.value.problems) == 2
    for problem in error.value.problems:
        assert "HDF4 file" in problem.detail
        assert "no HDF4 driver" in problem.detail
        assert problem.fix is not None
        assert "gdal_translate" in problem.fix
        assert "GeoTIFF" in problem.fix


def test_a_file_gdal_cannot_open_keeps_the_generic_remedy(tmp_path):
    folder = tmp_path / "junk"
    folder.mkdir()
    (folder / "x_20240101.bil").write_text("junk")
    with pytest.raises(PreflightError) as error:
        plan_conversion(folder, sample=False)
    (problem,) = error.value.problems
    assert "HDF4" not in problem.detail
    assert problem.fix is not None
    assert "readable raster" in problem.fix
