"""Deltas outside the int16 range are rejected, never silently clipped."""

from __future__ import annotations

import numpy as np
import pytest
import zarr

import chronozarr
from tests.synthetic import make_da

pytestmark = pytest.mark.unit

# One 1x4 cell, two timesteps: anchor 0, delta 1 -> anchor 0. Columns:
#   0: overflow up      anchor 100,   value 60000 -> delta +59900
#   1: overflow down    anchor 50000, value 100   -> delta -49900
#   2: largest exact +  anchor 100,   value 32867 -> delta +32767
#   3: largest exact -  anchor 32868, value 100   -> delta -32768
ANCHOR = np.array([100, 50000, 100, 32868], dtype=np.uint16)
CURRENT = np.array([60000, 100, 32867, 100], dtype=np.uint16)


def _cube(anchor: np.ndarray, current: np.ndarray) -> np.ndarray:
    return np.stack([anchor, current])[:, None, None, :]  # (time, band, y, x)


@pytest.mark.parametrize("shard", [True, False], ids=["sharded", "unsharded"])
def test_overflowing_deltas_raise_and_leave_no_store(tmp_path, shard):
    with pytest.raises(
        ValueError, match="2 pixel\\(s\\) at timestep 1 differ from anchor 0"
    ) as exc:
        chronozarr.encode(
            make_da(_cube(ANCHOR, CURRENT)),
            tmp_path / "s",
            anchor_interval=2,
            chunk_size=4,
            shard=shard,
        )
    assert "anchor_interval=1" in str(exc.value)
    assert "level 0 cell (row 0, col 0)" in str(exc.value)
    assert not (tmp_path / "s").exists()


def test_failure_keeps_a_preexisting_empty_output_directory(tmp_path):
    (tmp_path / "s").mkdir()
    with pytest.raises(ValueError, match="int16 range"):
        chronozarr.encode(
            make_da(_cube(ANCHOR, CURRENT)), tmp_path / "s", anchor_interval=2, chunk_size=4
        )
    assert (tmp_path / "s").is_dir()
    assert not list((tmp_path / "s").iterdir())


@pytest.mark.parametrize("shard", [True, False], ids=["sharded", "unsharded"])
def test_boundary_deltas_roundtrip_exactly(tmp_path, shard):
    truth = _cube(ANCHOR[2:], CURRENT[2:])
    chronozarr.encode(make_da(truth), tmp_path / "s", anchor_interval=2, chunk_size=4, shard=shard)
    store = chronozarr.open_store(tmp_path / "s")
    assert store.read(1)[0, 0].tolist() == [32867, 100]
    assert np.array_equal(store.to_xarray().values, truth)


def test_stored_boundary_deltas_are_the_int16_residuals(tmp_path):
    chronozarr.encode(
        make_da(_cube(ANCHOR[2:], CURRENT[2:])),
        tmp_path / "s",
        anchor_interval=2,
        chunk_size=4,
        shard=False,
    )
    raw = zarr.open_array(str(tmp_path / "s" / "0" / "data"), mode="r")[1, 0, 0]
    assert raw.view(np.int16).tolist() == [32767, -32768]


def test_anchor_interval_one_stores_full_range_uint16_losslessly(tmp_path):
    truth = _cube(np.array([0, 65535, 1, 65534], dtype=np.uint16), CURRENT)
    chronozarr.encode(make_da(truth), tmp_path / "s", anchor_interval=1, chunk_size=4)
    assert np.array_equal(chronozarr.open_store(tmp_path / "s").to_xarray().values, truth)


@pytest.mark.parametrize("workers", [1, 4])
def test_overflow_in_one_of_many_cells_fails_the_whole_encode(tmp_path, workers):
    truth = np.full((2, 1, 8, 8), 1000, dtype=np.uint16)
    truth[1, 0, 5, 6] = 50000  # cell (1, 1) of a 2 x 2 grid of 4 px cells
    with pytest.raises(ValueError, match="cell \\(row 1, col 1\\): 1 pixel"):
        chronozarr.encode(
            make_da(truth), tmp_path / "s", anchor_interval=2, chunk_size=4, workers=workers
        )
    assert not (tmp_path / "s").exists()
