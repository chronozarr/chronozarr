"""Residuals are modular: no clipping, no rejection, every difference roundtrips."""

from __future__ import annotations

import numpy as np
import pytest
import zarr

import chronozarr
from tests.synthetic import make_da

pytestmark = pytest.mark.unit

# One 1x4 cell, two timesteps: anchor 0, delta 1 -> anchor 0. Columns:
#   0: wraps up     anchor 100,   value 60000 -> difference +59900 (> int16 max)
#   1: wraps down   anchor 50000, value 100   -> difference -49900 (< int16 min)
#   2: largest exact +  anchor 100,   value 32867 -> difference +32767
#   3: largest exact -  anchor 32868, value 100   -> difference -32768
ANCHOR = np.array([100, 50000, 100, 32868], dtype=np.uint16)
CURRENT = np.array([60000, 100, 32867, 100], dtype=np.uint16)


def _cube(anchor: np.ndarray, current: np.ndarray) -> np.ndarray:
    return np.stack([anchor, current])[:, None, None, :]  # (time, band, y, x)


def _encode(tmp_path, truth, **kwargs):
    kwargs.setdefault("anchor_interval", 2)
    kwargs.setdefault("chunk_size", 4)
    chronozarr.encode(make_da(truth), tmp_path / "s", encoding="star-delta", **kwargs)
    return chronozarr.open_store(tmp_path / "s")


@pytest.mark.parametrize("shard", [True, False], ids=["sharded", "unsharded"])
def test_wraparound_differences_roundtrip_exactly(tmp_path, shard):
    truth = _cube(ANCHOR, CURRENT)
    store = _encode(tmp_path, truth, shard=shard)
    assert store.read(1)[0, 0].tolist() == CURRENT.tolist()
    assert np.array_equal(store.to_xarray().values, truth)


def test_stored_residuals_are_the_difference_modulo_65536(tmp_path):
    _encode(tmp_path, _cube(ANCHOR, CURRENT), shard=False)
    raw = zarr.open_array(str(tmp_path / "s" / "0" / "data"), mode="r")
    assert raw[0, 0, 0].tolist() == ANCHOR.tolist()  # anchors are true values
    expected = (CURRENT.astype(np.int64) - ANCHOR.astype(np.int64)) % 65536
    assert raw[1, 0, 0].tolist() == expected.tolist()
    assert expected.tolist() == [59900, 15636, 32767, 32768]


def test_small_differences_are_bit_identical_to_the_v01_int16_view(tmp_path):
    truth = _cube(ANCHOR[2:], CURRENT[2:])  # +32767 and -32768: the extremes of int16
    _encode(tmp_path, truth, shard=False)
    raw = zarr.open_array(str(tmp_path / "s" / "0" / "data"), mode="r")[1, 0, 0]
    assert raw.view(np.int16).tolist() == [32767, -32768]


def test_uint8_residuals_wrap_modulo_256(tmp_path):
    anchor = np.array([5, 250, 0, 255], dtype=np.uint8)
    current = np.array([250, 5, 255, 0], dtype=np.uint8)
    truth = np.stack([anchor, current])[:, None, None, :]
    store = _encode(tmp_path, truth, shard=False)
    raw = zarr.open_array(str(tmp_path / "s" / "0" / "data"), mode="r")
    assert raw.dtype == np.uint8
    assert raw[1, 0, 0].tolist() == [245, 11, 255, 1]
    assert np.array_equal(store.to_xarray().values, truth)


@pytest.mark.parametrize("anchor_interval", [1, 2])
def test_full_range_uint16_roundtrips(tmp_path, anchor_interval):
    truth = _cube(np.array([0, 65535, 1, 65534], dtype=np.uint16), CURRENT)
    store = _encode(tmp_path, truth, anchor_interval=anchor_interval)
    assert np.array_equal(store.to_xarray().values, truth)


@pytest.mark.parametrize("workers", [1, 4])
def test_extreme_values_in_one_of_many_cells_still_roundtrip(tmp_path, workers):
    truth = np.full((2, 1, 8, 8), 1000, dtype=np.uint16)
    truth[1, 0, 5, 6] = 50000  # cell (1, 1) of a 2 x 2 grid of 4 px cells
    truth[1, 0, 0, 0] = 0
    store = _encode(tmp_path, truth, workers=workers)
    assert np.array_equal(store.to_xarray().values, truth)
