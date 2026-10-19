"""`convert` of one raster whose bands are dates, as Earth Engine's `toBands()` exports it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest
from click.testing import CliRunner

import chronozarr
from chronozarr.cli import main
from chronozarr.convert import PreflightError, convert, plan_conversion
from tests.synthetic import CRS, TRANSFORM, make_truth

pytestmark = pytest.mark.unit

rasterio = pytest.importorskip("rasterio")
from rasterio.transform import Affine  # noqa: E402  (after importorskip)

N_TIME, N_VARIABLE, HEIGHT, WIDTH = 4, 2, 40, 50
OPTIONS: dict[str, Any] = {"chunk_size": 16}
DAYS = ["2024_01_01", "2024_01_17", "2024_02_02", "2024_02_18"]
ISO = ["2024-01-01", "2024-01-17", "2024-02-02", "2024-02-18"]
VARIABLES = ("NDVI", "EVI")


def write_bands(
    path: Path,
    array: np.ndarray,
    descriptions: list[str],
    *,
    nodata: int | None = 0,
    scales: list[float] | None = None,
    crs: str = CRS,
    transform=TRANSFORM,
    interleave: str = "pixel",
) -> Path:
    """A GeoTIFF with the bands of `array` (band, y, x), described as `descriptions`."""
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
        transform=Affine(*transform),
        nodata=nodata,
        interleave=interleave,
    ) as dst:
        dst.write(array)
        dst.descriptions = descriptions
        if scales:
            dst.scales = scales
    return path


def run(*args: str):
    return CliRunner().invoke(main, list(args), catch_exceptions=False)


def stored(path: Path) -> np.ndarray:
    return chronozarr.open_store(path).to_xarray().values


def text_of(error: PreflightError) -> str:
    return " ".join(str(error).split())


@pytest.fixture
def truth() -> np.ndarray:
    """(time, variable, y, x): variable 0 is NDVI, variable 1 is EVI."""
    return make_truth(N_TIME, N_VARIABLE, HEIGHT, WIDTH)


@pytest.fixture
def export(tmp_path, truth) -> Path:
    """One band per date, named as toBands() names them: <image id>_<band>."""
    return write_bands(tmp_path / "ndvi.tif", truth[:, 0], [f"{d}_NDVI" for d in DAYS])


# --- one variable per date ------------------------------------------------------------------


def test_bands_that_are_dates_convert_to_a_series_and_are_bit_exact(tmp_path, export, truth):
    out = tmp_path / "store"
    report = convert(export, out, bands_are_dates=True, **OPTIONS)
    assert report.plan.source.kind == "GeoTIFF with a date per band"
    assert np.array_equal(stored(out), truth[:, :1])
    store = chronozarr.open_store(out)
    assert [str(t)[:10] for t in store.times] == ISO
    assert list(store.to_xarray().coords["band"].values) == ["NDVI"]
    assert chronozarr.validate(out) == []


def test_bands_out_of_date_order_become_timesteps_in_date_order(tmp_path, truth):
    order = [2, 0, 3, 1]
    path = write_bands(
        tmp_path / "shuffled.tif", truth[order, 0], [f"{DAYS[t]}_NDVI" for t in order]
    )
    out = tmp_path / "store"
    convert(path, out, bands_are_dates=True, **OPTIONS)
    assert np.array_equal(stored(out), truth[:, :1])
    assert [str(t)[:10] for t in chronozarr.open_store(out).times] == ISO


@pytest.mark.parametrize(
    "form", ["{d}_NDVI", "NDVI_{d}", "{d}", "S2 {d} B4"], ids=["prefix", "suffix", "bare", "words"]
)
def test_the_date_may_sit_anywhere_in_the_description(tmp_path, truth, form):
    iso = [d.replace("_", "-") for d in DAYS]
    path = write_bands(tmp_path / "f.tif", truth[:, 0], [form.format(d=d) for d in iso])
    plan = plan_conversion(path, bands_are_dates=True, sample=False)
    assert [str(t)[:10] for t in plan.source.times] == ISO
    shared = {"{d}_NDVI": "NDVI", "NDVI_{d}": "NDVI", "{d}": "1", "S2 {d} B4": "S2 B4"}[form]
    assert plan.source.info.band_names == (shared,)


def test_the_dry_run_says_how_many_bands_were_read_as_how_many_dates(tmp_path, export):
    plan = plan_conversion(export, bands_are_dates=True, sample=False)
    (note,) = [line for line in plan.lines() if line.startswith("bands:")]
    assert "4 bands read as 4 dates from their descriptions" in note
    assert "'2024_01_01_NDVI'" in note
    assert "'2024_02_18_NDVI'" in note
    assert "the band is named 'NDVI'" in note
    assert not any(line.startswith("files:") for line in plan.lines())
    listed = plan.lines(list_files=True)
    assert "files:      1, a band per date" in listed
    assert "  2024-01-17  band 2  2024_01_17_NDVI" in listed


def test_descriptions_that_hold_only_the_date_name_the_band_by_its_index(tmp_path, truth):
    path = write_bands(tmp_path / "bare.tif", truth[:, 0], DAYS)
    plan = plan_conversion(path, bands_are_dates=True, sample=False)
    assert plan.source.info.band_names == ("1",)
    (note,) = [line for line in plan.lines() if line.startswith("bands:")]
    assert "nothing but the date" in note


def test_scale_offset_and_nodata_come_from_the_file(tmp_path, truth):
    path = write_bands(
        tmp_path / "scaled.tif", truth[:, 0], [f"{d}_NDVI" for d in DAYS], scales=[0.5] * N_TIME
    )
    out = tmp_path / "store"
    report = convert(path, out, bands_are_dates=True, **OPTIONS)
    store = chronozarr.open_store(out)
    assert [(b.scale, b.offset) for b in store.attrs.bands] == [(0.5, 0.0)]
    assert store.attrs.nodata == 0
    assert report.plan.source.info.nodata == 0


def test_a_scale_that_differs_between_dates_is_refused_and_names_the_bands(tmp_path, truth):
    path = write_bands(
        tmp_path / "odd.tif",
        truth[:, 0],
        [f"{d}_NDVI" for d in DAYS],
        scales=[1.0, 1.0, 0.5, 1.0],
    )
    with pytest.raises(PreflightError, match="scales") as error:
        plan_conversion(path, bands_are_dates=True, sample=False)
    assert "2024_02_02_NDVI" in text_of(error.value)
    assert "2024_01_01_NDVI" in text_of(error.value)


def test_a_series_in_another_crs_is_warped_like_the_files_of_a_folder(tmp_path, export, truth):
    folder = tmp_path / "files"
    for d, array in zip(DAYS, truth[:, 0], strict=True):
        write_bands(folder / f"ndvi_{d}.tif", array[None], ["NDVI"])
    options: dict[str, Any] = {"crs": "EPSG:32632", "resampling": "nearest", **OPTIONS}
    from_bands, from_files = tmp_path / "a", tmp_path / "b"
    convert(export, from_bands, bands_are_dates=True, **options)
    convert(folder, from_files, **options)
    assert np.array_equal(stored(from_bands), stored(from_files))


def test_a_pixel_interleaved_file_says_that_each_date_decodes_every_band(
    tmp_path, truth, monkeypatch
):
    descriptions = [f"{d}_NDVI" for d in DAYS]
    pixel = write_bands(tmp_path / "pixel.tif", truth[:, 0], descriptions)
    planar = write_bands(tmp_path / "planar.tif", truth[:, 0], descriptions, interleave="band")
    small = plan_conversion(pixel, bands_are_dates=True, sample=False)
    assert not any(line.startswith("interleave:") for line in small.lines())

    monkeypatch.setattr("chronozarr._convert_cog.PIXEL_INTERLEAVE_NOTE_BYTES", 1)
    (note,) = [
        line
        for line in plan_conversion(pixel, bands_are_dates=True, sample=False).lines()
        if line.startswith("interleave:")
    ]
    assert "pixel-interleaved" in note
    assert "each date decodes all 4 bands" in note
    assert "INTERLEAVE=BAND" in note
    planned = plan_conversion(planar, bands_are_dates=True, sample=False)
    assert not any(line.startswith("interleave:") for line in planned.lines())


def test_resume_fingerprints_tell_apart_a_file_whose_bands_were_reordered(tmp_path, truth):
    order = [1, 0, 2, 3]
    path = write_bands(tmp_path / "a.tif", truth[:, 0], [f"{d}_NDVI" for d in DAYS])
    first = plan_conversion(path, bands_are_dates=True, sample=False).source.fingerprint()
    write_bands(path, truth[order, 0], [f"{DAYS[t]}_NDVI" for t in order])
    second = plan_conversion(path, bands_are_dates=True, sample=False).source.fingerprint()
    assert first != second


# --- several variables per date -------------------------------------------------------------


@pytest.mark.parametrize("layout", ["by date", "by variable"])
def test_several_variables_per_date_become_the_bands_of_each_timestep(tmp_path, truth, layout):
    if layout == "by date":  # toBands() of images that each hold two bands
        array = truth.reshape(N_TIME * N_VARIABLE, HEIGHT, WIDTH)
        descriptions = [f"{d}_{v}" for d in DAYS for v in VARIABLES]
    else:  # two toBands() exports joined
        array = np.concatenate([truth[:, 0], truth[:, 1]])
        descriptions = [f"{d}_{v}" for v in VARIABLES for d in DAYS]
    path = write_bands(tmp_path / "two.tif", array, descriptions)
    out = tmp_path / "store"
    plan = plan_conversion(path, bands_are_dates=True, sample=False)
    (note,) = [line for line in plan.lines() if line.startswith("bands:")]
    assert "8 bands read as 4 dates from their descriptions" in note
    assert "NDVI" in note and "EVI" in note
    convert(path, out, bands_are_dates=True, **OPTIONS)
    assert np.array_equal(stored(out), truth)
    assert list(chronozarr.open_store(out).to_xarray().coords["band"].values) == list(VARIABLES)
    assert [str(t)[:10] for t in chronozarr.open_store(out).times] == ISO


def test_a_date_that_lacks_a_variable_is_refused_and_says_which(tmp_path, truth):
    descriptions = [f"{d}_{v}" for d in DAYS for v in VARIABLES]
    array = truth.reshape(N_TIME * N_VARIABLE, HEIGHT, WIDTH)
    keep = [i for i in range(len(descriptions)) if descriptions[i] != "2024_02_02_EVI"]
    path = write_bands(tmp_path / "gap.tif", array[keep], [descriptions[i] for i in keep])
    with pytest.raises(PreflightError) as error:
        plan_conversion(path, bands_are_dates=True, sample=False)
    text = text_of(error.value)
    assert "do not all have the same bands" in text
    assert "2024-02-02 has NDVI" in text
    assert "EVI" in text


# --- a date pattern for the descriptions ----------------------------------------------------


def test_a_date_pattern_reads_the_descriptions_the_default_forms_miss(tmp_path, truth):
    path = write_bands(
        tmp_path / "doy.tif", truth[:, 0], [f"A2020{doy:03d}_NDVI" for doy in (1, 17, 33, 49)]
    )
    with pytest.raises(PreflightError, match="hold no date"):
        plan_conversion(path, bands_are_dates=True, sample=False)
    out = tmp_path / "store"
    convert(path, out, bands_are_dates=True, date_pattern="A%Y%j", **OPTIONS)
    assert [str(t)[:10] for t in chronozarr.open_store(out).times] == [
        "2020-01-01",
        "2020-01-17",
        "2020-02-02",
        "2020-02-18",
    ]
    assert np.array_equal(stored(out), truth[:, :1])
    assert chronozarr.open_store(out).bands == ("NDVI",)


def test_a_date_pattern_reports_a_match_that_is_not_a_real_date(tmp_path, truth):
    path = write_bands(
        tmp_path / "bad.tif", truth[:, 0], [f"A2020{doy:03d}_NDVI" for doy in (1, 17, 999, 49)]
    )
    with pytest.raises(PreflightError, match="not a real date"):
        plan_conversion(path, bands_are_dates=True, date_pattern="A%Y%j", sample=False)


# --- what is refused, and how it says so ------------------------------------------------------


def test_without_the_option_the_refusal_names_it_and_writes_nothing(tmp_path, export):
    out = tmp_path / "store"
    with pytest.raises(PreflightError) as error:
        convert(export, out, **OPTIONS)
    text = text_of(error.value)
    assert "no date in the file name" in text
    assert "--bands-are-dates" in text
    assert "4 bands read as 4 dates from their descriptions" in text
    assert "NDVI" in text
    assert not out.exists()
    assert not (tmp_path / "store.convert-work").exists()


def test_a_multi_band_file_whose_bands_are_not_dates_gets_no_offer(tmp_path, truth):
    path = write_bands(tmp_path / "s2.tif", truth[0], ["B04", "B08"])
    with pytest.raises(PreflightError) as error:
        plan_conversion(path, sample=False)
    assert "no date in the file name" in text_of(error.value)
    assert "--bands-are-dates" not in text_of(error.value)


@pytest.mark.parametrize(
    ("descriptions", "message"),
    [
        (
            ["", "2024_01_17_NDVI", "2024_02_02_NDVI", "2024_02_18_NDVI"],
            "band 1 has no description",
        ),
        (["2024_01_01_NDVI", "NDVI", "2024_02_02_NDVI", "B4"], "2 band descriptions hold no date"),
        (
            [
                "2024_01_01_NDVI",
                "2024_01_17_to_2024_01_18_NDVI",
                "2024_02_02_to_2024_02_03_NDVI",
                "2024_02_18_NDVI",
            ],
            "2 band descriptions hold several dates",
        ),
        (
            ["2024_01_01_NDVI", "2024_01_01_NDVI", "2024_02_02_NDVI", "2024_02_18_NDVI"],
            "band 1 '2024_01_01_NDVI' and band 2 '2024_01_01_NDVI' are the same date",
        ),
        (
            ["2024_01_01_NDVI", "2024_01_17_EVI", "2024_02_02_NDVI", "2024_02_18_EVI"],
            "do not all have the same bands",
        ),
        (
            ["2024_01_01_NDVI", "2024_01_01_EVI", "2024_01_01_NBR", "2024_01_01_NDWI"],
            "all the bands are the same date",
        ),
        (
            ["2024_01_01", "2024_01_01_EVI", "2024_01_17", "2024_01_17_EVI"],
            "nothing but the date",
        ),
        (
            ["2024_01_01_NDVI", "NDVI_2024_01_01", "2024_01_17_NDVI", "NDVI_2024_01_17"],
            "both name a band 'NDVI'",
        ),
    ],
    ids=[
        "no description",
        "no date",
        "several dates",
        "same date twice",
        "different variables per date",
        "one date",
        "empty name beside a name",
        "same name in two places",
    ],
)
def test_bands_that_cannot_be_read_as_dates_are_refused_with_the_reason(
    tmp_path, truth, descriptions, message
):
    path = write_bands(tmp_path / "bad.tif", truth[:, 0], descriptions)
    with pytest.raises(PreflightError) as error:
        plan_conversion(path, bands_are_dates=True, sample=False)
    assert message in text_of(error.value)
    assert {Path(p.uri).resolve() for p in error.value.problems} == {path.resolve()}


def test_a_file_with_one_band_is_not_a_series(tmp_path, truth):
    path = write_bands(tmp_path / "one.tif", truth[:1, 0], ["2024_01_01_NDVI"])
    with pytest.raises(PreflightError, match="has 1 band"):
        plan_conversion(path, bands_are_dates=True, sample=False)


def test_every_problem_in_the_descriptions_is_reported_at_once(tmp_path, truth):
    descriptions = ["", "NDVI", "2024_01_17_to_2024_01_18_NDVI", "2024_02_18_NDVI"]
    path = write_bands(tmp_path / "bad.tif", truth[:, 0], descriptions)
    with pytest.raises(PreflightError) as error:
        plan_conversion(path, bands_are_dates=True, sample=False)
    assert len(error.value.problems) == 3
    assert "3 problem(s) in 1 of 1 source file(s)" in str(error.value)


def test_the_option_needs_one_raster_file(tmp_path, export, truth):
    folder = tmp_path / "two"
    write_bands(folder / "a.tif", truth[:, 0], [f"{d}_NDVI" for d in DAYS])
    write_bands(folder / "b.tif", truth[:, 0], [f"{d}_NDVI" for d in DAYS])
    with pytest.raises(ValueError, match=r"one raster file whose bands are dates.*holds 2 files"):
        plan_conversion(folder, bands_are_dates=True, sample=False)
    manifest = tmp_path / "m.csv"
    manifest.write_text(f"uri,datetime\n{export},2024-01-01\n")
    with pytest.raises(ValueError, match="applies to one raster file"):
        plan_conversion(manifest, bands_are_dates=True, sample=False)
    with pytest.raises(ValueError, match="applies to one raster file"):
        plan_conversion(tmp_path / "model.nc", bands_are_dates=True, sample=False)


def test_a_manifest_cannot_hold_a_file_whose_bands_are_dates(tmp_path, export):
    target = tmp_path / "found.csv"
    with pytest.raises(ValueError, match=r"--write-manifest.*one file"):
        convert(
            export, tmp_path / "o", dry_run=True, bands_are_dates=True, write_manifest_to=target
        )
    assert not target.exists()


# --- command line -----------------------------------------------------------------------------


def test_cli_offers_the_option_then_reads_the_bands_as_dates(tmp_path, export, truth):
    out = tmp_path / "store"
    refused = run("convert", str(export), str(out), "--dry-run")
    assert refused.exit_code == 1
    assert "--bands-are-dates" in refused.output

    dry = run("convert", str(export), str(out), "--dry-run", "--bands-are-dates")
    assert dry.exit_code == 0, dry.output
    text = " ".join(dry.output.split())
    assert "4 bands read as 4 dates from their descriptions" in text
    assert "source: GeoTIFF with a date per band, 4 timesteps (2024-01-01 .. 2024-02-18)" in text
    assert "data: 1 bands (NDVI)" in text
    assert "band 3 2024_02_02_NDVI" in text
    assert not out.exists()

    real = run("convert", str(export), str(out), "--bands-are-dates", "--chunk-size", "16")
    assert real.exit_code == 0, real.output
    assert np.array_equal(stored(out), truth[:, :1])
    assert chronozarr.validate(out) == []


def test_cli_date_pattern_reads_descriptions_under_the_option(tmp_path, truth):
    path = write_bands(
        tmp_path / "doy.tif", truth[:, 0], [f"A2020{doy:03d}_NDVI" for doy in (1, 17, 33, 49)]
    )
    out = tmp_path / "store"
    result = run(
        "convert", str(path), str(out), "--bands-are-dates", "--date-pattern", "A%Y%j", "--dry-run"
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "2020-02-18" in result.output


def test_cli_help_names_the_option_and_what_it_does(tmp_path):
    text = " ".join(run("convert", "--help").output.split())
    assert "--bands-are-dates" in text
    assert "toBands()" in text
