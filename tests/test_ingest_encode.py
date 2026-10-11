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
    with ingest.open_mosaic_stack(out, no_coverage=True) as stack:
        assert stack.data.shape == (2, 4, 3, 3) and stack.incomplete == {}
        np.testing.assert_array_equal(stack.data.values, np.ones((2, 4, 3, 3)))
        assert stack.coverage is None
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


def legacy_case(tmp_path, with_tif_month: bool = True):
    """mosaics/ with 2024-01 as an .npz saved without scenes_searched (coverage = k / 4, k up
    to 4) and, optionally, 2024-03 as a current GeoTIFF; returns (out, expected counts)."""
    grid, by_month = build_case(tmp_path)
    bands, fraction = reference_mosaics({"2024-01": by_month["2024-01"]}, grid)["2024-01"]
    counts = np.rint(fraction * 4).astype(np.uint8)
    assert counts.max() > 1 and np.array_equal(counts.astype(np.float32) / 4, fraction)
    out = tmp_path / "mosaics"
    out.mkdir()
    write_legacy(out / "2024-01.npz", grid, bands, fraction)
    if with_tif_month:
        run({"2024-01": by_month["2024-01"], "2024-03": by_month["2024-03"]}, out)
    return out, counts


def write_legacy(path: Path, grid, bands, fraction) -> None:
    np.savez_compressed(
        path,
        bands=bands,
        coverage=fraction,
        transform=np.array(list(grid.transform)[:6]),
        epsg=np.array(EPSG),
        band_names=np.array(list(BANDS)),
    )


def test_months_without_scenes_searched_are_refused_by_name(tmp_path, caplog):
    out, _ = legacy_case(tmp_path)
    with pytest.raises(SystemExit) as refusal:
        ingest.open_mosaic_stack(out).__enter__()
    message = str(refusal.value)
    assert "1 months" in message and "2024-01" in message and "2024-03" not in message
    assert "--scenes-searched" in message and "--no-coverage" in message
    with pytest.raises(SystemExit, match="2024-01"):
        ingest.encode(out, tmp_path / "store")
    assert not (tmp_path / "store").exists()


def test_no_coverage_writes_a_store_without_coverage(tmp_path):
    import chronozarr

    out, _ = legacy_case(tmp_path)
    ingest.encode(out, tmp_path / "store", no_coverage=True)
    store = chronozarr.open_store(tmp_path / "store")
    assert store.read_coverage(0) is None and store.read_coverage(1) is None


def test_no_coverage_drops_coverage_even_when_counts_are_known(tmp_path):
    import chronozarr

    out, _ = legacy_case(tmp_path)
    ingest.encode(out, tmp_path / "store", scenes_searched={"2024-01": 4}, no_coverage=True)
    assert chronozarr.open_store(tmp_path / "store").read_coverage(0) is None


def test_a_correct_mapping_backfills_exact_counts(tmp_path):
    import chronozarr

    out, counts = legacy_case(tmp_path)
    ingest.encode(out, tmp_path / "store", scenes_searched={"2024-01": 4})
    store = chronozarr.open_store(tmp_path / "store")
    np.testing.assert_array_equal(store.read_coverage(0), counts)
    carried = store.read_coverage(1)
    assert carried is not None and carried.shape == counts.shape  # the GeoTIFF month is there too


def test_a_mapping_for_other_months_is_ignored_and_none_is_guessed(tmp_path):
    out, _ = legacy_case(tmp_path, with_tif_month=False)
    mapping = {"2024-01": 4, "1999-12": 9}
    with ingest.open_mosaic_stack(out, scenes_searched=mapping) as stack:
        assert stack.coverage is not None
    with pytest.raises(SystemExit, match="2024-01"):
        ingest.open_mosaic_stack(out, scenes_searched={"1999-12": 9}).__enter__()


@pytest.mark.parametrize("wrong", [3, 5, 7])
def test_a_wrong_n_is_rejected(tmp_path, wrong):
    """0.25 and 0.5 are not float32(k / 3), k / 5 or k / 7 for any whole k."""
    out, _ = legacy_case(tmp_path)
    with pytest.raises(SystemExit, match=rf"no count is guessed: 2024-01 \(n={wrong}\)"):
        ingest.open_mosaic_stack(out, scenes_searched={"2024-01": wrong}).__enter__()


def test_counts_from_fraction_checks_every_value_bit_for_bit():
    n = 7
    fraction = np.arange(n + 1, dtype=np.float32) / n
    np.testing.assert_array_equal(ingest.counts_from_fraction(fraction, n), np.arange(n + 1))
    nudged = fraction.copy()
    nudged[3] = np.nextafter(nudged[3], np.float32(1))  # one ulp off: not what mosaic.py wrote
    with pytest.raises(ValueError, match="not float32"):
        ingest.counts_from_fraction(nudged, n)
    with pytest.raises(ValueError, match="not float32"):
        ingest.counts_from_fraction(np.array([0.5, 1.5], dtype=np.float32), 2)
    with pytest.raises(ValueError, match="not float32"):
        ingest.counts_from_fraction(np.array([np.nan], dtype=np.float32), 2)


def test_a_month_of_only_zero_and_one_confirms_n_only_when_n_is_1(tmp_path):
    """Nothing in 0 / 1 distinguishes n=3 from n=1: a count of 3 would be invented, so it is
    refused; with n=1 the fraction is the count."""
    flags = np.array([[0.0, 1.0], [1.0, 0.0]], dtype=np.float32)
    with pytest.raises(ValueError, match="only 0 and 1"):
        ingest.counts_from_fraction(flags, 3)
    np.testing.assert_array_equal(ingest.counts_from_fraction(flags, 1), flags.astype(np.uint16))

    grid, by_month = build_case(tmp_path)
    bands = reference_mosaics({"2024-01": by_month["2024-01"]}, grid)["2024-01"][0]
    out = tmp_path / "mosaics"
    out.mkdir()
    binary = np.zeros(bands.shape[1:], dtype=np.float32)
    binary[0, 0] = 1.0
    write_legacy(out / "2024-01.npz", grid, bands, binary)
    with pytest.raises(SystemExit, match="only 0 and 1, which cannot confirm n=3"):
        ingest.open_mosaic_stack(out, scenes_searched={"2024-01": 3}).__enter__()
    with ingest.open_mosaic_stack(out, scenes_searched={"2024-01": 1}) as stack:
        assert stack.coverage is not None
        assert stack.coverage.values[0, 0, 0] == 1 and stack.coverage.values.sum() == 1


def test_a_mapping_that_disagrees_with_a_month_record_is_an_error(tmp_path):
    out, _ = legacy_case(tmp_path)
    recorded = ingest.month_record(out / "2024-03.tif")["scenes_searched"]
    agreeing = {"2024-01": 4, "2024-03": recorded}
    with ingest.open_mosaic_stack(out, scenes_searched=agreeing):
        pass
    disagreeing = {"2024-01": 4, "2024-03": recorded + 1}
    with pytest.raises(SystemExit, match=rf"2024-03: file records n={recorded}, mapping says"):
        ingest.open_mosaic_stack(out, scenes_searched=disagreeing).__enter__()


def test_the_scenes_searched_file_takes_a_plain_mapping_or_the_audit_report(tmp_path):
    plain = tmp_path / "plain.json"
    plain.write_text(json.dumps({"2024-01": 4, "2024-02": 2}))
    audit = tmp_path / "audit.json"
    audit.write_text(json.dumps({"2024-01": {"n": 4, "missing": []}, "2024-02": {"n": 2}}))
    expected = {"2024-01": 4, "2024-02": 2}
    assert ingest.read_scenes_searched(plain) == expected
    assert ingest.read_scenes_searched(audit) == expected
    for text in ("[]", "{}", '{"2024-01": "4"}', '{"2024-01": {"missing": []}}', "not json"):
        bad = tmp_path / "bad.json"
        bad.write_text(text)
        with pytest.raises(SystemExit):
            ingest.read_scenes_searched(bad)
