"""The plan lines about the sampled pixels: a likely undeclared fill value and the value range."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest
import xarray as xr
from click.testing import CliRunner

from chronozarr import _convert_pixels
from chronozarr.cli import main
from chronozarr.convert import Plan, plan_conversion
from tests.synthetic import CRS, TRANSFORM, make_times

pytestmark = pytest.mark.unit

rasterio = pytest.importorskip("rasterio")
from rasterio.transform import Affine  # noqa: E402  (after importorskip)

HEIGHT, WIDTH, N_TIME = 40, 50, 3
DATES = ["2024-01-01", "2024-02-01", "2024-03-01"]


def write_tif(
    path: Path,
    array: np.ndarray,
    *,
    nodata: float | None = None,
    scales: list[float] | None = None,
    offsets: list[float] | None = None,
    units: list[str] | None = None,
    descriptions: list[str] | None = None,
    internal_mask: np.ndarray | None = None,
) -> Path:
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=array.shape[1],
        width=array.shape[2],
        count=array.shape[0],
        dtype=array.dtype,
        crs=CRS,
        transform=Affine(*TRANSFORM),
        nodata=nodata,
    ) as dst:
        dst.write(array)
        if internal_mask is not None:
            dst.write_mask(np.where(internal_mask, 255, 0).astype(np.uint8))
        if scales:
            dst.scales = scales
        if offsets:
            dst.offsets = offsets
        if units:
            dst.units = units
        if descriptions:
            dst.descriptions = descriptions
    return path


def plan_for(
    tmp_path: Path,
    steps: list[np.ndarray],
    *,
    file_nodata: float | None = None,
    scales: list[float] | None = None,
    offsets: list[float] | None = None,
    units: list[str] | None = None,
    descriptions: list[str] | None = None,
    **plan_options,
) -> Plan:
    """The plan for one GeoTIFF per (band, y, x) array in `steps`. `file_nodata`, `scales`,
    `offsets`, `units` and `descriptions` are what the files declare; `plan_options` go to convert.
    """
    files = [
        write_tif(
            tmp_path / f"s{t}.tif",
            array,
            nodata=file_nodata,
            scales=scales,
            offsets=offsets,
            units=units,
            descriptions=descriptions,
        )
        for t, array in enumerate(steps)
    ]
    rows = "\n".join(f"{f},{d}" for f, d in zip(files, DATES, strict=False))
    manifest = tmp_path / "m.csv"
    manifest.write_text(f"uri,datetime\n{rows}\n")
    return plan_conversion(manifest, chunk_size=16, **plan_options)


def line(plan: Plan, label: str) -> str:
    found = [text for text in plan.lines() if text.startswith(label)]
    assert len(found) == 1, plan.lines()
    return found[0]


def has_line(plan: Plan, label: str) -> bool:
    return any(text.startswith(label) for text in plan.lines())


def rng() -> np.random.Generator:
    return np.random.default_rng(11)


def put(array: np.ndarray, value: float, share: float, *, after: float = 0.0) -> None:
    """Set exactly `share` of the pixels of each band plane to `value`, skipping the first
    `after` share of a fixed shuffle so that several values never overlap."""
    size = HEIGHT * WIDTH
    order = np.random.default_rng(5).permutation(size)
    chosen = order[int(after * size) : int((after + share) * size)]
    for band in array:
        band.reshape(-1)[chosen] = value


def rain(fill_share: float, fill: float = -9999.0) -> list[np.ndarray]:
    """float32 rainfall: about 40 % exact zeros, a gamma body, `fill_share` of pixels at `fill`."""
    random = rng()
    steps = []
    for _ in range(N_TIME):
        field = random.gamma(0.6, 40.0, size=(1, HEIGHT, WIDTH)).astype(np.float32)
        field[random.random(field.shape) < 0.4] = 0.0
        put(field, fill, fill_share)
        steps.append(field)
    return steps


# --- an undeclared fill value -------------------------------------------------------------------


def test_a_large_undeclared_fill_is_named_with_its_fix(tmp_path):
    plan = plan_for(tmp_path, rain(0.45))
    fill = line(plan, "fill:")
    assert (
        "-9999 (45 % of the valid pixels sampled) lies far outside the other values (0 to " in fill
    )
    assert "The store keeps it as data" in fill
    assert "pass --nodata -9999" in fill
    validity = line(plan, "validity:")
    assert "every pixel is valid" not in validity
    assert validity == "validity:   no nodata declared and no mask: every pixel is stored as data"


def test_naming_the_fill_with_nodata_ends_the_finding(tmp_path):
    plan = plan_for(tmp_path, rain(0.45), nodata=-9999)
    assert not has_line(plan, "fill:")
    assert line(plan, "validity:") == "validity:   nodata -9999.0 sentinel, no mask"


def test_two_flag_values_are_both_named_and_nodata_takes_one(tmp_path):
    random = rng()
    steps = []
    for _ in range(N_TIME):
        concentration = (random.integers(0, 251, size=(1, HEIGHT, WIDTH)) * 4).astype(np.uint16)
        concentration[random.random(concentration.shape) < 0.6] = 0
        put(concentration, 2540, 0.2)
        put(concentration, 2530, 0.02, after=0.2)
        steps.append(concentration)
    fill = line(plan_for(tmp_path, steps), "fill:")
    assert (
        "2540 (20 % of the valid pixels sampled) and 2530 (2 %) lie far outside the other values"
        in fill
    )
    assert "(0 to 1000)" in fill
    assert "pass --nodata 2540; it takes one value, so 2530 stays data" in fill


def test_a_common_fill_inside_the_range_is_named_as_a_common_fill(tmp_path):
    random = rng()
    steps = []
    for _ in range(N_TIME):
        scaled = random.integers(-4000, 10000, size=(1, HEIGHT, WIDTH)).astype(np.int16)
        put(scaled, -3000, 0.03)
        steps.append(scaled)
    fill = line(plan_for(tmp_path, steps), "fill:")
    assert "-3000 (3 % of the valid pixels sampled) is a common fill value" in fill
    assert "far outside" not in fill
    assert "pass --nodata -3000" in fill


def test_a_fill_in_one_band_names_the_band(tmp_path):
    random = rng()
    steps = []
    for _ in range(N_TIME):
        two = random.gamma(2.0, 50.0, size=(2, HEIGHT, WIDTH)).astype(np.float32)
        put(two[1:], -9999.0, 0.3)
        steps.append(two)
    fill = line(plan_for(tmp_path, steps, descriptions=["rain", "snow"]), "fill:")
    assert "-9999 in snow (30 % of the valid pixels sampled)" in fill


def test_a_fill_that_replaces_a_declared_nodata_says_so(tmp_path):
    random = rng()
    steps = []
    for _ in range(N_TIME):
        counts = random.integers(1, 2000, size=(1, HEIGHT, WIDTH)).astype(np.uint16)
        put(counts, 0, 0.1)
        put(counts, 65535, 0.2, after=0.1)
        steps.append(counts)
    fill = line(plan_for(tmp_path, steps, file_nodata=0), "fill:")
    assert "65535 (22 %" in fill
    assert "it replaces the declared nodata 0" in fill


def two_clusters() -> np.ndarray:
    """uint8 in two runs of integers, 10 to 60 and 200 to 240: 41 heavy values off the body."""
    random = rng()
    low = random.integers(10, 61, size=(1, HEIGHT, WIDTH))
    high = random.integers(200, 241, size=(1, HEIGHT, WIDTH))
    return np.where(random.random((1, HEIGHT, WIDTH)) < 0.3, high, low).astype(np.uint8)


def zero_to_255(_: int) -> np.ndarray:
    ramp = np.linspace(0, 330, WIDTH) + rng().normal(0, 20, (1, HEIGHT, WIDTH))
    return np.clip(ramp, 0, 255).astype(np.uint8)


@pytest.mark.parametrize(
    "make",
    [
        pytest.param(lambda: rain(0.0), id="zeros-are-the-low-end-of-rainfall"),
        pytest.param(
            lambda: [zero_to_255(t) for t in range(N_TIME)], id="saturated-8-bit-imagery"
        ),
        pytest.param(
            lambda: [
                np.where(rng().random((1, HEIGHT, WIDTH)) < 0.3, 255, 0).astype(np.uint8)
                for _ in range(N_TIME)
            ],
            id="binary-0-255-mask",
        ),
        pytest.param(
            lambda: [rng().integers(100, 6000, (1, HEIGHT, WIDTH)).astype(np.uint16)] * N_TIME,
            id="reflectance",
        ),
        pytest.param(
            lambda: [two_clusters()] * N_TIME,
            id="two-clusters-of-integers",
        ),
        pytest.param(
            lambda: [
                np.where(
                    (np.arange(HEIGHT * WIDTH) == 7).reshape(1, HEIGHT, WIDTH),
                    np.float32(-9999),
                    rng().gamma(2.0, 50.0, (1, HEIGHT, WIDTH)).astype(np.float32),
                )
                for _ in range(N_TIME)
            ],
            id="three-stray-pixels",
        ),
    ],
)
def test_data_without_a_fill_gets_a_none_found_line(
    tmp_path, make: Callable[[], list[np.ndarray]]
):
    fill = line(plan_for(tmp_path, make()), "fill:")
    assert fill.startswith("fill:       none found in 3 sampled timesteps")
    assert "--nodata" not in fill


def test_infinite_values_do_not_hide_a_fill(tmp_path):
    steps = rain(0.45)
    for step in steps:
        step[0, 0, :3] = np.inf
    fill = line(plan_for(tmp_path, steps), "fill:")
    assert "-9999 (45 % of the valid pixels sampled) lies far outside the other values" in fill


def test_the_sample_is_bounded(tmp_path, monkeypatch):
    plan = plan_for(tmp_path, rain(0.0))
    monkeypatch.setattr(_convert_pixels, "SAMPLE_PIXELS", 300)
    values = _convert_pixels._sample(list(plan.samples.values()), plan.source.info, 0)
    assert 0 < values.size <= 300


def test_a_single_file_is_one_sampled_timestep(tmp_path):
    found = plan_for(tmp_path, rain(0.45)[:1])
    assert "-9999 (45 % of the valid pixels sampled)" in line(found, "fill:")
    other = tmp_path / "clean"
    other.mkdir()
    none = line(plan_for(other, rain(0.0)[:1], scales=[0.5]), "fill:")
    assert none.startswith("fill:       none found in 1 sampled timestep (")
    assert "valid pixels of the 1 sampled timestep)" in line(
        plan_for(other, rain(0.0)[:1], scales=[0.5]), "range:"
    )


def test_a_declared_nodata_is_not_reported_and_needs_no_none_found_line(tmp_path):
    plan = plan_for(tmp_path, rain(0.45), file_nodata=-9999)
    assert not has_line(plan, "fill:")


def test_pixels_an_internal_mask_hides_are_not_a_finding(tmp_path):
    random = rng()
    steps, masks = [], []
    for _ in range(N_TIME):
        field = random.gamma(2.0, 50.0, size=(1, HEIGHT, WIDTH)).astype(np.float32)
        hidden = random.random((HEIGHT, WIDTH)) < 0.4
        field[0][hidden] = -9999.0
        steps.append(field)
        masks.append(~hidden)
    files = [
        write_tif(tmp_path / f"m{t}.tif", steps[t], internal_mask=masks[t]) for t in range(N_TIME)
    ]
    rows = "\n".join(f"{f},{d}" for f, d in zip(files, DATES, strict=True))
    manifest = tmp_path / "m.csv"
    manifest.write_text(f"uri,datetime\n{rows}\n")
    plan = plan_conversion(manifest, chunk_size=16)
    assert plan.source.info.mask
    assert not has_line(plan, "fill:")


def test_without_samples_the_plan_claims_nothing_about_fills_or_ranges(tmp_path):
    plan = plan_for(tmp_path, rain(0.45), sample=False, scales=[0.5])
    assert not has_line(plan, "fill:")
    assert not has_line(plan, "range:")
    assert "stored as data" in line(plan, "validity:")


def test_nodata_none_is_named_as_asked_not_as_declared(tmp_path):
    steps = rain(0.0)
    declared = plan_for(tmp_path, steps, file_nodata=0, sample=False)
    assert line(declared, "validity:") == "validity:   nodata 0.0 sentinel, no mask"
    ignored = plan_for(tmp_path, steps, file_nodata=0, nodata=None, sample=False)
    assert line(ignored, "validity:") == (
        "validity:   no nodata (--nodata none) and no mask: every pixel is stored as data"
    )


def test_a_fill_in_a_zarr_variable_is_found_too(tmp_path):
    values = rng().gamma(2.0, 50.0, size=(N_TIME, HEIGHT, WIDTH)).astype(np.float32)
    put(values, -9999.0, 0.4)
    a, _, c, _, e, f = TRANSFORM
    ds = xr.DataArray(
        values,
        dims=("time", "y", "x"),
        coords={
            "time": make_times(N_TIME),
            "y": f + e * (np.arange(HEIGHT) + 0.5),
            "x": c + a * (np.arange(WIDTH) + 0.5),
        },
        attrs={"crs": CRS},
        name="v",
    ).to_dataset()
    ds.to_zarr(tmp_path / "in.zarr", zarr_format=2, consolidated=True)
    assert "-9999 (40 % of the valid pixels sampled)" in line(
        plan_conversion(tmp_path / "in.zarr", chunk_size=16), "fill:"
    )
    ds["v"].attrs["_FillValue"] = np.float32(-9999)
    ds.to_zarr(tmp_path / "declared.zarr", zarr_format=2, consolidated=True)
    assert not has_line(plan_conversion(tmp_path / "declared.zarr", chunk_size=16), "fill:")


def test_detection_works_on_a_sample_of_the_pixels(tmp_path, monkeypatch):
    monkeypatch.setattr(_convert_pixels, "SAMPLE_PIXELS", 400)  # of 2000 per timestep
    fill = line(plan_for(tmp_path, rain(0.45)), "fill:")
    assert "-9999 (4" in fill
    assert "pass --nodata -9999" in fill


def test_the_cli_dry_run_prints_the_fill_and_the_fix_works(tmp_path):
    folder = tmp_path / "scenes"
    folder.mkdir()
    for t, array in enumerate(rain(0.45)):
        write_tif(folder / f"rain_{DATES[t]}.tif", array)
    command = ["convert", str(folder), str(tmp_path / "out"), "--dry-run", "--chunk-size", "16"]
    dry = CliRunner().invoke(main, command)
    assert dry.exit_code == 0, dry.output
    assert "fill:       -9999 (45 % of the valid pixels sampled)" in dry.output
    fixed = CliRunner().invoke(main, [*command, "--nodata", "-9999"])
    assert fixed.exit_code == 0, fixed.output
    assert "fill:" not in fixed.output
    assert "validity:   nodata -9999.0 sentinel, no mask" in fixed.output


# --- the value range after scale and offset -----------------------------------------------------


def test_a_declared_scale_gets_a_physical_range_line(tmp_path):
    random = rng()
    steps = [
        random.integers(100, 6000, size=(2, HEIGHT, WIDTH)).astype(np.uint16)
        for _ in range(N_TIME)
    ]
    plan = plan_for(
        tmp_path,
        steps,
        scales=[0.0001, 0.0002],
        units=["reflectance", "reflectance"],
        descriptions=["red", "nir"],
    )
    low = min(int(s[0].min()) for s in steps) * 0.0001
    high = max(int(s[0].max()) for s in steps) * 0.0001
    text = line(plan, "range:")
    assert f"red {low:.4g} to {high:.4g} [reflectance]" in text
    assert "nir 0.02" in text
    assert "valid pixels of the 3 sampled timesteps" in text


def test_the_range_applies_the_offset_and_orders_a_negative_scale(tmp_path):
    counts = rng().integers(5, 51, size=(1, HEIGHT, WIDTH)).astype(np.uint16)
    counts[0, 0, :2] = (5, 50)
    shifted = line(plan_for(tmp_path, [counts] * N_TIME, scales=[0.5], offsets=[-10.0]), "range:")
    assert "1 -7.5 to 15 (" in shifted
    flipped = line(plan_for(tmp_path, [counts] * N_TIME, scales=[-1.0]), "range:")
    assert "1 -50 to -5 (" in flipped


def test_a_scale_that_multiplies_a_ratio_shows_an_absurd_range(tmp_path):
    ndvi = rng().integers(-1900, 9900, size=(1, HEIGHT, WIDTH)).astype(np.int16)
    ndvi[0, 0, :2] = (-2000, 10000)
    text = line(plan_for(tmp_path, [ndvi] * N_TIME, scales=[10000.0]), "range:")
    assert "1 -2e+07 to 1e+08" in text


def test_the_range_leaves_out_declared_nodata_and_is_absent_without_a_scale(tmp_path):
    steps = rain(0.45)
    scaled = plan_for(tmp_path, steps, file_nodata=-9999, scales=[0.5])
    assert "-4999" not in line(scaled, "range:")
    other = tmp_path / "plain"
    other.mkdir()
    assert not has_line(plan_for(other, steps, file_nodata=-9999), "range:")
