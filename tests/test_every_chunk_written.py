"""Writers store every chunk, all-fill ones included, so no reader asks a host for an absent key.

Chromium logs each 404 as a console error, whatever the host, so a store with omitted chunks
looked broken in the viewer (newcomer sessions 201 and 204). Readers still read an absent key as
fill: stores written before keep their absent chunks, and an append leaves them as they are.
"""

from __future__ import annotations

import importlib
import itertools
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import xarray as xr
import zarr

import chronozarr
from chronozarr import schema
from chronozarr.append import append
from chronozarr.convert import convert
from chronozarr.decode import open_store
from tests.synthetic import BANDS, CRS, TRANSFORM, make_da, make_times, make_truth

pytestmark = pytest.mark.unit

CHUNK = 16
HEIGHT, WIDTH = 40, 50  # cells 3 x 4, 2 x 2, 1 x 1
N_TIME = 6
EMPTY_ROWS = slice(32, HEIGHT)  # the bottom cell row of level 0 holds no valid pixel
EMPTY_ENTRY = 2**64 - 1  # offset and length of an inner chunk that a shard does not hold
ZARR_DEFAULT = {"write_empty_chunks": False}  # what chronozarr wrote with before 0.5.0


def _truth() -> np.ndarray:
    truth = make_truth(N_TIME, 2, HEIGHT, WIDTH)
    truth[:, :, EMPTY_ROWS, :] = 0
    return truth


def _mask(truth: np.ndarray) -> xr.DataArray:
    valid = (truth[:, 0] != 0).astype(np.uint8)
    return xr.DataArray(valid, dims=("time", "y", "x"))


def _grid(shape: list[int], chunks: list[int]) -> itertools.product:
    return itertools.product(
        *(range(math.ceil(s / c)) for s, c in zip(shape, chunks, strict=True))
    )


def _unwritten(path: Path) -> list[str]:
    """Every chunk that the metadata of an array in the store defines and the store does not
    hold: a chunk or shard with no object, or an inner chunk that a shard index lists as empty.
    Inner chunks outside the array (the rest of a partial time shard) do not count."""
    found = []
    for node in sorted(path.rglob("zarr.json")):
        meta = json.loads(node.read_text())
        if meta["node_type"] != "array":
            continue
        name = node.parent.relative_to(path).as_posix()
        shape = meta["shape"]
        outer = meta["chunk_grid"]["configuration"]["chunk_shape"]
        assert meta["chunk_key_encoding"] == {
            "name": "default",
            "configuration": {"separator": "/"},
        }
        sharding = next((c for c in meta["codecs"] if c["name"] == "sharding_indexed"), None)
        for index in _grid(shape, outer):
            key = "/".join(["c", *map(str, index)])
            obj = node.parent / key
            if not obj.is_file():
                found.append(f"{name}/{key}")
                continue
            if sharding is None:
                continue
            config = sharding["configuration"]
            inner = config["chunk_shape"]
            per_shard = [o // i for o, i in zip(outer, inner, strict=True)]
            n = math.prod(per_shard)
            crc = any(c["name"] == "crc32c" for c in config["index_codecs"])
            length = 16 * n + (4 if crc else 0)
            body = obj.read_bytes()
            raw = body[-length:] if config.get("index_location", "end") == "end" else body[:length]
            entries = np.frombuffer(raw[: 16 * n], dtype="<u8").reshape(n, 2)
            for j, local in enumerate(itertools.product(*map(range, per_shard))):
                start = [
                    (i * p + loc) * c
                    for i, p, loc, c in zip(index, per_shard, local, inner, strict=True)
                ]
                inside = all(s < size for s, size in zip(start, shape, strict=True))
                if inside and (entries[j] == EMPTY_ENTRY).all():
                    found.append(f"{name}/{key} inner chunk {local}")
    return found


def _encode_as_before(monkeypatch, *args, **kwargs) -> None:
    """Encode as chronozarr did before 0.5.0, with zarr's default: all-fill chunks not written."""
    with monkeypatch.context() as patch:
        for module in ("chronozarr.encode", "chronozarr._writer"):
            patch.setattr(importlib.import_module(module), "WRITE_EVERY_CHUNK", ZARR_DEFAULT)
        chronozarr.encode(*args, **kwargs)


def _epoch_da(truth: np.ndarray) -> xr.DataArray:
    """One timestep at 1970-01-01 (time coordinate 0 ms, its fill value), so the time array holds
    only fill; with one timestep the volatility array is all 0, its fill value too."""
    da = make_da(truth[:1], BANDS)
    return da.assign_coords(time=pd.to_datetime(["1970-01-01"]).values)


@pytest.mark.parametrize(
    "options",
    [
        {"nodata": 0},
        {"nodata": 0, "volatility": True},
        {"planes": True, "volatility": True},
        {"nodata": 0, "shard": True, "shard_time": 4, "volatility": True},
        {"planes": True, "shard": True, "shard_time": 4},
    ],
    ids=["nodata", "volatility", "mask-coverage", "sharded", "sharded-planes"],
)
def test_encode_writes_every_chunk(tmp_path, options):
    truth = _truth()
    out = tmp_path / "store"
    if options.pop("planes", False):
        options.update(mask=_mask(truth), coverage=_mask(truth))
    chronozarr.encode(make_da(truth, BANDS), out, chunk_size=CHUNK, **options)

    assert _unwritten(out) == []


def test_the_audit_finds_what_an_older_writer_left_out(monkeypatch, tmp_path):
    out = tmp_path / "older"
    _encode_as_before(monkeypatch, make_da(_truth()[:1], BANDS), out, chunk_size=CHUNK, nodata=0)

    # The bottom cell row of level 0 and the one of level 1 that is its block mean.
    level0 = [f"0/data/c/0/0/2/{c}" for c in range(4)]
    assert _unwritten(out) == [*level0, "1/data/c/0/0/1/0", "1/data/c/0/0/1/1"]


def test_arrays_that_hold_only_their_fill_value_are_written(tmp_path):
    out = tmp_path / "epoch"
    chronozarr.encode(_epoch_da(_truth()), out, chunk_size=CHUNK, nodata=0, volatility=True)

    assert (out / "0/time/c/0").is_file()
    assert (out / "volatility/c/0/0").is_file()
    assert _unwritten(out) == []


def test_written_fill_chunks_read_back_as_the_values_given(tmp_path):
    truth = _truth()
    out = tmp_path / "values"
    chronozarr.encode(make_da(truth, BANDS), out, chunk_size=CHUNK, nodata=0)

    store = open_store(out)
    for t in range(N_TIME):
        np.testing.assert_array_equal(store.read(t), truth[t])
    assert schema.validate(out) == []


def test_convert_writes_every_chunk(tmp_path):
    truth = _truth()
    source, out = tmp_path / "source.zarr", tmp_path / "converted"
    y, x = schema.pixel_centers(TRANSFORM, HEIGHT, WIDTH)
    xr.DataArray(
        truth,
        dims=schema.DIMENSIONS,
        coords={"time": make_times(N_TIME), "band": BANDS, "y": y, "x": x},
        name="reflectance",
        attrs={"crs": CRS},
    ).to_dataset().to_zarr(source, zarr_format=2, consolidated=False)

    convert(source, out, chunk_size=CHUNK, nodata=0)

    assert _unwritten(out) == []


@pytest.mark.parametrize(
    "options",
    [{"nodata": 0, "volatility": True}, {"nodata": 0, "shard": True, "shard_time": 4}],
    ids=["unsharded", "sharded"],
)
def test_append_writes_every_chunk_of_the_new_timesteps(tmp_path, options):
    truth = _truth()
    out = tmp_path / "grown"
    chronozarr.encode(make_da(truth[:3], BANDS), out, chunk_size=CHUNK, **options)

    append(out, make_da(truth, BANDS).isel(time=slice(3, N_TIME)))

    assert _unwritten(out) == []


def test_append_keeps_a_volatility_array_of_zeros(tmp_path):
    # The same field every timestep: volatility stays 0, the array's fill value.
    still = np.repeat(_truth()[:1], 4, axis=0)
    out = tmp_path / "still"
    chronozarr.encode(make_da(still[:2], BANDS), out, chunk_size=CHUNK, nodata=0, volatility=True)

    append(out, make_da(still, BANDS).isel(time=slice(2, 4)))

    assert (out / "volatility/c/0/0").is_file()
    assert _unwritten(out) == []


def test_append_updates_a_volatility_that_falls_to_zero(tmp_path):
    # Frames A A A A B B compare 4 and 5 with frame 0, so volatility is not 0. With 12 frames,
    # 4 and 5 compare with frame 6, and every comparison is B with B or A with A: 0 everywhere.
    # The update must be written even though it holds only fill, or the old value stays.
    a = _truth()[0]
    b = np.where(a > 0, a + 500, 0).astype(a.dtype)
    frames = np.stack([a, a, a, a] + [b] * 8)
    out = tmp_path / "falls"
    chronozarr.encode(make_da(frames[:6], BANDS), out, chunk_size=CHUNK, nodata=0, volatility=True)
    assert np.asarray(zarr.open_array(out / "volatility")[:]).any()

    append(out, make_da(frames, BANDS).isel(time=slice(6, 12)))

    assert not np.asarray(zarr.open_array(out / "volatility")[:]).any()
    assert _unwritten(out) == []


def test_append_to_a_store_with_absent_chunks_leaves_them_absent(monkeypatch, tmp_path):
    truth = _truth()
    out = tmp_path / "older"
    _encode_as_before(monkeypatch, make_da(truth[:4], BANDS), out, chunk_size=CHUNK, nodata=0)
    omitted = _unwritten(out)
    assert omitted

    append(out, make_da(truth, BANDS).isel(time=slice(4, N_TIME)))

    assert _unwritten(out) == omitted, "an append writes the new timesteps only, all of them"
    store = open_store(out)
    for t in range(N_TIME):
        np.testing.assert_array_equal(store.read(t), truth[t])
    assert schema.validate(out) == []


def test_an_absent_shard_of_an_older_store_reads_as_fill(monkeypatch, tmp_path):
    truth = _truth()
    out = tmp_path / "older-sharded"
    da = make_da(truth, BANDS)
    _encode_as_before(monkeypatch, da, out, chunk_size=CHUNK, nodata=0, shard=True, shard_time=4)

    assert "0/data/c/0/0/2/0" in _unwritten(out)  # timesteps 0..3 of an all-nodata cell
    store = open_store(out)
    for t in range(N_TIME):
        assert not store.read_cell(t, 2, 0).any()
        np.testing.assert_array_equal(store.read(t), truth[t])
    assert schema.validate(out) == []
