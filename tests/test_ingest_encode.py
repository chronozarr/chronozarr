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

    with pytest.raises(SystemExit, match=r"2024-02: e\b"):
        ingest.load_mosaic_stack(out)

    _, _, incomplete = ingest.load_mosaic_stack(out, keep_going=True)
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
    _, _, incomplete = ingest.load_mosaic_stack(out)
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
    data, _, _ = ingest.load_mosaic_stack(out)
    assert [str(t)[:7] for t in data["time"].values] == ["2024-01", "2024-03"]


def test_a_month_rebuilt_after_its_successor_is_refused(tmp_path):
    """Re-running January after March was built from it leaves March's carried-forward pixels
    from the old January: encoding must not mix them."""
    _, by_month = build_case(tmp_path)
    out = tmp_path / "mosaics"
    run({"2024-01": by_month["2024-01"], "2024-03": by_month["2024-03"]}, out)
    (out / "2024-01.npz").unlink()
    run({"2024-01": by_month["2024-01"][:2]}, out)  # a different January
    with pytest.raises(SystemExit, match=r"2024-03 \(filled from 2024-01"):
        ingest.load_mosaic_stack(out)


def test_a_month_inserted_before_its_successor_is_refused(tmp_path):
    _, by_month = build_case(tmp_path)
    out = tmp_path / "mosaics"
    run({"2024-01": by_month["2024-01"], "2024-03": by_month["2024-03"]}, out)
    run({"2024-02": by_month["2024-02"][1:]}, out)  # February arrives later
    with pytest.raises(SystemExit, match=r"2024-03 \(filled from 2024-01; the stack has 2024-02"):
        ingest.load_mosaic_stack(out)


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
    data, _, incomplete = ingest.load_mosaic_stack(out)
    assert data.shape == (2, 4, 3, 3) and incomplete == {}
