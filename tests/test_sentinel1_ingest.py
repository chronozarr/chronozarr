"""examples/sentinel1_pc/ingest.py: grid, pass grouping, windows, multilook, monthly means."""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import asdict, replace
from datetime import date
from pathlib import Path

import numpy as np
import pytest


def load_ingest():
    """The example script as a module, by path: another example is also called `ingest`."""
    path = Path(__file__).resolve().parents[1] / "examples" / "sentinel1_pc" / "ingest.py"
    spec = importlib.util.spec_from_file_location("sentinel1_ingest", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve the module's annotations here
    spec.loader.exec_module(module)
    return module


ingest = load_ingest()

pytestmark = pytest.mark.unit

GRID = ingest.Grid(epsg=32718, left=1000.0, top=9000.0, width=4, height=3)


def make_slice(
    n: int,
    start: str = "2024-01-05T10:37:09Z",
    relative_orbit: int = 171,
    absolute_orbit: int = 100,
    platform: str = "sentinel-1a",
    epsg: int = 32718,
    transform: tuple = (10.0, 0.0, 1000.0, 0.0, -10.0, 9000.0),
    shape: tuple = (6, 8),
) -> ingest.Slice:
    return ingest.Slice(
        id=f"S{n}",
        start=start,
        platform=platform,
        absolute_orbit=absolute_orbit,
        relative_orbit=relative_orbit,
        direction="descending",
        epsg=epsg,
        transform=transform,
        shape=shape,
        vv=f"https://x/{n}/vv.tiff",
        vh=f"https://x/{n}/vh.tiff",
    )


def test_grid_is_level_1_of_the_sentinel2_grid_of_the_same_box():
    pytest.importorskip("rasterio")
    # The Sentinel-2 Ucayali store's level 0: origin (485650, 9169880), 2759 x 2765 px at 10 m.
    grid = ingest.grid_for((-75.13, -7.76, -74.88, -7.51))
    assert grid == ingest.Grid(32718, 485650.0, 9169880.0, 1380, 1383)
    assert grid.transform == [20.0, 0.0, 485650.0, 0.0, -20.0, 9169880.0]


def test_parse_item_reads_the_fields_and_skips_what_is_not_iw_vv_vh():
    properties = {
        "datetime": "2024-01-05T10:37:21Z",
        "start_datetime": "2024-01-05T10:37:09Z",
        "platform": "SENTINEL-1A",
        "sar:instrument_mode": "IW",
        "sar:polarizations": ["VV", "VH"],
        "sat:absolute_orbit": 52000,
        "sat:relative_orbit": 171,
        "sat:orbit_state": "descending",
        "proj:code": "EPSG:32718",
        "proj:transform": [10, 0, 367340, 0, -10, 9301400, 0, 0, 1],
        "proj:shape": [22293, 28738],
    }
    assets = {"vv": {"href": "https://x/vv.tiff"}, "vh": {"href": "https://x/vh.tiff"}}
    s = ingest.parse_item("A", properties, assets)
    assert s is not None
    assert (s.start, s.platform, s.relative_orbit, s.epsg) == (
        "2024-01-05T10:37:09Z",
        "sentinel-1a",
        171,
        32718,
    )
    assert s.transform == (10.0, 0.0, 367340.0, 0.0, -10.0, 9301400.0)
    assert s.shape == (22293, 28738)
    assert ingest.parse_item("A", {**properties, "sar:instrument_mode": "EW"}, assets) is None
    assert ingest.parse_item("A", {**properties, "sar:polarizations": ["VV"]}, assets) is None


def test_select_series_groups_a_pass_and_takes_the_orbit_with_most_dates():
    slices = [
        # orbit 171: two passes, the second in two slices
        make_slice(1, "2024-01-05T10:37:09Z", absolute_orbit=100),
        make_slice(2, "2024-01-17T10:37:34Z", absolute_orbit=275),
        make_slice(3, "2024-01-17T10:37:09Z", absolute_orbit=275),
        # orbit 120: one pass in three slices
        *(make_slice(10 + i, "2024-01-09T23:26:22Z", relative_orbit=120) for i in range(3)),
    ]
    (relative, direction), acquisitions = ingest.select_series(slices)
    assert (relative, direction) == (171, "descending")
    assert [[s.id for s in a.slices] for a in acquisitions] == [["S1"], ["S3", "S2"]]
    assert acquisitions[1].key == "S3"

    (relative, _), pinned = ingest.select_series(slices, orbit=120)
    assert relative == 120 and len(pinned) == 1 and len(pinned[0].slices) == 3
    with pytest.raises(SystemExit, match="relative orbit 7"):
        ingest.select_series(slices, orbit=7)


def test_stac_problems_fail_off_grid_slices_and_defer_implausible_transforms():
    aligned = make_slice(1, transform=(10.0, 0.0, 900.0, 0.0, -10.0, 9100.0))
    other_zone = make_slice(2, epsg=32717)
    fractional = make_slice(3, transform=(10.0, 0.0, 905.0, 0.0, -10.0, 9100.0))
    # Planetary Computer items whose proj:transform is not north-up; their files are.
    south_up = make_slice(4, transform=(10.0, 0.0, 367.08, 0.0, 10.0, 9078.58))
    problems, deferred = ingest.stac_problems(GRID, [aligned, other_zone, fractional, south_up])
    assert len(problems) == 2
    assert "EPSG:32717" in problems[0] and "fraction of a pixel" in problems[1]
    assert deferred == [south_up]
    assert ingest.misaligned(GRID, aligned) is None


def test_window_of_clips_the_box_to_the_slice():
    # The box's 10 m window is 6 x 8 samples from (1000, 9000).
    inside = make_slice(1, transform=(10.0, 0.0, 900.0, 0.0, -10.0, 9100.0), shape=(100, 100))
    assert ingest.window_of(GRID, inside) == (
        slice(10, 16),
        slice(10, 18),
        slice(0, 6),
        slice(0, 8),
    )
    # A slice that starts 30 m east of and 20 m south of the box's corner.
    corner = make_slice(2, transform=(10.0, 0.0, 1030.0, 0.0, -10.0, 8980.0), shape=(3, 100))
    assert ingest.window_of(GRID, corner) == (slice(0, 3), slice(0, 5), slice(2, 5), slice(3, 8))
    away = make_slice(3, transform=(10.0, 0.0, 5000.0, 0.0, -10.0, 9000.0))
    assert ingest.window_of(GRID, away) is None


def test_multilook_means_power_and_drops_blocks_with_an_invalid_sample():
    power = np.array(
        [[1.0, 3.0, 2.0, 2.0], [5.0, 7.0, np.nan, 2.0]],
        dtype=np.float32,
    )
    looked = ingest.multilook(power)
    assert looked.shape == (1, 2)
    assert looked[0, 0] == 4.0
    assert np.isnan(looked[0, 1])


def test_month_mean_averages_power_over_the_valid_acquisitions():
    nan = np.nan
    first = (np.array([[1.0, nan, nan]]), np.array([[0.1, nan, nan]]))
    second = (np.array([[3.0, 2.0, nan]]), np.array([[0.3, 0.2, nan]]))
    vv, vh, valid = ingest.month_mean([first, second])
    assert valid.tolist() == [[True, True, False]]
    np.testing.assert_allclose(vv[valid], [2.0, 2.0])
    np.testing.assert_allclose(vh[valid], [0.2, 0.2])


def test_stored_db_quantizes_to_a_hundredth_of_a_db_and_clips_at_minus_60():
    power = np.array([1.0, 0.1, 1e-9, 0.0])
    valid = np.array([True, True, True, False])
    assert ingest.stored_db(power, valid).tolist() == [6000, 5000, 0, 0]


def test_month_range_and_the_last_complete_month():
    assert ingest.month_range("2024-11-15", "2025-02-01") == [
        "2024-11",
        "2024-12",
        "2025-01",
        "2025-02",
    ]
    assert ingest.last_complete_month_end(date(2026, 10, 15)) == date(2026, 9, 30)
    assert ingest.last_complete_month_end(date(2026, 1, 1)) == date(2025, 12, 31)


def test_a_cached_acquisition_is_reused_only_for_the_same_slices(tmp_path):
    acquisition = ingest.Acquisition("sentinel-1a", 100, (make_slice(1), make_slice(2)))
    path = tmp_path / "S1.npz"
    assert ingest.cached_record(acquisition, GRID, path) is None
    np.savez(path, vv=np.zeros((GRID.height, GRID.width)), vh=np.zeros((GRID.height, GRID.width)))
    ingest.sidecar_of(path).write_text(
        json.dumps(
            {
                "slices": ["S1", "S2"],
                "grid": GRID.spatial_identity,
                "valid_fraction": 1.0,
                "npz": path.name,
            }
        )
    )
    assert ingest.cached_record(acquisition, GRID, path)["npz"] == str(path)
    one_more = ingest.Acquisition("sentinel-1a", 100, (*acquisition.slices, make_slice(3)))
    assert ingest.cached_record(one_more, GRID, path) is None


@pytest.fixture
def extracted_cache(tmp_path, monkeypatch):
    vv = np.ones((GRID.height, GRID.width), dtype=np.float32)
    vh = np.full_like(vv, 0.1)
    vv[0, 0] = vh[0, 0] = np.nan
    monkeypatch.setattr(ingest, "read_acquisition", lambda *_: (vv, vh))
    acquisition = ingest.Acquisition("sentinel-1a", 100, (make_slice(1), make_slice(2)))
    path = tmp_path / "original" / "S1.npz"
    record = ingest.extract(acquisition, GRID, path)
    return acquisition, path, record


def test_extract_writes_spatial_identity_and_reuses_a_relocated_json_cache(
    extracted_cache, tmp_path, monkeypatch
):
    acquisition, path, record = extracted_cache
    expected = {
        "crs": "EPSG:32718",
        "transform": [20.0, 0.0, 1000.0, 0.0, -20.0, 9000.0],
        "width": 4,
        "height": 3,
    }
    sidecar = json.loads(ingest.sidecar_of(path).read_text())
    assert record["grid"] == sidecar["grid"] == expected
    assert sidecar["npz"] == path.name
    with np.load(path) as cached:
        assert cached["vv"].shape == cached["vh"].shape == (3, 4)
        assert np.isnan(cached["vv"][0, 0]) and np.isnan(cached["vh"][0, 0])
        np.testing.assert_allclose(cached["vv"][1:], 1.0)
        np.testing.assert_allclose(cached["vh"][1:], 0.1)

    moved = tmp_path / "relocated"
    path.parent.rename(moved)
    monkeypatch.setattr(ingest, "extract", lambda *_: pytest.fail("cached pixels were read again"))
    restored = ingest.Grid(**json.loads(json.dumps(asdict(GRID))))
    [reused] = ingest.extract_all([acquisition], restored, moved, workers=1)
    assert reused["cached"] is True
    assert reused["npz"] == str(moved / path.name)
    assert reused["grid"] == expected


@pytest.mark.parametrize(
    "changes",
    [
        {"left": GRID.left + 20.0},  # same-sized box, translated one pixel
        {"epsg": 32717},
        {"width": GRID.width + 1},
        {"height": GRID.height + 1},
    ],
)
def test_cache_refuses_other_grids_before_any_extraction(extracted_cache, monkeypatch, changes):
    acquisition, path, _ = extracted_cache
    before = {p: p.read_bytes() for p in (path, ingest.sidecar_of(path))}
    uncached = ingest.Acquisition("sentinel-1a", 101, (make_slice(3, absolute_orbit=101),))
    monkeypatch.setattr(
        ingest,
        "ThreadPoolExecutor",
        lambda **_: pytest.fail("cache validation must finish before extraction starts"),
    )
    with pytest.raises(SystemExit, match=r"spatial identity.*--name.*--out-dir"):
        ingest.extract_all([uncached, acquisition], replace(GRID, **changes), path.parent, 1)
    assert {p: p.read_bytes() for p in before} == before
    assert not (path.parent / "S3.npz").exists()


@pytest.mark.parametrize("missing", ["identity", "sidecar"])
def test_legacy_cache_is_refused_without_extraction(extracted_cache, monkeypatch, missing):
    acquisition, path, _ = extracted_cache
    sidecar = ingest.sidecar_of(path)
    if missing == "identity":
        record = json.loads(sidecar.read_text())
        del record["grid"]
        sidecar.write_text(json.dumps(record))
    else:
        sidecar.unlink()
    before = path.read_bytes()
    uncached = ingest.Acquisition("sentinel-1a", 101, (make_slice(3, absolute_orbit=101),))
    monkeypatch.setattr(
        ingest,
        "ThreadPoolExecutor",
        lambda **_: pytest.fail("legacy cache must be refused before extraction starts"),
    )
    with pytest.raises(SystemExit, match=r"spatial identity.*--name.*--out-dir"):
        ingest.extract_all([uncached, acquisition], GRID, path.parent, workers=1)
    assert path.read_bytes() == before
    assert not (path.parent / "S3.npz").exists()


def test_extract_all_still_extracts_misses_and_changed_slices(extracted_cache):
    acquisition, path, _ = extracted_cache
    one_more = ingest.Acquisition("sentinel-1a", 100, (*acquisition.slices, make_slice(3)))
    uncached = ingest.Acquisition("sentinel-1a", 101, (make_slice(4, absolute_orbit=101),))
    records = ingest.extract_all([one_more, uncached], GRID, path.parent, workers=1)
    assert [r["slices"] for r in records] == [["S1", "S2", "S3"], ["S4"]]
    assert all("cached" not in r and Path(r["npz"]).is_file() for r in records)
    sidecar = json.loads(ingest.sidecar_of(path).read_text())
    assert sidecar["slices"] == ["S1", "S2", "S3"]
    assert sidecar["grid"] == records[0]["grid"]
