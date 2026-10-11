"""The Sentinel-2 ingest pipeline gives the same composites as the original algorithm.

The original (examples/sentinel2_pc/mosaic.py before the pipeline rewrite) reprojected every
band and SCL asset onto the AOI grid, masked, took a float32 nanmedian over scenes and filled
gaps from the previous month. `reference_mosaics` below is that algorithm; the tests compare
the pipeline's .npz output with it bit for bit on synthetic local GeoTIFFs.
"""

from __future__ import annotations

import sys
import warnings
from datetime import date
from pathlib import Path

import numpy as np
import pytest

rasterio = pytest.importorskip("rasterio")
pytest.importorskip("planetary_computer")
pytest.importorskip("pystac_client")

from rasterio.crs import CRS  # noqa: E402  # ty: ignore[unresolved-import]
from rasterio.transform import Affine  # noqa: E402
from rasterio.warp import Resampling, reproject  # noqa: E402

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "sentinel2_pc"
sys.path.insert(0, str(EXAMPLE))
import catalog  # noqa: E402
import mosaic  # noqa: E402
import performance  # noqa: E402

pytestmark = [
    pytest.mark.unit,
    # rasterio wraps a numpy warp destination in a MEM dataset inside warnings.catch_warnings()
    # to hide this warning, then sets the transform. catch_warnings is not thread-safe, so with
    # concurrent warps the filter is sometimes gone when the warning fires (2 in 3600 threaded
    # reads); the outputs are unaffected. Every input in these tests is georeferenced.
    pytest.mark.filterwarnings(
        "ignore:Dataset has no geotransform, gcps, or rpcs:rasterio.errors.NotGeoreferencedWarning"
    ),
]

BBOX = (-75.0, -7.6, -74.985, -7.585)
EPSG = 32718
BANDS = catalog.REQUIRED_BANDS


def _grid() -> mosaic.Grid:
    transform, height, width = mosaic.compute_target_grid(BBOX, EPSG)
    return mosaic.Grid(transform, CRS.from_epsg(EPSG), height, width)


def write_tif(path: Path, data: np.ndarray, transform: Affine, epsg: int, block: int = 32) -> str:
    profile = {
        "driver": "GTiff",
        "height": data.shape[0],
        "width": data.shape[1],
        "count": 1,
        "dtype": data.dtype,
        "crs": CRS.from_epsg(epsg),
        "transform": transform,
        "nodata": 0,
        "tiled": True,
        "blockxsize": block,
        "blockysize": block,
        "compress": "deflate",
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data, 1)
    return str(path)


def reproject_ref(href: str, grid: mosaic.Grid, dtype, resampling) -> np.ndarray:
    out = np.zeros((grid.height, grid.width), dtype=dtype)
    with rasterio.open(href) as src:
        reproject(
            source=rasterio.band(src, 1),
            destination=out,
            src_transform=src.transform,
            src_crs=src.crs,
            dst_transform=grid.transform,
            dst_crs=grid.crs,
            resampling=resampling,
        )
    return out


def random_band(rng, shape, zero_fraction=0.05) -> np.ndarray:
    data = rng.integers(1, 12000, size=shape, dtype=np.uint16)
    data[rng.random(shape) < zero_fraction] = 0
    return data


def scene_files(
    tmp: Path,
    rng,
    name: str,
    grid: mosaic.Grid,
    dx: int,
    dy: int,
    epsg=EPSG,
    size=(90, 80),
    scl_classes=(4, 5, 8, 9, 0),
) -> dict[str, str]:
    """Four 10 m bands and a 20 m SCL whose origin is (dx, dy) grid pixels off the grid's."""
    g = grid.transform
    origin_x, origin_y = g.c + dx * 10.0, g.f - dy * 10.0
    if epsg != EPSG:
        # Same area, other UTM zone: an unaligned grid that needs a real warp.
        from rasterio.warp import transform as warp_xy

        xs, ys = warp_xy(CRS.from_epsg(EPSG), CRS.from_epsg(epsg), [origin_x], [origin_y])
        origin_x, origin_y = round(xs[0] / 10) * 10 - 300, round(ys[0] / 10) * 10 + 300
        size = (size[0] + 60, size[1] + 60)
    hrefs = {}
    for band in BANDS:
        hrefs[band] = write_tif(
            tmp / f"{name}_{band}.tif",
            random_band(rng, size),
            Affine(10, 0, origin_x, 0, -10, origin_y),
            epsg,
        )
    scl = rng.choice(
        np.array(scl_classes, dtype=np.uint8), size=(size[0] // 2 + 1, size[1] // 2 + 1)
    )
    hrefs["SCL"] = write_tif(
        tmp / f"{name}_SCL.tif", scl, Affine(20, 0, origin_x, 0, -20, origin_y), epsg, block=16
    )
    return hrefs


def scene(item_id: str, day: date, hrefs: dict[str, str], baseline=5.0) -> catalog.SceneRef:
    return catalog.SceneRef(
        item_id=item_id,
        datetime=day,
        cloud_cover=10.0,
        epsg=EPSG,
        mgrs_tile="18MXX",
        processing_baseline=baseline,
        asset_hrefs=hrefs,
    )


def reference_mosaics(
    scenes_by_month, grid: mosaic.Grid
) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """The original algorithm: reproject everything, float32 nanmedian, carry forward."""
    out = {}
    prev = None
    for key in sorted(scenes_by_month):
        scenes = scenes_by_month[key]
        stack = np.full((len(scenes), len(BANDS), grid.height, grid.width), np.nan, np.float32)
        for i, s in enumerate(scenes):
            try:
                bands = np.stack(
                    [
                        reproject_ref(s.asset_hrefs[b], grid, np.uint16, Resampling.bilinear)
                        for b in BANDS
                    ]
                )
                scl = reproject_ref(s.scl_href, grid, np.uint8, Resampling.nearest)
            except rasterio.errors.RasterioIOError:
                continue
            if s.boa_offset:
                shifted = bands.astype(np.int32) + s.boa_offset
                bands = np.where(bands > 0, np.clip(shifted, 1, 65535), 0).astype(np.uint16)
            valid = np.isin(scl, list(mosaic.SCL_VALID)) & np.all(bands > 0, axis=0)
            f = bands.astype(np.float32)
            f[:, ~valid] = np.nan
            stack[i] = f
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN pixels
            med = np.nanmedian(stack, axis=0)
        coverage = np.sum(~np.isnan(stack[:, 0]), axis=0).astype(np.float32) / len(scenes)
        composite = np.nan_to_num(med, nan=0.0).astype(np.uint16)
        if prev is not None:
            gap = np.all(composite == 0, axis=0)
            composite[:, gap] = prev[:, gap]
        prev = composite.copy()
        out[key] = (composite, coverage)
    return out


def settings(**overrides) -> performance.Settings:
    resources = performance.Resources(
        cpus=4,
        cpu_source="test",
        memory_total=8 * performance.GIB,
        memory_available=4 * performance.GIB,
        memory_limit=None,
        memory_source="test",
        open_files=1024,
    )
    return performance.plan_settings(resources, **overrides)


# --- reading -----------------------------------------------------------------------------------


@pytest.mark.parametrize("dx,dy", [(0, 0), (-7, 5), (13, -9), (-40, -50), (60, 70)])
def test_native_read_equals_reproject(tmp_path, dx, dy):
    """Bilinear 10 m and nearest 20 m -> 10 m on an aligned grid: identical to GDAL's warp,
    including partial overlap and source edges."""
    grid = _grid()
    rng = np.random.default_rng(abs(dx) * 100 + abs(dy))
    hrefs = scene_files(tmp_path, rng, "s", grid, dx, dy)
    env = dict(mosaic.GDAL_ENV)
    for band, dtype, resampling in (
        ("B04", np.uint16, Resampling.bilinear),
        ("SCL", np.uint8, Resampling.nearest),
    ):
        ref = reproject_ref(hrefs[band], grid, dtype, resampling)
        out = np.zeros_like(ref)
        _, native = mosaic.read_asset(hrefs[band], grid, out, resampling, env)
        assert native
        np.testing.assert_array_equal(out, ref)


def test_unaligned_scene_falls_back_to_reproject(tmp_path):
    grid = _grid()
    rng = np.random.default_rng(3)
    hrefs = scene_files(tmp_path, rng, "z", grid, 0, 0, epsg=32719)
    ref = reproject_ref(hrefs["B02"], grid, np.uint16, Resampling.bilinear)
    out = np.zeros_like(ref)
    _, native = mosaic.read_asset(hrefs["B02"], grid, out, Resampling.bilinear, mosaic.GDAL_ENV)
    assert not native
    np.testing.assert_array_equal(out, ref)


def test_half_pixel_shift_is_not_native():
    grid = _grid()
    g = grid.transform
    shifted = Affine(10, 0, g.c + 5, 0, -10, g.f)
    assert mosaic.native_offset(shifted, grid.crs, grid) is None
    # Source top-left 4 grid pixels left of and 6 above the grid's, at 20 m.
    coarse = Affine(20, 0, g.c - 40, 0, -20, g.f + 60)
    assert mosaic.native_offset(coarse, grid.crs, grid) == (6, 4, 2)
    assert mosaic.native_offset(g, CRS.from_epsg(32719), grid) is None


def test_block_skipping_reads_only_needed_blocks(tmp_path):
    grid = _grid()
    rng = np.random.default_rng(11)
    hrefs = scene_files(tmp_path, rng, "b", grid, -5, -3, size=(160, 160))
    ref = reproject_ref(hrefs["B08"], grid, np.uint16, Resampling.bilinear)
    needed = np.zeros((grid.height, grid.width), dtype=bool)
    needed[10:14, 20:25] = True
    needed[70:71, 90:91] = True
    out = np.zeros_like(ref)
    pixels, native = mosaic.read_asset(
        hrefs["B08"], grid, out, Resampling.bilinear, mosaic.GDAL_ENV, needed=needed
    )
    assert native
    np.testing.assert_array_equal(out[needed], ref[needed])
    assert pixels < grid.height * grid.width / 4
    assert np.all((out == ref) | (out == 0))


def test_block_rects_cover_needed_pixels_once():
    rng = np.random.default_rng(5)
    needed = rng.random((150, 170)) < 0.004
    needed[100:150, :] = True  # whole block rows merge into one rectangle
    rects = mosaic._block_rects(
        needed, row=7, col=-3, rows=(0, 150), cols=(3, 170), block=(32, 32)
    )
    covered = np.zeros(needed.shape, dtype=int)
    for r0, r1, c0, c1 in rects:
        covered[r0:r1, c0:c1] += 1
    assert covered.max() == 1
    # Rectangles start and end on source block edges (row + r) % 32 == 0, or on the window edge.
    for r0, r1, c0, c1 in rects:
        assert r0 == 0 or (7 + r0) % 32 == 0
        assert r1 == 150 or (7 + r1) % 32 == 0
        assert c0 == 3 or (c0 - 3) % 32 == 0
        assert c1 == 170 or (c1 - 3) % 32 == 0
    assert np.all(covered[needed[:, 3:].nonzero()[0], needed[:, 3:].nonzero()[1] + 3] == 1)
    full = np.ones((64, 64), dtype=bool)
    assert mosaic._block_rects(full, 0, 0, (0, 64), (0, 64), (32, 32)) == [(0, 64, 0, 64)]


# --- compositing -------------------------------------------------------------------------------


@pytest.mark.parametrize("n", [1, 2, 3, 4, 7, 12])
def test_median_matches_float32_nanmedian(n):
    rng = np.random.default_rng(n)
    stack = rng.integers(1, 65536, size=(n, 2, 40, 30), dtype=np.uint16)
    stack[rng.random((n, 2, 40, 30)) < 0.2] = 65535  # saturated values are valid
    valid = rng.random((n, 40, 30)) < 0.6
    valid[:, :3] = False  # some pixels have no valid scene
    stack[np.broadcast_to(~valid[:, None], stack.shape)] = 0
    f = stack.astype(np.float32)
    f[f == 0] = np.nan
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN pixels
        expected = np.nan_to_num(np.nanmedian(f, axis=0), nan=0.0).astype(np.uint16)
    composite, coverage = mosaic.median_composite(stack, n, rows=7)
    np.testing.assert_array_equal(composite, expected)
    expected_cov = np.sum(~np.isnan(f[:, 0]), axis=0).astype(np.float32) / n
    np.testing.assert_array_equal(coverage, expected_cov)


def test_scene_mask_applies_offset_and_band_zeros():
    bands = np.array([[[0, 500, 1000, 3000]], [[7, 900, 0, 3000]]], dtype=np.uint16)
    valid = np.array([[True, True, True, False]])
    n = mosaic.apply_scene_mask(bands, valid, -1000)
    # 500 - 1000 clips to 1 (not 0), a 0 band invalidates the pixel, SCL-invalid pixels are zeroed.
    np.testing.assert_array_equal(bands, [[[0, 1, 0, 0]], [[0, 1, 0, 0]]])
    assert n == 1


# --- the pipeline ------------------------------------------------------------------------------


def build_case(tmp_path: Path):
    grid = _grid()
    rng = np.random.default_rng(42)
    d = tmp_path / "src"
    d.mkdir()
    jan = [
        scene("a", date(2024, 1, 3), scene_files(d, rng, "a", grid, -6, -4), baseline=3.0),
        scene("b", date(2024, 1, 9), scene_files(d, rng, "b", grid, 10, 12)),
        scene("c", date(2024, 1, 15), scene_files(d, rng, "c", grid, 0, 0, epsg=32719)),
        # SCL with no valid class: the scene contributes nothing and reads no band.
        scene("d", date(2024, 1, 20), scene_files(d, rng, "d", grid, 0, 0, scl_classes=(8, 9))),
    ]
    missing = dict(scene_files(d, rng, "e", grid, 0, 0))
    missing["B03"] = str(d / "does_not_exist.tif")
    feb = [
        scene("e", date(2024, 2, 2), missing),
        scene("f", date(2024, 2, 8), scene_files(d, rng, "f", grid, 30, -20, size=(50, 40))),
    ]
    mar = [
        scene(
            "g", date(2024, 3, 1), scene_files(d, rng, "g", grid, 5, 5, scl_classes=(4, 0, 0, 0))
        )
    ]
    return grid, {"2024-01": jan, "2024-02": feb, "2024-03": mar}


@pytest.fixture
def no_backoff(monkeypatch):
    monkeypatch.setattr(mosaic, "READ_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(mosaic, "READ_ATTEMPTS", 2)


def load(path: Path) -> tuple[np.ndarray, np.ndarray]:
    with np.load(path) as data:
        return data["bands"], data["coverage"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"adaptive": False, "requests": 1, "cpu_workers": 1},
        {"adaptive": True, "requests": 3, "max_requests": 6},
        # A budget that fits the largest month but never two at once: months run in turn.
        {"adaptive": False, "requests": 4, "memory_budget": "one month"},
    ],
)
def test_pipeline_matches_reference(tmp_path, no_backoff, overrides):
    """With --keep-going, a failed scene is left out of its month's median exactly as the
    original algorithm left it out, and the month lists it."""
    grid, by_month = build_case(tmp_path)
    expected = reference_mosaics(by_month, grid)
    if overrides.get("memory_budget") == "one month":
        planned = settings(**{**overrides, "memory_budget": None})
        one = mosaic.fixed_bytes(planned, 4, grid.height, grid.width) + mosaic.month_bytes(
            4, 4, grid.height, grid.width
        )
        overrides = {**overrides, "memory_budget": one}
    report = mosaic.RunReport()
    outputs = mosaic.build_monthly_mosaics(
        by_month,
        BBOX,
        EPSG,
        tmp_path / "out",
        settings=settings(**overrides),
        sign=str,
        report=report,
        keep_going=True,
    )
    assert list(outputs) == ["2024-01", "2024-02", "2024-03"]
    for key, (bands, coverage) in expected.items():
        got_bands, got_coverage = load(outputs[key])
        np.testing.assert_array_equal(got_bands, bands, err_msg=key)
        np.testing.assert_array_equal(got_coverage, coverage, err_msg=key)
    assert report.scenes_failed == 1
    assert report.scenes_no_valid == 1
    assert report.reads_native > 0 and report.reads > report.reads_native
    assert report.months_incomplete == ["2024-02"]
    feb = mosaic.load_mosaic(outputs["2024-02"])
    assert feb["scenes_failed"] == ["e"]
    assert mosaic.load_mosaic(outputs["2024-01"])["scenes_failed"] == []
    assert not list((tmp_path / "out").glob(".*.tmp"))


def test_resume_carries_forward_from_existing_month(tmp_path, no_backoff):
    grid, by_month = build_case(tmp_path)
    expected = reference_mosaics(by_month, grid)
    out = tmp_path / "out"
    first = {"2024-01": by_month["2024-01"]}
    mosaic.build_monthly_mosaics(first, BBOX, EPSG, out, settings=settings(), sign=str)
    report = mosaic.RunReport()
    outputs = mosaic.build_monthly_mosaics(
        by_month, BBOX, EPSG, out, settings=settings(), sign=str, report=report, keep_going=True
    )
    assert report.months_skipped == 1
    for key, (bands, _) in expected.items():
        np.testing.assert_array_equal(load(outputs[key])[0], bands, err_msg=key)


def test_error_outside_reads_stops_the_run(tmp_path, no_backoff):
    _, by_month = build_case(tmp_path)

    def sign(href: str) -> str:
        raise PermissionError("token endpoint refused")

    with pytest.raises(PermissionError):
        mosaic.build_monthly_mosaics(
            by_month, BBOX, EPSG, tmp_path / "out", settings=settings(), sign=sign
        )
    assert not list((tmp_path / "out").glob("*.npz"))


def test_a_failed_scene_stops_the_run_by_default(tmp_path, no_backoff):
    """A median without a scene that exists is not the month's composite: by default the run
    stops at that month, keeps the months before it and starts none after it."""
    _, by_month = build_case(tmp_path)
    with pytest.raises(mosaic.SceneReadError, match="1 of 2 scenes of 2024-02") as error:
        mosaic.build_monthly_mosaics(
            by_month, BBOX, EPSG, tmp_path / "out", settings=settings(), sign=str
        )
    assert "--keep-going" in str(error.value)
    assert sorted(p.name for p in (tmp_path / "out").glob("*.npz")) == ["2024-01.npz"]


def test_month_with_every_scene_failing_stops_the_run(tmp_path, no_backoff):
    _, by_month = build_case(tmp_path)
    broken = {
        "2024-02": [
            scene(s.item_id, s.datetime, {k: v + ".missing" for k, v in s.asset_hrefs.items()})
            for s in by_month["2024-02"]
        ]
    }
    with pytest.raises(mosaic.SceneReadError, match="2 of 2 scenes of 2024-02"):
        mosaic.build_monthly_mosaics(
            {"2024-01": by_month["2024-01"], **broken},
            BBOX,
            EPSG,
            tmp_path / "out",
            settings=settings(),
            sign=str,
        )
    assert (tmp_path / "out" / "2024-01.npz").exists()
    assert not (tmp_path / "out" / "2024-02.npz").exists()


def test_memory_preflight_fails_before_any_download(tmp_path, no_backoff):
    _, by_month = build_case(tmp_path)
    signed: list[str] = []

    def sign(href: str) -> str:
        signed.append(href)
        return href

    with pytest.raises(mosaic.MemoryBudgetError) as error:
        mosaic.build_monthly_mosaics(
            by_month,
            BBOX,
            EPSG,
            tmp_path / "out",
            settings=settings(memory_budget=1024),
            sign=sign,
        )
    message = str(error.value)
    assert "2024-01 has 4 scenes" in message
    assert "Nothing was downloaded" in message and "--memory" in message
    assert "set by user" in message
    # The suggested --memory must cover the need, not round below it.
    grid = _grid()
    planned = settings(memory_budget=1024)
    need = mosaic.fixed_bytes(planned, 4, grid.height, grid.width) + mosaic.month_bytes(
        4, 4, grid.height, grid.width
    )
    suggested = message.split("--memory with at least ")[1].split("GB")[0]
    assert performance.parse_bytes(f"{suggested}GB") >= need
    assert signed == []
    assert not list((tmp_path / "out").glob("*.npz"))


def test_throttle_counter_counts_gdal_http_retry_warnings():
    import logging

    counter = mosaic.HttpThrottleCounter()
    logger = logging.getLogger("rasterio._env")
    logger.addFilter(counter)
    try:
        for message in (
            "CPLE_AppDefined in HTTP error code: 503 - https://h/a.tif. Retrying in 1.0 secs",
            "CPLE_AppDefined in HTTP error code: 429 - https://host/a.tif",
            "CPLE_AppDefined in HTTP error code: 404 - https://host/a.tif",
            "something else",
        ):
            logger.handle(
                logger.makeRecord(logger.name, logging.WARNING, "", 0, message, (), None)
            )
    finally:
        logger.removeFilter(counter)
    assert counter.events == 2


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _lab_server(root: Path, port: int, fail_rate: str, *extra: str):
    import subprocess

    process = subprocess.Popen(
        [
            sys.executable,
            str(EXAMPLE / "labserver.py"),
            "--root",
            str(root),
            "--port",
            str(port),
            "--fail-rate",
            fail_rate,
            *extra,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline().startswith("http://")
    return process


def _stop(process) -> None:
    process.terminate()
    process.wait(5)
    process.stdout.close()


def test_retry_after_a_failed_open_reaches_the_host(tmp_path):
    """GDAL caches a failed open per URL; forget_url must clear it so a retry can succeed."""
    grid = _grid()
    hrefs = scene_files(tmp_path, np.random.default_rng(1), "r", grid, 0, 0)
    port = _free_port()
    url = f"http://127.0.0.1:{port}/{Path(hrefs['B02']).name}"
    env = {**mosaic.GDAL_ENV, "GDAL_HTTP_MAX_RETRY": "0"}
    out = np.zeros((grid.height, grid.width), dtype=np.uint16)

    failing = _lab_server(tmp_path, port, "1.0")
    try:
        with pytest.raises(rasterio.errors.RasterioIOError):
            mosaic.read_asset(url, grid, out, Resampling.bilinear, env)
    finally:
        _stop(process=failing)
    healthy = _lab_server(tmp_path, port, "0")
    try:
        with pytest.raises(rasterio.errors.RasterioIOError):  # the failure is cached
            mosaic.read_asset(url, grid, out, Resampling.bilinear, env)
        mosaic.forget_url(url)
        mosaic.read_asset(url, grid, out, Resampling.bilinear, env)
    finally:
        _stop(process=healthy)
    np.testing.assert_array_equal(
        out, reproject_ref(hrefs["B02"], grid, np.uint16, Resampling.bilinear)
    )


def test_pipeline_recovers_from_a_transient_error_on_every_asset(
    tmp_path, no_backoff, monkeypatch
):
    """Every asset's first request fails and GDAL does not retry: each read must succeed on the
    pipeline's second attempt and the composites must equal the reference."""
    monkeypatch.setattr(mosaic, "GDAL_ENV", {**mosaic.GDAL_ENV, "GDAL_HTTP_MAX_RETRY": "0"})
    grid, by_month = build_case(tmp_path)
    jan = {"2024-01": [s for s in by_month["2024-01"] if s.item_id in ("a", "b")]}
    expected = reference_mosaics(jan, grid)
    port = _free_port()
    root = tmp_path / "src"
    served = {
        "2024-01": [
            scene(
                s.item_id,
                s.datetime,
                {k: f"http://127.0.0.1:{port}/{Path(v).name}" for k, v in s.asset_hrefs.items()},
                baseline=s.processing_baseline,
            )
            for s in jan["2024-01"]
        ]
    }
    server = _lab_server(root, port, "0", "--fail-first", "1")
    try:
        report = mosaic.RunReport()
        outputs = mosaic.build_monthly_mosaics(
            served, BBOX, EPSG, tmp_path / "out", settings=settings(), sign=str, report=report
        )
    finally:
        _stop(server)
    assert report.scenes_failed == 0
    assert report.read_retries == 10  # 2 scenes x 5 assets, one failed attempt each
    bands, coverage = load(outputs["2024-01"])
    np.testing.assert_array_equal(bands, expected["2024-01"][0])
    np.testing.assert_array_equal(coverage, expected["2024-01"][1])


def test_month_without_scenes_is_all_zero_then_carried_forward(tmp_path, no_backoff):
    _, by_month = build_case(tmp_path)
    cases = {"2024-01": by_month["2024-01"], "2024-02": []}
    outputs = mosaic.build_monthly_mosaics(
        cases, BBOX, EPSG, tmp_path / "out", settings=settings(), sign=str
    )
    jan, _ = load(outputs["2024-01"])
    feb, feb_coverage = load(outputs["2024-02"])
    np.testing.assert_array_equal(feb, jan)  # every pixel carried forward
    assert not feb_coverage.any()


def test_keep_going_skips_a_month_whose_scenes_all_failed(tmp_path, no_backoff):
    """No imputed time step: the month is not written, and the next month records that it
    filled its gaps from the month before the skipped one."""
    _, by_month = build_case(tmp_path)
    broken = [
        scene(s.item_id, s.datetime, {k: v + ".missing" for k, v in s.asset_hrefs.items()})
        for s in by_month["2024-02"]
    ]
    report = mosaic.RunReport()
    outputs = mosaic.build_monthly_mosaics(
        {"2024-01": by_month["2024-01"], "2024-02": broken, "2024-03": by_month["2024-03"]},
        BBOX,
        EPSG,
        tmp_path / "out",
        settings=settings(),
        sign=str,
        report=report,
        keep_going=True,
    )
    assert report.months_not_written == ["2024-02"]
    assert list(outputs) == ["2024-01", "2024-03"]
    jan = mosaic.load_mosaic(outputs["2024-01"])
    mar = mosaic.load_mosaic(outputs["2024-03"])
    assert jan["carried_from"] == []
    assert mar["carried_from"] == ["2024-01", mosaic.bands_sha256(jan["bands"])]


def test_suggested_memory_always_covers_the_need():
    planned = settings(memory_budget=1024)
    for height in range(1000, 6000, 97):
        months = {"2024-01": [object()] * 12}
        with pytest.raises(mosaic.MemoryBudgetError) as error:
            mosaic.check_memory(months, planned, height, 2000)  # ty: ignore[invalid-argument-type]
        need = mosaic.fixed_bytes(planned, 4, height, 2000) + mosaic.month_bytes(
            12, 4, height, 2000
        )
        suggested = str(error.value).split("--memory with at least ")[1].split("GB")[0]
        assert performance.parse_bytes(f"{suggested}GB") >= need, height


def test_a_failed_name_resolve_waits_out_curl_and_keeps_the_request_limit(
    tmp_path, no_backoff, monkeypatch
):
    """libcurl caches a failed resolve on the thread for 30 s, so the retry waits past it, and
    a local resolver failure is not taken as the host pushing back."""
    _, by_month = build_case(tmp_path)
    jan = {"2024-01": by_month["2024-01"][:2]}
    monkeypatch.setattr(mosaic, "RESOLVE_RETRY_SECONDS", 0.3)
    read_asset = mosaic.read_asset
    failed: set[str] = set()

    def flaky(href, *args, **kwargs):
        if href not in failed:
            failed.add(href)
            raise rasterio.errors.RasterioIOError("CURL error: Could not resolve host: example")
        return read_asset(href, *args, **kwargs)

    monkeypatch.setattr(mosaic, "read_asset", flaky)
    limiter = performance.AdaptiveLimiter(4, 4, adaptive=True)
    report = mosaic.RunReport()
    outputs = mosaic.build_monthly_mosaics(
        jan,
        BBOX,
        EPSG,
        tmp_path / "out",
        settings=settings(requests=4, max_requests=4),
        sign=str,
        report=report,
        limiter=limiter,
    )
    assert report.read_retries == len(failed) and report.scenes_failed == 0
    assert limiter.limit == 4 and limiter.failures == 0
    assert report.wall_seconds >= 0.3
    expected = reference_mosaics(jan, _grid())["2024-01"][0]
    np.testing.assert_array_equal(load(outputs["2024-01"])[0], expected)
