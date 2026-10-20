"""Writers store every chunk, all-fill ones included, so no reader asks a host for an absent key.

Chromium logs each 404 as a console error, whatever the host, so a store with omitted chunks
looked broken in the viewer (newcomer sessions 201 and 204). Readers still read an absent key as
fill: stores written before keep their absent chunks, and an append leaves them as they are.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import xarray as xr

import chronozarr
from chronozarr import schema
from chronozarr.append import append
from chronozarr.decode import open_store
from tests.synthetic import BANDS, make_da, make_truth

pytestmark = pytest.mark.unit

CHUNK = 16
HEIGHT, WIDTH = 40, 50  # cells 3 x 4, 2 x 2, 1 x 1
N_TIME = 6
EMPTY_ROWS = slice(32, HEIGHT)  # the bottom cell row of level 0 holds no valid pixel


def _truth() -> np.ndarray:
    truth = make_truth(N_TIME, 2, HEIGHT, WIDTH)
    truth[:, :, EMPTY_ROWS, :] = 0
    return truth


def _mask(truth: np.ndarray) -> xr.DataArray:
    valid = (truth[:, 0] != 0).astype(np.uint8)
    return xr.DataArray(valid, dims=("time", "y", "x"))


def _expected_keys(path: Path, n_time: int) -> set[str]:
    """Every chunk key of every data, mask and coverage array, from the store's own metadata."""
    store = open_store(path)
    meta = store.attrs
    keys = set()
    for k, level in enumerate(store.levels):
        rows, cols = level.grid
        for t in range(n_time):
            for r in range(rows):
                for c in range(cols):
                    keys.add(f"{k}/{meta.variable}/c/{t}/0/{r}/{c}")
                    for plane in (meta.mask_variable, meta.coverage_variable):
                        if plane is not None:
                            keys.add(f"{k}/{plane}/c/{t}/{r}/{c}")
    return keys


def _chunk_keys(path: Path) -> set[str]:
    """Keys of the chunk objects on disk of the data, mask and coverage arrays."""
    keys = (str(p.relative_to(path)) for p in path.rglob("*") if p.is_file())
    return {
        key
        for key in keys
        if key.split("/")[1:3] in (["data", "c"], ["mask", "c"], ["coverage", "c"])
    }


def test_encode_writes_the_chunks_of_a_cell_with_no_valid_pixel(tmp_path):
    out = tmp_path / "nodata"
    chronozarr.encode(make_da(_truth(), BANDS), out, chunk_size=CHUNK, nodata=0)

    assert "0/data/c/0/0/2/0" in _chunk_keys(out)  # all nodata: absent before
    assert _chunk_keys(out) == _expected_keys(out, N_TIME)


def test_encode_writes_every_mask_and_coverage_chunk(tmp_path):
    truth = _truth()
    out = tmp_path / "masked"
    chronozarr.encode(
        make_da(truth, BANDS), out, chunk_size=CHUNK, mask=_mask(truth), coverage=_mask(truth)
    )

    assert {"0/mask/c/0/2/0", "0/coverage/c/0/2/0"} <= _chunk_keys(out)
    assert _chunk_keys(out) == _expected_keys(out, N_TIME)


def test_a_sharded_encode_writes_a_shard_that_holds_only_fill(tmp_path):
    out = tmp_path / "sharded"
    chronozarr.encode(make_da(_truth(), BANDS), out, chunk_size=CHUNK, nodata=0, shard=True)

    assert (out / "0/data/c/0/0/2/0").is_file()


def test_written_fill_chunks_read_back_as_the_values_given(tmp_path):
    truth = _truth()
    out = tmp_path / "values"
    chronozarr.encode(make_da(truth, BANDS), out, chunk_size=CHUNK, nodata=0)

    store = open_store(out)
    for t in range(N_TIME):
        np.testing.assert_array_equal(store.read(t), truth[t])
    assert schema.validate(out) == []


def test_append_writes_every_chunk_of_the_new_timesteps(tmp_path):
    truth = _truth()
    out = tmp_path / "grown"
    chronozarr.encode(make_da(truth[:4], BANDS), out, chunk_size=CHUNK, nodata=0)

    append(out, make_da(truth, BANDS).isel(time=slice(4, N_TIME)))

    assert _chunk_keys(out) == _expected_keys(out, N_TIME)


def test_append_to_a_store_with_absent_chunks_leaves_them_absent(tmp_path):
    # A store written before every chunk was stored: its all-fill chunks have no object.
    truth = _truth()
    out = tmp_path / "older"
    chronozarr.encode(make_da(truth[:4], BANDS), out, chunk_size=CHUNK, nodata=0)
    omitted = {f"0/data/c/{t}/0/2/{c}" for t in range(4) for c in range(4)}
    for key in omitted:
        (out / key).unlink(missing_ok=True)

    append(out, make_da(truth, BANDS).isel(time=slice(4, N_TIME)))

    keys = _chunk_keys(out)
    assert not omitted & keys, "an append does not rewrite the chunks of existing timesteps"
    assert {f"0/data/c/{t}/0/2/{c}" for t in (4, 5) for c in range(4)} <= keys
    store = open_store(out)
    for t in range(N_TIME):
        np.testing.assert_array_equal(store.read(t), truth[t])
    assert schema.validate(out) == []
