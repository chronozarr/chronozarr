"""`convert` of a series exported as several tiles per date, as Earth Engine splits an export."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest
from click.testing import CliRunner

import chronozarr
from chronozarr.cli import main
from chronozarr.convert import PreflightError, convert
from tests.synthetic import CRS, TRANSFORM, make_truth

pytestmark = pytest.mark.unit

rasterio = pytest.importorskip("rasterio")
from rasterio.transform import Affine  # noqa: E402  (after importorskip)

N_TIME, HEIGHT, WIDTH = 3, 40, 60
OPTIONS: dict[str, Any] = {"chunk_size": 16}
DAYS = ["2024_01_01", "2024_01_17", "2024_02_02"]
ISO = ["2024-01-01", "2024-01-17", "2024-02-02"]
# Earth Engine names a tile by its row and column offset in the image: name-RRRRRRRRRR-CCCCCCCCCC
QUARTERS = [(0, 0), (0, 30), (20, 0), (20, 30)]


def write_tif(
    path: Path,
    array: np.ndarray,
    *,
    row: int = 0,
    col: int = 0,
    nodata: int | None = None,
    transform=TRANSFORM,
    crs: str = CRS,
) -> Path:
    """A GeoTIFF of `array` (band, y, x), `row` and `col` pixels from the origin of `transform`."""
    a, b, c, d, e, f = transform
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=array.shape[1],
        width=array.shape[2],
        count=array.shape[0],
        dtype=array.dtype,
        crs=crs,
        transform=Affine(a, b, c + col * a, d, e, f + row * e),
        nodata=nodata,
    ) as dst:
        dst.write(array)
        dst.descriptions = ["NDVI", "EVI"][: array.shape[0]]
    return path


def tile_name(t: int, row: int, col: int) -> str:
    return f"NDVI_{DAYS[t]}-{row:010d}-{col:010d}.tif"


def write_tiles(
    folder: Path,
    truth: np.ndarray,
    quarters=QUARTERS,
    *,
    nodata: int | None = None,
    skip: tuple[tuple[int, int, int], ...] = (),
) -> Path:
    """Every timestep of `truth` (time, band, y, x) as one file per quarter, named as Earth Engine
    names the files of a split export; `skip` leaves out (t, row, col) tiles."""
    for t in range(truth.shape[0]):
        for row, col in quarters:
            if (t, row, col) in skip:
                continue
            part = truth[t, :, row : row + HEIGHT // 2, col : col + WIDTH // 2]
            write_tif(folder / tile_name(t, row, col), part, row=row, col=col, nodata=nodata)
    return folder


def run(*args: str):
    return CliRunner().invoke(main, list(args), catch_exceptions=False)


def text_of(error: Exception) -> str:
    return " ".join(str(error).split())


@pytest.fixture
def truth() -> np.ndarray:
    """(time, band, y, x) int16, as an Earth Engine NDVI export; no value is 0."""
    return make_truth(N_TIME, 1, HEIGHT, WIDTH).clip(1, None).astype(np.int16)


# --- without the option ----------------------------------------------------------------------


def test_tiles_of_one_grid_are_refused_with_the_option_that_joins_them(tmp_path, truth):
    folder = write_tiles(tmp_path / "raw", truth)
    out = tmp_path / "store"

    with pytest.raises(PreflightError) as error:
        convert(folder, out, **OPTIONS)

    assert not out.exists()
    problems = error.value.problems
    assert len(problems) == N_TIME * len(QUARTERS)  # one per file: no grid finding besides
    for problem in problems:
        assert "tiles of one 40 x 60 px grid" in problem.detail
        assert problem.fix is not None and "--mosaic-tiles" in problem.fix
    message = text_of(error.value)
    assert "narrow the glob" not in message
    assert "leave one out" not in message
    assert "--resampling" not in message


def test_the_dry_run_names_the_option_for_tiles(tmp_path, truth):
    folder = write_tiles(tmp_path / "raw", truth)

    result = run("convert", str(folder), str(tmp_path / "store"), "--dry-run")

    assert result.exit_code != 0
    assert "pass --mosaic-tiles" in result.output
    assert "narrow the glob" not in result.output


def test_copies_of_a_date_are_refused_as_copies_with_or_without_the_option(tmp_path, truth):
    folder = tmp_path / "raw"
    write_tif(folder / "ndvi_2024-01-01.tif", truth[0])
    write_tif(folder / "ndvi_copy_20240101.tif", truth[0])
    write_tif(folder / "ndvi_2024-01-17.tif", truth[1])

    for mosaic_tiles in (False, True):
        with pytest.raises(PreflightError) as error:
            convert(folder, tmp_path / "store", mosaic_tiles=mosaic_tiles, **OPTIONS)
        by_name = {Path(p.uri).name: p for p in error.value.problems}
        assert set(by_name) == {"ndvi_2024-01-01.tif", "ndvi_copy_20240101.tif"}
        for problem in by_name.values():
            assert "same date 2024-01-01" in problem.detail
            assert "covers the same pixels" in problem.detail
            assert problem.fix is not None and problem.fix.startswith("keep one")
            assert "--mosaic-tiles" not in problem.fix


@pytest.mark.parametrize(
    ("shift", "found"),
    [
        ((0.5, 0.0, 1.0), "off the pixel grid of"),  # half a pixel across
        ((0.0, 0.0, 2.0), "pixels of 20 x 10"),  # another pixel size
    ],
)
def test_files_of_a_date_off_one_pixel_grid_are_not_offered_as_tiles(
    tmp_path, truth, shift, found
):
    folder = tmp_path / "raw"
    left, right = truth[:, :, :, :30], truth[:, :, :, 30:]
    across, _, size = shift
    a, b, c, d, e, f = TRANSFORM
    moved = (a * size, b, c + a * across, d, e, f)
    for t in range(N_TIME):
        write_tif(folder / tile_name(t, 0, 0), left[t])
        write_tif(folder / tile_name(t, 0, 30), right[t], col=30, transform=moved)

    with pytest.raises(PreflightError) as plain:
        convert(folder, tmp_path / "store", **OPTIONS)
    same_date = [p for p in plain.value.problems if "same date" in p.detail]
    assert len(same_date) == 2 * N_TIME
    for problem in same_date:
        assert problem.fix is not None
        assert "--mosaic-tiles" not in problem.fix
        assert "gdalwarp" in problem.fix

    with pytest.raises(PreflightError) as mosaic:
        convert(folder, tmp_path / "store", mosaic_tiles=True, **OPTIONS)
    by_name = {Path(p.uri).name: p for p in mosaic.value.problems}
    assert set(by_name) == {tile_name(t, 0, 30) for t in range(N_TIME)}
    for problem in by_name.values():
        assert found in problem.detail
        assert problem.fix is not None and "gdalwarp" in problem.fix


# --- with the option -------------------------------------------------------------------------


def test_mosaic_tiles_stores_every_date_as_the_whole_mosaic(tmp_path, truth):
    folder = write_tiles(tmp_path / "raw", truth)
    out = tmp_path / "store"

    report = convert(folder, out, mosaic_tiles=True, **OPTIONS)

    assert report.plan.n_time == N_TIME
    store = chronozarr.open_store(out)
    data = store.to_xarray()
    assert [str(t)[:10] for t in data.time.values] == ISO
    assert data.shape == (N_TIME, 1, HEIGHT, WIDTH)
    np.testing.assert_array_equal(data.values, truth)
    a, _, c, _, e, f = TRANSFORM
    assert data.x.values[0] == pytest.approx(c + a / 2)
    assert data.y.values[0] == pytest.approx(f + e / 2)
    assert store.read_mask(0) is None
    assert chronozarr.schema.validate(out) == []


def test_mosaic_dry_run_lists_every_tile_and_its_place(tmp_path, truth):
    folder = write_tiles(tmp_path / "raw", truth)

    result = run("convert", str(folder), str(tmp_path / "store"), "--mosaic-tiles", "--dry-run")

    assert result.exit_code == 0, result.output
    out = result.output
    assert "source:     dated GeoTIFF tiles, 3 timesteps (2024-01-01 .. 2024-02-02)" in out
    assert "12 files are tiles of one 40 x 60 px grid, 4 per date" in out
    assert "every date covers the whole grid" in out
    assert "40 x 60 px" in out.split("grid:")[1].splitlines()[0]
    assert f"2024-01-17  rows 20..39, columns 30..59  {folder / tile_name(1, 20, 30)}" in out
    assert "dry run: nothing was written" in out


def test_files_of_a_date_that_overlap_are_refused_from_their_grids(tmp_path, truth):
    folder = tmp_path / "raw"
    for t in range(N_TIME):
        write_tif(folder / tile_name(t, 0, 0), truth[t, :, :, :35])
        write_tif(folder / tile_name(t, 0, 25), truth[t, :, :, 25:], col=25)  # 10 columns shared

    with pytest.raises(PreflightError) as plain:
        convert(folder, tmp_path / "store", **OPTIONS)
    same_date = [p for p in plain.value.problems if "same date" in p.detail]
    assert len(same_date) == 2 * N_TIME
    for problem in same_date:
        assert problem.fix is not None and "--mosaic-tiles" not in problem.fix
        assert "rule you choose" in problem.fix

    with pytest.raises(PreflightError) as mosaic:
        convert(folder, tmp_path / "store", mosaic_tiles=True, **OPTIONS)
    by_name = {Path(p.uri).name: p for p in mosaic.value.problems}
    assert set(by_name) == {tile_name(t, 0, 25) for t in range(N_TIME)}
    for t in range(N_TIME):
        problem = by_name[tile_name(t, 0, 25)]
        assert f"overlaps {tile_name(t, 0, 0)} by 400 px" in problem.detail  # 40 rows x 10 cols
        assert problem.fix is not None and "rule you choose" in problem.fix


def test_a_date_missing_a_tile_is_invalid_where_no_tile_covers_it(tmp_path, truth):
    folder = write_tiles(tmp_path / "raw", truth, skip=((1, 20, 30),))
    out = tmp_path / "store"

    report = convert(folder, out, mosaic_tiles=True, **OPTIONS)

    validity = report.plan.source.info.validity
    assert validity.startswith("mask (the tiles leave pixels uncovered on 1 of 3 dates)")
    store = chronozarr.open_store(out)
    first = store.read_mask(0)
    assert first is not None and first.all()
    gap = np.ones((HEIGHT, WIDTH), dtype=np.uint8)
    gap[20:, 30:] = 0
    np.testing.assert_array_equal(store.read_mask(1), gap)
    values = store.read(1)
    np.testing.assert_array_equal(values[:, gap == 1], truth[1][:, gap == 1])
    np.testing.assert_array_equal(store.read(2), truth[2])


@pytest.mark.parametrize(
    "options",
    [
        {"resampling": "nearest"},
        {"bounds": (746090.0, 2540040.0, 746690.0, 2540440.0), "crs": CRS},
        {"transform": TRANSFORM, "shape": (HEIGHT, WIDTH), "crs": CRS},
        {"shape": (HEIGHT, WIDTH)},
        {"crs": "EPSG:4326"},  # the tiles are in EPSG:32631, and tiles are not warped
        {"bands_are_dates": True},
        {"write_manifest_to": "found.csv"},
    ],
)
def test_mosaic_tiles_refuses_options_that_do_not_apply(tmp_path, truth, options):
    folder = write_tiles(tmp_path / "raw", truth)
    if "write_manifest_to" in options:
        options = {"write_manifest_to": tmp_path / options["write_manifest_to"]}

    with pytest.raises(ValueError, match="--mosaic-tiles"):
        convert(folder, tmp_path / "store", mosaic_tiles=True, **options, **OPTIONS)


def test_mosaic_tiles_refuses_a_manifest(tmp_path, truth):
    folder = write_tiles(tmp_path / "raw", truth)
    manifest = tmp_path / "m.csv"
    manifest.write_text(f"uri,datetime\n{folder / tile_name(0, 0, 0)},2024-01-01\n")

    with pytest.raises(ValueError, match="--mosaic-tiles"):
        convert(manifest, tmp_path / "store", mosaic_tiles=True, **OPTIONS)


# --- review of PR #133 ------------------------------------------------------------------------


def test_a_missing_tile_holds_the_nodata_of_a_sentinel_store(tmp_path, truth):
    folder = write_tiles(tmp_path / "raw", truth, skip=((1, 20, 30),))
    out = tmp_path / "store"

    report = convert(folder, out, mosaic_tiles=True, nodata=-3000, **OPTIONS)

    assert report.plan.source.info.validity == "nodata -3000 sentinel, no mask"
    store = chronozarr.open_store(out)
    assert store.read_mask(1) is None
    gap = store.read(1)[:, 20:, 30:]
    assert (gap == -3000).all()
    assert np.isnan(store.physical(1)[:, 20:, 30:]).all()
    np.testing.assert_array_equal(store.read(1)[:, :20, :], truth[1][:, :20, :])


def test_tiles_with_several_bands_keep_every_band(tmp_path):
    truth = make_truth(N_TIME, 2, HEIGHT, WIDTH).clip(1, None).astype(np.int16)
    folder = write_tiles(tmp_path / "raw", truth)
    out = tmp_path / "store"

    convert(folder, out, mosaic_tiles=True, **OPTIONS)

    np.testing.assert_array_equal(chronozarr.open_store(out).to_xarray().values, truth)


def test_a_glob_of_tiles_converts_like_the_folder(tmp_path, truth):
    folder = write_tiles(tmp_path / "raw", truth)
    (folder / "notes.txt").write_text("not a raster")
    out = tmp_path / "store"

    convert(str(folder / "NDVI_*.tif"), out, mosaic_tiles=True, **OPTIONS)

    np.testing.assert_array_equal(chronozarr.open_store(out).to_xarray().values, truth)


def test_resume_refuses_tiles_staged_in_other_places(tmp_path, truth):
    folder = write_tiles(tmp_path / "raw", truth)
    out, work = tmp_path / "store", tmp_path / "work"

    class Interrupted(Exception):
        pass

    def stop_after_one(done: int, total: int) -> None:
        if done >= 1:
            raise Interrupted

    with pytest.raises(Interrupted):
        convert(folder, out, mosaic_tiles=True, work_dir=work, progress=stop_after_one, **OPTIONS)
    assert (work / "plan.json").is_file()
    # the same files, dates and grid, with two tiles of every date in each other's place
    for t in range(N_TIME):
        top_left = truth[t, :, :20, :30]
        bottom_right = truth[t, :, 20:, 30:]
        write_tif(folder / tile_name(t, 0, 0), bottom_right, row=20, col=30)
        write_tif(folder / tile_name(t, 20, 30), top_left)

    with pytest.raises(ValueError, match="staged for a different input"):
        convert(folder, out, mosaic_tiles=True, work_dir=work, resume=True, **OPTIONS)


@pytest.mark.parametrize("series", ["tiles with a time of day", "one file per date"])
def test_mosaic_tiles_refuses_files_that_share_no_date(tmp_path, truth, series):
    folder = tmp_path / "raw"
    options: dict[str, Any] = {}
    if series == "tiles with a time of day":
        for t in range(N_TIME):
            for i, (row, col) in enumerate(QUARTERS):
                part = truth[t, :, row : row + 20, col : col + 30]
                name = f"S2_{ISO[t].replace('-', '')}T10{i:02d}00-{row:010d}-{col:010d}.tif"
                write_tif(folder / name, part, row=row, col=col)
        options["date_pattern"] = "S2_%Y%m%dT%H%M%S"
    else:  # two series of one file per date, on different extents of one lattice
        for t in range(N_TIME):
            write_tif(folder / f"west_{ISO[t]}.tif", truth[t, :, :, :30])
        later = ["2024-03-01", "2024-03-17", "2024-04-02"]
        for t in range(N_TIME):
            write_tif(folder / f"east_{later[t]}.tif", truth[t, :, :, 30:], col=30)

    with pytest.raises(ValueError, match="no two of the") as error:
        convert(folder, tmp_path / "store", mosaic_tiles=True, **options, **OPTIONS)
    assert "nothing to join" in text_of(error.value)


def test_a_series_of_tiled_and_whole_dates_converts(tmp_path, truth):
    folder = write_tiles(tmp_path / "raw", truth[:2])
    write_tif(folder / f"NDVI_{DAYS[2]}.tif", truth[2])  # the last date in one file
    out = tmp_path / "store"

    convert(folder, out, mosaic_tiles=True, **OPTIONS)

    np.testing.assert_array_equal(chronozarr.open_store(out).to_xarray().values, truth)


def iso_tile_name(t: int, row: int, col: int) -> str:
    """A tile named with a date that `encode` and `append` read (YYYY-MM-DD)."""
    return f"NDVI_{ISO[t]}-{row:010d}-{col:010d}.tif"


def write_iso_tiles(folder: Path, truth: np.ndarray) -> Path:
    for t in range(truth.shape[0]):
        for row, col in QUARTERS:
            part = truth[t, :, row : row + 20, col : col + 30]
            write_tif(folder / iso_tile_name(t, row, col), part, row=row, col=col)
    return folder


def test_encode_names_the_convert_command_for_tiles(tmp_path, truth):
    folder = write_iso_tiles(tmp_path / "raw", truth)
    pattern = str(folder / "*.tif")

    result = CliRunner().invoke(main, ["encode", pattern, str(tmp_path / "store")])

    assert result.exit_code != 0
    text = " ".join(result.output.split())
    assert "tiles of one 40 x 60 px grid" in text
    assert f"chronozarr convert '{pattern}'" in text and "--mosaic-tiles" in text
    assert "pass --mosaic-tiles" not in text


def test_encode_does_not_offer_convert_options_for_copies(tmp_path, truth):
    folder = tmp_path / "raw"
    write_tif(folder / "ndvi_2024-01-01.tif", truth[0])
    write_tif(folder / "ndvi_copy_2024-01-01.tif", truth[0])

    result = CliRunner().invoke(main, ["encode", str(folder / "*.tif"), str(tmp_path / "s")])

    assert result.exit_code != 0
    text = " ".join(result.output.split())
    assert "covers the same pixels" in text
    assert "--date-pattern" not in text and "manifest" not in text


def test_append_names_the_convert_command_for_tiles(tmp_path, truth):
    store = tmp_path / "store"
    convert(write_tiles(tmp_path / "first", truth[:1]), store, mosaic_tiles=True, **OPTIONS)
    folder = write_iso_tiles(tmp_path / "raw", truth)
    pattern = str(folder / "*.tif")

    result = CliRunner().invoke(main, ["append", str(store), pattern])

    assert result.exit_code != 0
    text = " ".join(result.output.split())
    assert f"chronozarr convert '{pattern}' NEW_STORE --mosaic-tiles" in text
    assert f"chronozarr append {store} NEW_STORE" in text
    assert "pass --mosaic-tiles" not in text
