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

from chronozarr.schema import parse_provenance
from tests.test_ingest_mosaic import (
    BBOX,
    EPSG,
    build_case,
    mosaic,
    scene,
    settings,
)

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
        np.testing.assert_array_equal(stack.coverage.values, np.ones((2, 3, 3)))
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
        np.testing.assert_array_equal(
            store.read_coverage(t), (month["valid_count"] > 0).astype(np.uint8), err_msg=key
        )
