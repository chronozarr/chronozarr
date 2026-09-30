"""Temporal encoding is optional: none, star-delta, and the auto choice between them."""

from __future__ import annotations

import importlib
import warnings

import numpy as np
import pytest
import zarr
from zarr.errors import ZarrUserWarning

import chronozarr
from chronozarr import schema
from chronozarr.schema import Selection
from tests.synthetic import (
    build_store,
    make_correlated,
    make_da,
    make_independent,
    make_truth,
    reference_downsample,
)

pytestmark = pytest.mark.unit

encode_module = importlib.import_module("chronozarr.encode")


@pytest.mark.parametrize("shard", [True, False], ids=["sharded", "unsharded"])
def test_none_stores_true_values_at_every_level(tmp_path, shard):
    truth = make_truth(5, 2, 40, 50)
    report = build_store(
        tmp_path / "s", truth, shard=shard, anchor_interval=2, chunk_size=16, encoding="none"
    )
    assert report.encoding == "none"
    store = chronozarr.open_store(tmp_path / "s")
    assert store.attrs.temporal.encoding == "none"
    level = truth
    for lod in range(len(store.levels)):
        if lod:
            level = reference_downsample(level)
        assert np.array_equal(store.to_xarray(lod=lod).values, level)
        # a plain Zarr reader sees true values at every timestep, no adapter needed
        raw = zarr.open_group(str(tmp_path / "s"), mode="r")[str(lod)]["data"][:]
        assert np.array_equal(raw, level)
    assert chronozarr.validate(tmp_path / "s") == []


def test_none_attrs_carry_no_anchor_schedule(tmp_path):
    build_store(tmp_path / "s", make_truth(3, 1, 20, 20), shard=True, encoding="none")
    block = zarr.open_group(str(tmp_path / "s"), mode="r").attrs["chronozarr"]
    assert block["temporal"] == {"encoding": "none"}
    temporal = chronozarr.open_store(tmp_path / "s").attrs.temporal
    assert temporal.anchor_indices == (0, 1, 2)  # every timestep reads directly
    assert dict(temporal.delta_reference) == {}


def test_none_reads_one_chunk_per_timestep(tmp_path):
    from zarr.storage import LocalStore

    from tests.test_reads import CountingStore

    build_store(
        tmp_path / "s", make_truth(4, 2, 40, 50), shard=False, chunk_size=16, encoding="none"
    )
    counting = CountingStore(LocalStore(tmp_path / "s", read_only=True))
    store = chronozarr.open_store(counting)
    counting.reads.clear()
    store.read_cell(3, 1, 1)
    assert [key for key, _ in counting.reads if "/data/c/" in key] == ["0/data/c/3/0/1/1"]


def test_volatility_is_written_for_none_stores_and_matches_star_delta(tmp_path):
    truth = make_truth(6, 1, 40, 50)
    build_store(
        tmp_path / "a", truth, shard=True, anchor_interval=3, chunk_size=16, encoding="none"
    )
    build_store(tmp_path / "b", truth, shard=True, anchor_interval=3, chunk_size=16)
    plain = zarr.open_group(str(tmp_path / "a"), mode="r")["volatility"][:]
    delta = zarr.open_group(str(tmp_path / "b"), mode="r")["volatility"][:]
    assert plain.any()
    assert np.array_equal(plain, delta)


def test_auto_keeps_star_delta_for_a_static_scene(tmp_path):
    truth = make_correlated(6, 2, 64, 64)
    report = chronozarr.encode(make_da(truth), tmp_path / "s", chunk_size=16, anchor_interval=3)
    assert report.encoding == "star-delta"
    assert report.selection is not None
    assert report.selection.ratio <= 0.85
    temporal = zarr.open_group(str(tmp_path / "s"), mode="r").attrs["chronozarr"]["temporal"]
    assert temporal["encoding"] == "star-delta"
    assert temporal["selection"]["mode"] == "auto"
    assert temporal["selection"]["ratio"] == report.selection.ratio
    assert np.array_equal(chronozarr.open_store(tmp_path / "s").to_xarray().values, truth)


def test_auto_falls_back_to_none_when_differencing_does_not_help(tmp_path):
    truth = make_independent(6, 1, 64, 64)
    report = chronozarr.encode(make_da(truth), tmp_path / "s", chunk_size=16, anchor_interval=3)
    assert report.encoding == "none"
    assert report.selection is not None
    assert report.selection.ratio > 0.85
    temporal = zarr.open_group(str(tmp_path / "s"), mode="r").attrs["chronozarr"]["temporal"]
    assert temporal["encoding"] == "none"
    assert temporal["selection"]["sampled_cells"] == report.selection.sampled_cells
    assert np.array_equal(chronozarr.open_store(tmp_path / "s").to_xarray().values, truth)


@pytest.mark.parametrize(
    ("ratio", "expected"), [(0.85, "star-delta"), (0.8501, "none"), (0.3, "star-delta")]
)
def test_auto_threshold_is_at_most_085(tmp_path, monkeypatch, ratio, expected):
    monkeypatch.setattr(encode_module, "_measure_star_delta", lambda *a, **k: Selection(3, ratio))
    report = chronozarr.encode(
        make_da(make_truth(4, 1, 20, 20)), tmp_path / "s", chunk_size=16, anchor_interval=2
    )
    assert report.encoding == expected


@pytest.mark.parametrize(
    ("height", "width", "chunk_size", "expected_cells"),
    [
        (16, 16, 16, 1),  # one cell: all of them
        (16, 32, 16, 2),  # two cells: all of them
        (32, 32, 16, 3),  # four cells: at least 3
        (40, 50, 16, 3),  # twelve cells: still 3 (10 % is 2)
        (80, 80, 8, 10),  # 100 cells: 10 %
    ],
)
def test_auto_samples_at_least_three_cells_or_all(
    tmp_path, height, width, chunk_size, expected_cells
):
    report = chronozarr.encode(
        make_da(make_correlated(3, 1, height, width)),
        tmp_path / "s",
        chunk_size=chunk_size,
        anchor_interval=2,
        n_lods=1,
    )
    assert report.selection is not None
    assert report.selection.sampled_cells == expected_cells


@pytest.mark.parametrize("forced", ["none", "star-delta"])
def test_forced_encodings_record_no_selection(tmp_path, forced):
    report = build_store(
        tmp_path / "s", make_truth(4, 1, 20, 20), shard=True, chunk_size=16, encoding=forced
    )
    assert report.encoding == forced
    assert report.selection is None
    temporal = zarr.open_group(str(tmp_path / "s"), mode="r").attrs["chronozarr"]["temporal"]
    assert "selection" not in temporal


@pytest.mark.parametrize(
    ("n_time", "interval"), [(1, 6), (5, 1)], ids=["one-timestep", "anchor-interval-1"]
)
def test_auto_without_deltas_picks_none_and_records_nothing(tmp_path, n_time, interval):
    report = chronozarr.encode(
        make_da(make_truth(n_time, 1, 20, 20)),
        tmp_path / "s",
        chunk_size=16,
        anchor_interval=interval,
    )
    assert report.encoding == "none"
    assert report.selection is None


def test_unknown_encoding_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="encoding must be 'auto'"):
        chronozarr.encode(
            make_da(make_truth(2, 1, 8, 8)), tmp_path / "s", chunk_size=4, encoding="chain"
        )
    assert not (tmp_path / "s").exists()


def test_readers_still_accept_a_v01_star_delta_store(tmp_path):
    """A v0.1 block (string bands, no levels, version 0.1.0) decodes as before."""
    truth = make_truth(3, 2, 40, 50)
    build_store(tmp_path / "s", truth, shard=True, chunk_size=16)
    root = zarr.open_group(str(tmp_path / "s"), mode="r+", use_consolidated=False)
    block = dict(root.attrs["chronozarr"])
    block["spec_version"] = "0.1.0"
    block["bands"] = [b["name"] for b in block["bands"]]
    for key in ("band_names", "levels", "shard_bytes"):
        block.pop(key, None)
    root.attrs["chronozarr"] = block
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ZarrUserWarning)
        zarr.consolidate_metadata(str(tmp_path / "s"))
    store = chronozarr.open_store(tmp_path / "s")
    assert store.attrs.spec_version == "0.1.0"
    assert store.bands == ("B04", "B08")
    assert np.array_equal(store.to_xarray().values, truth)
    assert chronozarr.validate(tmp_path / "s") == []
    assert schema.SPEC_VERSION == "0.2.0"
