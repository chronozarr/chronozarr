"""The plan lines about the sampled pixels: a likely undeclared fill value and the value range."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import xarray as xr
from click.testing import CliRunner

import chronozarr
from chronozarr import _convert_pixels
from chronozarr.cli import main
from chronozarr.convert import Plan, PreflightError, convert, plan_conversion
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


def refusal(tmp_path: Path, steps: list[np.ndarray], **plan_options) -> str:
    """The message of the ValueError that planning `steps` raises."""
    with pytest.raises(ValueError) as caught:
        plan_for(tmp_path, steps, **plan_options)
    return str(caught.value)


def flags(second_share: float = 0.02) -> list[np.ndarray]:
    """uint16 sea-ice concentration (0 to 1000), a 20 % land flag 2540 and a coast flag 2530."""
    random = rng()
    steps = []
    for _ in range(N_TIME):
        concentration = (random.integers(0, 251, size=(1, HEIGHT, WIDTH)) * 4).astype(np.uint16)
        concentration[random.random(concentration.shape) < 0.6] = 0
        put(concentration, 2540, 0.2)
        put(concentration, 2530, second_share, after=0.2)
        steps.append(concentration)
    return steps


def test_a_large_undeclared_fill_is_refused_with_the_fix_and_the_way_out(tmp_path):
    message = refusal(tmp_path, rain(0.45))
    assert (
        "-9999 (45 % of the valid pixels in the 3 sampled timesteps) lies far outside the other "
        "values (0 to " in message
    )
    assert "nothing marks it invalid, so the store would keep it as data" in message
    assert "If it marks missing pixels, pass --nodata -9999." in message
    assert "If it is data, pass --nodata none to keep every value." in message


def test_the_refusal_names_a_fill_in_one_band(tmp_path):
    random = rng()
    steps = []
    for _ in range(N_TIME):
        two = random.gamma(2.0, 50.0, size=(2, HEIGHT, WIDTH)).astype(np.float32)
        put(two[1:], -9999.0, 0.3)
        steps.append(two)
    message = refusal(tmp_path, steps, descriptions=["rain", "snow"])
    assert "-9999 in snow (30 % of the valid pixels in the 3 sampled timesteps)" in message


def test_naming_the_fill_with_nodata_ends_the_finding(tmp_path):
    plan = plan_for(tmp_path, rain(0.45), nodata=-9999)
    assert not has_line(plan, "fill:")
    assert line(plan, "validity:") == "validity:   nodata -9999.0 sentinel, no mask"


def test_nodata_none_answers_the_refusal_and_is_recorded(tmp_path):
    plan = plan_for(tmp_path, rain(0.45), nodata=None)
    assert line(plan, "validity:") == (
        "validity:   no nodata (--nodata none) and no mask: every pixel is stored as data"
    )
    fill = line(plan, "fill:")
    assert fill.startswith("fill:       -9999 (45 % of the valid pixels sampled) lies far outside")
    assert fill.endswith("The store keeps it as data, as --nodata none asks")
    assert "pass --nodata -9999" not in fill


@pytest.mark.parametrize(
    ("share", "refused"),
    [(0.04, False), (0.06, True)],
    ids=["below-five-percent-is-named", "from-five-percent-is-refused"],
)
def test_an_isolated_value_is_refused_from_five_percent(tmp_path, share, refused):
    steps = rain(share, fill=-8888.0)
    if refused:
        assert "-8888 (6 % of the valid pixels in the 3 sampled timesteps)" in refusal(
            tmp_path, steps
        )
        return
    fill = line(plan_for(tmp_path, steps), "fill:")
    assert "-8888 (4 % of the valid pixels sampled) lies far outside" in fill
    assert "pass --nodata -8888" in fill


def test_a_conventional_fill_value_is_refused_from_one_percent(tmp_path):
    assert "-9999 (2 % of the valid pixels in the 3 sampled timesteps)" in refusal(
        tmp_path, rain(0.02)
    )
    other = tmp_path / "ordinary"
    other.mkdir()
    named = line(plan_for(other, rain(0.02, fill=-8888.0)), "fill:")
    assert "-8888 (2 % of the valid pixels sampled)" in named
    small = tmp_path / "small"
    small.mkdir()
    assert "-9999 (0.5 % of the valid pixels sampled)" in line(
        plan_for(small, rain(0.005)), "fill:"
    )


def test_a_common_fill_inside_the_range_is_refused_as_a_common_fill(tmp_path):
    random = rng()
    steps = []
    for _ in range(N_TIME):
        scaled = random.integers(-4000, 10000, size=(1, HEIGHT, WIDTH)).astype(np.int16)
        put(scaled, -3000, 0.03)
        steps.append(scaled)
    message = refusal(tmp_path, steps)
    assert "-3000 (3 % of the valid pixels in the 3 sampled timesteps) is a common fill" in message
    assert "far outside" not in message
    assert "If it marks missing pixels, pass --nodata -3000." in message


def test_two_flag_values_are_refused_and_the_message_says_nodata_takes_one(tmp_path):
    message = refusal(tmp_path, flags())
    assert (
        "2540 (20 % of the valid pixels in the 3 sampled timesteps) and 2530 (2 %) lie far "
        "outside the other values (0 to 1000)" in message
    )
    assert "nothing marks them invalid, so the store would keep them as data" in message
    assert (
        "If they mark missing pixels, pass --nodata 2540: a store takes one nodata value, so "
        "2530 stays data." in message
    )
    assert (
        "To mark both, rewrite 2530 as 2540 in the files first, or give the files a mask (an "
        "alpha band or an internal mask)." in message
    )
    assert "If they are data, pass --nodata none to keep every value." in message


def test_the_second_flag_value_stays_a_finding_once_nodata_names_the_first(tmp_path):
    fill = line(plan_for(tmp_path, flags(), nodata=2540), "fill:")
    assert fill.startswith("fill:       2530 (2.5 % of the valid pixels sampled) lies far outside")
    assert fill.endswith(
        "The store keeps it as data; a store takes one nodata value and --nodata gave 2540"
    )


def test_both_flags_end_the_finding_when_the_second_is_rewritten_as_the_first(tmp_path):
    steps = flags()
    for step in steps:
        step[step == 2530] = 2540
    plan = plan_for(tmp_path, steps, nodata=2540)
    assert not has_line(plan, "fill:")


def test_a_fill_that_would_replace_a_declared_nodata_says_so(tmp_path):
    random = rng()
    steps = []
    for _ in range(N_TIME):
        counts = random.integers(1, 2000, size=(1, HEIGHT, WIDTH)).astype(np.uint16)
        put(counts, 0, 0.1)
        put(counts, 65535, 0.2, after=0.1)
        steps.append(counts)
    message = refusal(tmp_path, steps, file_nodata=0)
    assert "65535 (22 % of the valid pixels in the 3 sampled timesteps)" in message
    assert "pass --nodata 65535: it replaces the declared nodata 0, so 0 becomes data." in message


def test_an_explicit_nodata_for_another_value_is_never_refused(tmp_path):
    plan = plan_for(tmp_path, rain(0.45), nodata=-1)
    fill = line(plan, "fill:")
    assert "-9999 (45 % of the valid pixels sampled)" in fill
    assert fill.endswith(
        "The store keeps it as data; a store takes one nodata value and --nodata gave -1"
    )


def test_a_common_fill_is_named_when_nodata_names_another_value(tmp_path):
    random = rng()
    steps = []
    for _ in range(N_TIME):
        scaled = random.integers(-4000, 10000, size=(1, HEIGHT, WIDTH)).astype(np.int16)
        put(scaled, -3000, 0.03)
        steps.append(scaled)
    fill = line(plan_for(tmp_path, steps, nodata=-4001), "fill:")
    assert "-3000 (3 % of the valid pixels sampled) is a common fill value" in fill
    assert "far outside" not in fill


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
    message = refusal(tmp_path, steps)
    assert (
        "-9999 (45 % of the valid pixels in the 3 sampled timesteps) lies far outside" in message
    )


def test_the_sample_is_bounded(tmp_path, monkeypatch):
    plan = plan_for(tmp_path, rain(0.0))
    monkeypatch.setattr(_convert_pixels, "SAMPLE_PIXELS", 300)
    values = _convert_pixels._sample(list(plan.samples.values()), plan.source.info, 0)
    assert 0 < values.size <= 300


def test_a_single_file_is_one_sampled_timestep(tmp_path):
    found = refusal(tmp_path, rain(0.45)[:1])
    assert "-9999 (45 % of the valid pixels in the 1 sampled timestep)" in found
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
    with pytest.raises(ValueError, match=r"-9999 \(40 % of the valid pixels in the 3 sampled"):
        plan_conversion(tmp_path / "in.zarr", chunk_size=16)
    none = plan_conversion(tmp_path / "in.zarr", chunk_size=16, nodata=None)
    assert "-9999 (40 % of the valid pixels sampled)" in line(none, "fill:")
    ds["v"].attrs["_FillValue"] = np.float32(-9999)
    ds.to_zarr(tmp_path / "declared.zarr", zarr_format=2, consolidated=True)
    assert not has_line(plan_conversion(tmp_path / "declared.zarr", chunk_size=16), "fill:")


def test_detection_works_on_a_sample_of_the_pixels(tmp_path, monkeypatch):
    monkeypatch.setattr(_convert_pixels, "SAMPLE_PIXELS", 400)  # of 2000 per timestep
    message = refusal(tmp_path, rain(0.45))
    assert "-9999 (4" in message
    assert "pass --nodata -9999" in message


def scenes(tmp_path: Path, steps: list[np.ndarray]) -> Path:
    folder = tmp_path / "scenes"
    folder.mkdir()
    for t, array in enumerate(steps):
        write_tif(folder / f"rain_{DATES[t]}.tif", array)
    return folder


def test_the_cli_dry_run_refuses_a_large_fill_and_both_answers_work(tmp_path):
    folder = scenes(tmp_path, rain(0.45))
    out = tmp_path / "out"
    command = ["convert", str(folder), str(out), "--dry-run", "--chunk-size", "16"]
    refused = CliRunner().invoke(main, command)
    assert refused.exit_code == 1, refused.output
    assert refused.output.startswith(
        "Error: -9999 (45 % of the valid pixels in the 3 sampled timesteps) lies far outside"
    )
    assert "If it marks missing pixels, pass --nodata -9999." in refused.output
    marked = CliRunner().invoke(main, [*command, "--nodata", "-9999"])
    assert marked.exit_code == 0, marked.output
    assert "fill:" not in marked.output
    assert "validity:   nodata -9999.0 sentinel, no mask" in marked.output
    kept = CliRunner().invoke(main, [*command, "--nodata", "none"])
    assert kept.exit_code == 0, kept.output
    assert "validity:   no nodata (--nodata none) and no mask" in kept.output
    assert "The store keeps it as data, as --nodata none asks" in kept.output


def test_a_refused_fill_writes_nothing_and_each_answer_writes_the_store_it_names(tmp_path):
    steps = rain(0.45)
    folder = scenes(tmp_path, steps)
    out = tmp_path / "out"
    with pytest.raises(ValueError, match="pass --nodata -9999"):
        convert(folder, out, chunk_size=16)
    assert not out.exists()
    assert not (tmp_path / "out.convert-work").exists()

    convert(folder, tmp_path / "marked", nodata=-9999, chunk_size=16)
    marked = chronozarr.open_store(tmp_path / "marked")
    assert marked.attrs.nodata == -9999
    physical = marked.to_xarray(physical=True).values
    source = np.stack(steps)
    assert np.isnan(physical[source == -9999]).all()
    assert np.array_equal(physical[source != -9999], source[source != -9999])

    convert(folder, tmp_path / "kept", nodata=None, chunk_size=16)
    kept = chronozarr.open_store(tmp_path / "kept")
    assert kept.attrs.nodata is None
    assert np.array_equal(kept.to_xarray().values, source)


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


def ndvi_counts() -> np.ndarray:
    """int16 NDVI times 10000, -2000 to 10000, as MODIS MOD13 stores it."""
    ndvi = rng().integers(-1900, 9900, size=(1, HEIGHT, WIDTH)).astype(np.int16)
    ndvi[0, 0, :2] = (-2000, 10000)
    return ndvi


def counts_up_to(maximum: int, dtype: str = "uint16") -> np.ndarray:
    counts = rng().integers(0, maximum + 1, size=(1, HEIGHT, WIDTH)).astype(dtype)
    counts[0, 0, 0] = maximum
    return counts


MODIS: dict[str, Any] = {
    "scales": [10000.0],
    "units": ["NDVI"],
    "descriptions": ["500m 16 days NDVI"],
}


def test_a_scale_that_multiplies_a_ratio_is_refused_with_the_fix(tmp_path):
    message = refusal(tmp_path, [ndvi_counts()] * N_TIME, **MODIS)
    assert (
        "500m 16 days NDVI declares scale 10000 and would run -2e+07 to 1e+08 [NDVI] (stored * "
        "scale + offset, valid pixels of the 3 sampled timesteps)." in message
    )
    assert (
        "GDAL multiplies by the scale, but MODIS and other HDF-EOS products write a divisor"
        in (message)
    )
    assert "If that is what the files mean, pass --scale 0.0001." in message
    assert "If the scale is right, pass --scale 10000 to keep it." in message
    assert "all 1 bands" not in message


def test_the_refusal_says_that_scale_applies_to_every_band(tmp_path):
    two = np.concatenate([ndvi_counts(), ndvi_counts()])
    message = refusal(
        tmp_path,
        [two] * N_TIME,
        scales=[10000.0, 10000.0],
        descriptions=["NDVI", "EVI"],
    )
    assert "NDVI declares scale 10000 and would run -2e+07 to 1e+08" in message
    assert "EVI declares scale 10000 and would run -2e+07 to 1e+08" in message
    assert "--scale sets the scale of all 2 bands." in message


def test_scale_replaces_the_scale_of_the_files_and_the_plan_records_it(tmp_path):
    plan = plan_for(tmp_path, [ndvi_counts()] * N_TIME, scale=0.0001, **MODIS)
    assert line(plan, "scaling:") == "scaling:    500m 16 days NDVI = stored * 0.0001 +0 [NDVI]"
    assert "500m 16 days NDVI -0.2 to 1 [NDVI] (stored * scale + offset" in line(plan, "range:")
    assert line(plan, "override:") == "override:   --scale 0.0001; the files declare 10000"


def test_scale_equal_to_the_files_keeps_them_and_ends_the_refusal(tmp_path):
    plan = plan_for(tmp_path, [ndvi_counts()] * N_TIME, scale=10000.0, **MODIS)
    assert "-2e+07 to 1e+08" in line(plan, "range:")
    assert line(plan, "override:") == "override:   --scale 10000; the files declare 10000"


def test_offset_replaces_the_offset_of_the_files(tmp_path):
    steps = [counts_up_to(50)] * N_TIME
    plan = plan_for(tmp_path, steps, scales=[0.5], offsets=[3.0], offset=-1.0)
    assert line(plan, "scaling:") == "scaling:    1 = stored * 0.5 -1"
    assert line(plan, "override:") == "override:   --offset -1; the files declare 3"
    other = tmp_path / "both"
    other.mkdir()
    both = plan_for(other, steps, scales=[0.5], offsets=[3.0], scale=2.0, offset=0.0)
    assert line(both, "override:") == (
        "override:   --scale 2; the files declare 0.5. --offset 0; the files declare 3"
    )


@pytest.mark.parametrize(
    ("maximum", "dtype", "scale", "offset"),
    [
        pytest.param(10000, "uint16", 0.0001, 0.0, id="reflectance"),
        pytest.param(100, "uint8", 0.01, 0.0, id="percent"),
        pytest.param(65535, "uint16", 0.02, 0.0, id="land-surface-temperature-kelvin"),
        pytest.param(6000, "uint16", 0.01, -60.0, id="backscatter-db"),
        pytest.param(1050, "uint16", 100.0, 0.0, id="hectopascal-as-pascal"),
        pytest.param(500, "uint16", 1000.0, 0.0, id="thousands-of-people"),
        pytest.param(100, "uint16", 10000.0, 0.0, id="a-divisor-up-to-exactly-a-million"),
        pytest.param(2000, "uint16", 10000.0, 5.0, id="a-divisor-with-an-offset-is-not-the-case"),
        pytest.param(32767, "int16", 913.6, 29000000.0, id="era5-radiation-pack"),
    ],
)
def test_legitimate_scales_are_never_refused(tmp_path, maximum, dtype, scale, offset):
    steps = [counts_up_to(maximum, dtype)] * N_TIME
    plan = plan_for(tmp_path, steps, scales=[scale], offsets=[offset])
    assert not has_line(plan, "override:")
    assert has_line(plan, "range:")


def test_a_divisor_scale_is_refused_past_a_million_and_not_before(tmp_path):
    over = refusal(tmp_path, [counts_up_to(101)] * N_TIME, scales=[10000.0])
    assert "would run 0 to 1.01e+06" in over


def test_a_scale_or_offset_that_cannot_work_is_refused(tmp_path):
    steps = [counts_up_to(50)] * N_TIME
    cases: list[tuple[dict[str, Any], str]] = [
        ({"scale": 0.0}, "--scale must be finite and non-zero, got 0.0"),
        ({"scale": float("nan")}, "--scale must be finite and non-zero, got nan"),
        ({"offset": float("inf")}, "--offset must be finite, got inf"),
    ]
    for options, match in cases:
        with pytest.raises(ValueError, match=match):
            plan_for(tmp_path, steps, **options)


def test_files_that_disagree_on_scale_or_offset_are_accepted_once_it_is_given(tmp_path):
    files = [
        write_tif(
            tmp_path / f"d{t}.tif",
            counts_up_to(50),
            scales=[0.5 if t == 0 else 1.0],
            offsets=[0.0 if t < 2 else 4.0],
        )
        for t in range(N_TIME)
    ]
    rows = "\n".join(f"{f},{d}" for f, d in zip(files, DATES, strict=False))
    manifest = tmp_path / "m.csv"
    manifest.write_text(f"uri,datetime\n{rows}\n")
    with pytest.raises(PreflightError, match="has scales"):
        plan_conversion(manifest, chunk_size=16)
    with pytest.raises(PreflightError, match="has offsets"):
        plan_conversion(manifest, chunk_size=16, scale=1.0)
    plan = plan_conversion(manifest, chunk_size=16, scale=1.0, offset=0.0)
    assert line(plan, "scaling:") == "scaling:    1 = stored * 1 +0"


def zarr_with_packing(tmp_path: Path, counts: np.ndarray, **attrs: float) -> Path:
    a, _, c, _, e, f = TRANSFORM
    ds = xr.DataArray(
        np.stack([counts[0]] * N_TIME),
        dims=("time", "y", "x"),
        coords={
            "time": make_times(N_TIME),
            "y": f + e * (np.arange(HEIGHT) + 0.5),
            "x": c + a * (np.arange(WIDTH) + 0.5),
        },
        attrs={"crs": CRS, **attrs},
        name="v",
    ).to_dataset()
    ds.to_zarr(tmp_path / "packed.zarr", zarr_format=2, consolidated=True)
    return tmp_path / "packed.zarr"


def test_a_zarr_variable_packed_with_a_divisor_scale_is_refused_and_fixed(tmp_path):
    packed = zarr_with_packing(tmp_path, ndvi_counts(), scale_factor=10000.0)
    with pytest.raises(
        ValueError, match=r"v declares scale 10000 and would run -2e\+07 to 1e\+08"
    ):
        plan_conversion(packed, chunk_size=16)
    plan = plan_conversion(packed, chunk_size=16, scale=0.0001)
    assert "-0.2 to 1" in line(plan, "range:")


def test_a_zarr_pack_with_an_offset_and_an_irregular_scale_is_not_refused(tmp_path):
    counts = counts_up_to(32767, "int16")
    packed = zarr_with_packing(tmp_path, counts, scale_factor=913.6, add_offset=29000000.0)
    assert has_line(plan_conversion(packed, chunk_size=16), "range:")


def test_both_findings_are_reported_together(tmp_path):
    steps = [ndvi_counts()] * N_TIME
    for step in steps:
        put(step, -9999, 0.2)
    message = refusal(tmp_path, steps, **MODIS)
    fill, scale = message.split("\n")
    assert fill.startswith("-9999 (20 % of the valid pixels in the 3 sampled timesteps)")
    assert scale.startswith("500m 16 days NDVI declares scale 10000")


def test_the_cli_refuses_the_scale_and_each_answer_is_accepted(tmp_path):
    folder = tmp_path / "scenes"
    folder.mkdir()
    for date in DATES:
        write_tif(folder / f"ndvi_{date}.tif", ndvi_counts(), **MODIS)
    out = tmp_path / "out"
    command = ["convert", str(folder), str(out), "--dry-run", "--chunk-size", "16"]
    refused = CliRunner().invoke(main, command)
    assert refused.exit_code == 1, refused.output
    assert "If that is what the files mean, pass --scale 0.0001." in refused.output
    fixed = CliRunner().invoke(main, [*command, "--scale", "0.0001"])
    assert fixed.exit_code == 0, fixed.output
    assert "range:      500m 16 days NDVI -0.2 to 1 [NDVI]" in fixed.output
    assert "override:   --scale 0.0001; the files declare 10000" in fixed.output
    kept = CliRunner().invoke(main, [*command, "--scale", "10000"])
    assert kept.exit_code == 0, kept.output
    wrong = CliRunner().invoke(main, [*command, "--scale", "zero"])
    assert wrong.exit_code == 2
    assert "Invalid value for '--scale'" in wrong.output


def test_a_store_written_with_scale_carries_it_and_its_values_follow(tmp_path):
    counts = ndvi_counts()
    folder = tmp_path / "scenes"
    folder.mkdir()
    for date in DATES:
        write_tif(folder / f"ndvi_{date}.tif", counts, **MODIS)
    with pytest.raises(ValueError, match=r"pass --scale 0\.0001"):
        convert(folder, tmp_path / "refused", chunk_size=16)
    assert not (tmp_path / "refused").exists()
    convert(folder, tmp_path / "fixed", scale=0.0001, offset=0.0, chunk_size=16)
    store = chronozarr.open_store(tmp_path / "fixed")
    assert [(b.scale, b.offset) for b in store.attrs.bands] == [(0.0001, 0.0)]
    physical = store.to_xarray(physical=True).values
    assert np.allclose(physical, counts[None].repeat(N_TIME, axis=0) * np.float32(0.0001))
    assert float(physical.min()) >= -0.2 and float(physical.max()) <= 1.0


def test_the_range_leaves_out_declared_nodata_and_is_absent_without_a_scale(tmp_path):
    steps = rain(0.45)
    scaled = plan_for(tmp_path, steps, file_nodata=-9999, scales=[0.5])
    assert "-4999" not in line(scaled, "range:")
    other = tmp_path / "plain"
    other.mkdir()
    assert not has_line(plan_for(other, steps, file_nodata=-9999), "range:")
