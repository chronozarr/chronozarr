"""Encoding refuses monthly files that would put misleading time steps in a store."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("rasterio")
pytest.importorskip("planetary_computer")
pytest.importorskip("pystac_client")
pytest.importorskip("yaml")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "sentinel2_pc"))
import ingest
from rasterio.crs import CRS  # ty: ignore[unresolved-import]  (compiled module, no stubs)

from chronozarr.schema import parse_provenance
from tests.test_ingest_mosaic import (
    BANDS,
    BBOX,
    EPSG,
    build_case,
    mosaic,
    reference_mosaics,
    scene,
    settings,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(mosaic, "READ_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(mosaic, "READ_ATTEMPTS", 2)


def run(by_month, out: Path, keep_going: bool = True) -> dict:
    return mosaic.build_monthly_mosaics(
        by_month, BBOX, EPSG, out, settings=settings(), sign=str, keep_going=keep_going
    )


def test_incomplete_months_need_keep_going_and_are_named_in_provenance(tmp_path):
    _, by_month = build_case(tmp_path)  # one scene of 2024-02 cannot be read
    out = tmp_path / "mosaics"
    run(by_month, out)

    with pytest.raises(SystemExit, match=r"2024-02: e\b"), ingest.open_mosaic_stack(out):
        pass

    with ingest.open_mosaic_stack(out, keep_going=True) as stack:
        incomplete = stack.incomplete
    assert incomplete == {"2024-02": ["e"]}
    provenance = ingest.provenance_for(incomplete)
    assert (
        "INCOMPLETE MONTHS" in provenance["notes"] and "2024-02 without e" in provenance["notes"]
    )
    parse_provenance(provenance)  # still a valid chronozarr provenance object

    store = tmp_path / "store"
    ingest.encode(out, store, keep_going=True)
    root = json.loads((store / "zarr.json").read_text())
    notes = root["attributes"]["chronozarr"]["provenance"]["notes"]
    assert "2024-02 without e" in notes


def test_complete_months_keep_the_plain_provenance(tmp_path):
    _, by_month = build_case(tmp_path)
    out = tmp_path / "mosaics"
    run({"2024-01": by_month["2024-01"], "2024-03": by_month["2024-03"]}, out)
    with ingest.open_mosaic_stack(out) as stack:
        incomplete = stack.incomplete
    assert incomplete == {}
    assert ingest.provenance_for(incomplete) is ingest.PROVENANCE


def test_a_skipped_month_leaves_a_consistent_chain(tmp_path):
    _, by_month = build_case(tmp_path)
    broken = [
        scene(s.item_id, s.datetime, {k: v + ".missing" for k, v in s.asset_hrefs.items()})
        for s in by_month["2024-02"]
    ]
    out = tmp_path / "mosaics"
    run({"2024-01": by_month["2024-01"], "2024-02": broken, "2024-03": by_month["2024-03"]}, out)
    with ingest.open_mosaic_stack(out) as stack:
        times = [str(t)[:7] for t in stack.data["time"].values]
    assert times == ["2024-01", "2024-03"]


def test_a_month_rebuilt_after_its_successor_is_refused(tmp_path):
    """Re-running January after March was built from it leaves March's carried-forward pixels
    from the old January: encoding must not mix them."""
    _, by_month = build_case(tmp_path)
    out = tmp_path / "mosaics"
    run({"2024-01": by_month["2024-01"], "2024-03": by_month["2024-03"]}, out)
    (out / "2024-01.tif").unlink()
    run({"2024-01": by_month["2024-01"][:2]}, out)  # a different January
    with pytest.raises(SystemExit, match=r"2024-03 \(filled from 2024-01"):
        ingest.open_mosaic_stack(out).__enter__()


def test_a_month_inserted_before_its_successor_is_refused(tmp_path):
    _, by_month = build_case(tmp_path)
    out = tmp_path / "mosaics"
    run({"2024-01": by_month["2024-01"], "2024-03": by_month["2024-03"]}, out)
    run({"2024-02": by_month["2024-02"][1:]}, out)  # February arrives later
    with pytest.raises(SystemExit, match=r"2024-03 \(filled from 2024-01; the stack has 2024-02"):
        ingest.open_mosaic_stack(out).__enter__()


def test_files_from_before_the_chain_record_are_accepted(tmp_path):
    out = tmp_path / "mosaics"
    out.mkdir()
    for month in ("2024-01", "2024-02"):
        np.savez_compressed(
            out / f"{month}.npz",
            bands=np.ones((4, 3, 3), dtype=np.uint16),
            coverage=np.ones((3, 3), dtype=np.float32),
            transform=np.array([10.0, 0, 0, 0, -10.0, 0]),
            epsg=np.array(EPSG),
            band_names=np.array(["B02", "B03", "B04", "B08"]),
        )
    with ingest.open_mosaic_stack(out) as stack:
        assert stack.data.shape == (2, 4, 3, 3) and stack.incomplete == {}
        np.testing.assert_array_equal(stack.data.values, np.ones((2, 4, 3, 3)))
        assert stack.coverage is None  # no scenes_searched: the fraction cannot become a count
    assert sorted(p.name for p in out.iterdir()) == ["2024-01.npz", "2024-02.npz"]  # untouched


def test_the_store_holds_every_month_read_lazily(tmp_path, monkeypatch):
    """Encoding from the monthly files one cell at a time gives the months' values."""
    import chronozarr

    monkeypatch.setattr(mosaic, "CELL", 32)
    _, by_month = build_case(tmp_path)
    out = tmp_path / "mosaics"
    outputs = mosaic.build_monthly_mosaics(
        by_month,
        BBOX,
        EPSG,
        out,
        settings=settings(),
        sign=str,
        keep_going=True,
        strip_rows=64,
    )
    store_dir = tmp_path / "store"
    ingest.encode(out, store_dir, keep_going=True)
    store = chronozarr.open_store(store_dir)
    for t, key in enumerate(sorted(outputs)):
        month = mosaic.load_mosaic(outputs[key])
        np.testing.assert_array_equal(store.read(t), month["bands"], err_msg=key)
        np.testing.assert_array_equal(store.read_coverage(t), month["valid_count"], err_msg=key)


# --- coverage is a count of valid scenes -----------------------------------------------------


def test_the_store_holds_the_number_of_valid_scenes(tmp_path):
    """Coverage counts scenes (chronozarr spec 4.3), not a 0/1 flag; a month without scenes is
    0 everywhere."""
    import chronozarr

    grid, by_month = build_case(tmp_path)
    cases = {"2024-01": by_month["2024-01"], "2024-02": []}
    out = tmp_path / "mosaics"
    outputs = run(cases, out)
    expected = reference_mosaics({"2024-01": by_month["2024-01"]}, grid)["2024-01"][1] * 4
    expected = np.rint(expected).astype(np.uint8)
    assert expected.max() > 1  # a flag would not tell these apart
    np.testing.assert_array_equal(mosaic.load_mosaic(outputs["2024-01"])["valid_count"], expected)

    store_dir = tmp_path / "store"
    ingest.encode(out, store_dir)
    store = chronozarr.open_store(store_dir)
    np.testing.assert_array_equal(store.read_coverage(0), expected)
    february = store.read_coverage(1)
    assert february is not None and not february.any()


def test_coverage_saturates_at_255(tmp_path):
    transform, height, width = mosaic.compute_target_grid(BBOX, EPSG)
    grid = mosaic.Grid(transform, CRS.from_epsg(EPSG), height, width)
    out = tmp_path / "mosaics"
    out.mkdir()
    count = np.full((height, width), 300, dtype=np.uint16)
    count[0, 0] = 255
    count[0, 1] = 7
    mosaic.save_month(
        out / "2024-01.tif",
        grid,
        EPSG,
        list(BANDS),
        np.ones((4, height, width), dtype=np.uint16),
        count,
        {
            "band_names": list(BANDS),
            "scenes_searched": 300,
            "scenes_failed": [],
            "scenes_failed_windows": {},
            "carried_from": [],
        },
    )
    with ingest.open_mosaic_stack(out) as stack:
        assert stack.coverage is not None and stack.coverage.dtype == np.uint8
        plane = stack.coverage.values[0]
    assert plane[0, 0] == 255 and plane[0, 1] == 7 and plane.max() == 255


def test_months_without_scenes_searched_give_a_store_without_coverage(tmp_path, caplog):
    """An .npz month from before scenes_searched holds k / n without n: no count can be made,
    and a flag would be a wrong count, so the store has no coverage variable."""
    import chronozarr

    grid, by_month = build_case(tmp_path)
    expected = reference_mosaics({"2024-01": by_month["2024-01"]}, grid)["2024-01"]
    out = tmp_path / "mosaics"
    out.mkdir()
    np.savez_compressed(
        out / "2024-01.npz",
        bands=expected[0],
        coverage=expected[1],
        transform=np.array(list(grid.transform)[:6]),
        epsg=np.array(EPSG),
        band_names=np.array(list(BANDS)),
    )
    run({"2024-01": by_month["2024-01"], "2024-03": by_month["2024-03"]}, out)
    with caplog.at_level("WARNING", logger="ingest"), ingest.open_mosaic_stack(out) as stack:
        assert stack.coverage is None and stack.data.sizes["time"] == 2
    assert "2024-01" in caplog.text and "scenes_searched" in caplog.text
    store_dir = tmp_path / "store"
    ingest.encode(out, store_dir)
    assert chronozarr.open_store(store_dir).read_coverage(0) is None
