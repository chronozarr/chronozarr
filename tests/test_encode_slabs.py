"""Encoding in time slabs: the store is byte-identical whatever the slab length."""

from __future__ import annotations

import tracemalloc
from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import zarr

import chronozarr
from chronozarr import _writer, schema
from tests.synthetic import CRS, TRANSFORM, make_da, make_times, make_truth

pytestmark = pytest.mark.unit

CS = 8
BANDS = 2
STEP_BYTES = BANDS * CS * CS * 2  # one timestep of one uint16 cell
ONE_SLAB = 10**9


def _tree(path: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(path)): p.read_bytes() for p in sorted(path.rglob("*")) if p.is_file()
    }


def _slab_steps(monkeypatch, steps: int) -> None:
    monkeypatch.setattr(_writer, "SLAB_BYTES", steps * STEP_BYTES)


def _encode(monkeypatch, tmp_path: Path, name: str, steps: int, data, **kwargs: Any) -> Path:
    _slab_steps(monkeypatch, steps)
    out = tmp_path / name
    chronozarr.encode(data, out, chunk_size=CS, **kwargs)
    return out


def _stream_kwargs(n_time: int) -> dict[str, Any]:
    return {
        "times": make_times(n_time),
        "bands": [f"b{i}" for i in range(BANDS)],
        "crs": CRS,
        "transform": TRANSFORM,
    }


# --- slab boundaries --------------------------------------------------------------------------


@pytest.mark.parametrize("n_time", [1, 2, 5, 6, 7, 13, 25, 41, 60])
@pytest.mark.parametrize("steps", [1, 2, 4, 5, 6, 9, 30])
def test_slabs_tile_the_series_and_keep_comparison_groups_whole(n_time, steps):
    reference = schema.comparison_schedule(n_time)
    bounds = _writer._slab_bounds(n_time, steps, whole_groups=True)
    assert bounds[0][0] == 0
    assert bounds[-1][1] == n_time
    assert all(a[1] == b[0] for a, b in pairwise(bounds))
    for t0, t1 in bounds:
        assert t1 > t0
        for t, comparison in reference.items():
            if t0 <= t < t1:
                assert t0 <= comparison < t1, (t0, t1, t, comparison)


@pytest.mark.parametrize("steps", [1, 3, 7])
def test_slabs_without_volatility_are_exactly_steps_long(steps):
    assert _writer._slab_bounds(10, steps, whole_groups=False) == [
        (t, min(t + steps, 10)) for t in range(0, 10, steps)
    ]


def test_slab_steps_follow_the_byte_budget(monkeypatch):
    monkeypatch.setattr(_writer, "SLAB_BYTES", 64 * 2**20)
    assert _writer._slab_steps(4, 2, 512) == 32
    assert _writer._slab_steps(200, 4, 512) == 1  # never below one timestep


# --- byte identity ----------------------------------------------------------------------------


@pytest.mark.parametrize("n_time", [1, 5, 7, 25, 41])
@pytest.mark.parametrize("steps", [1, 4, 5, 9])
def test_slabbed_store_is_byte_identical_with_volatility_mask_and_coverage(
    monkeypatch, tmp_path, n_time, steps
):
    truth = make_truth(n_time, BANDS, 29, 21)
    mask = (truth[:, 0] > 0).astype(np.uint8)
    coverage = (mask * 3).astype(np.uint8)
    da = make_da(truth, ["b0", "b1"])
    kwargs: dict[str, Any] = {"volatility": True, "mask": mask, "coverage": coverage}
    whole = _encode(monkeypatch, tmp_path, "whole", ONE_SLAB, da, **kwargs)
    slabbed = _encode(monkeypatch, tmp_path, "slabbed", steps, da, **kwargs)
    assert _tree(slabbed) == _tree(whole)
    assert chronozarr.validate(slabbed) == []


@pytest.mark.parametrize("dtype", [np.float32, np.int16])
def test_volatility_is_identical_for_non_integer_and_signed_data(monkeypatch, tmp_path, dtype):
    rng = np.random.default_rng(3)
    truth = (rng.normal(0, 900, (25, BANDS, 29, 21))).astype(dtype)
    da = make_da(truth, ["b0", "b1"])
    whole = _encode(monkeypatch, tmp_path, "whole", ONE_SLAB, da, volatility=True)
    slabbed = _encode(monkeypatch, tmp_path, "slabbed", 5, da, volatility=True)
    assert _tree(slabbed) == _tree(whole)
    volatility = np.asarray(zarr.open_array(str(slabbed / schema.VOLATILITY_PATH), mode="r")[:])
    assert volatility.max() > 0


def test_slabbed_store_without_volatility_is_byte_identical(monkeypatch, tmp_path):
    da = make_da(make_truth(13, BANDS, 29, 21), ["b0", "b1"])
    whole = _encode(monkeypatch, tmp_path, "whole", ONE_SLAB, da)
    slabbed = _encode(monkeypatch, tmp_path, "slabbed", 4, da)
    assert _tree(slabbed) == _tree(whole)


@pytest.mark.parametrize("volatility", [True, False])
def test_iterable_input_is_byte_identical_in_slabs(monkeypatch, tmp_path, volatility):
    truth = make_truth(19, BANDS, 29, 21)
    mask = (truth[:, 0] > 0).astype(np.uint8)
    coverage = (mask * 2).astype(np.uint8)
    reference = _encode(
        monkeypatch,
        tmp_path,
        "array",
        ONE_SLAB,
        make_da(truth, ["b0", "b1"]),
        volatility=volatility,
        mask=mask,
        coverage=coverage,
    )
    slabbed = _encode(
        monkeypatch,
        tmp_path,
        "iter",
        5,
        iter(truth),
        volatility=volatility,
        mask=iter(mask),
        coverage=iter(coverage),
        **_stream_kwargs(19),
    )
    assert _tree(slabbed) == _tree(reference)


def test_slabbed_store_reads_back_the_input(monkeypatch, tmp_path):
    truth = make_truth(11, BANDS, 29, 21)
    out = _encode(monkeypatch, tmp_path, "s", 3, make_da(truth, ["b0", "b1"]))
    assert np.array_equal(chronozarr.open_store(out).to_xarray().values, truth)


# --- slabs are what bounds memory -------------------------------------------------------------


def _count_walks(monkeypatch) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    walk = _writer._Pyramid.walk

    def spy(self, submit, t0, t1):
        spans.append((t0, t1))
        return walk(self, submit, t0, t1)

    monkeypatch.setattr(_writer._Pyramid, "walk", spy)
    return spans


def test_an_unsharded_store_is_encoded_in_several_slabs(monkeypatch, tmp_path):
    spans = _count_walks(monkeypatch)
    da = make_da(make_truth(13, BANDS, 29, 21), ["b0", "b1"])
    _encode(monkeypatch, tmp_path, "s", 4, da)
    assert spans == [(0, 4), (4, 8), (8, 12), (12, 13)]


def test_a_sharded_store_is_one_slab_and_unchanged(monkeypatch, tmp_path):
    spans = _count_walks(monkeypatch)
    da = make_da(make_truth(13, BANDS, 29, 21), ["b0", "b1"])
    kwargs: dict[str, Any] = {"shard": True, "shard_time": 4, "volatility": True}
    whole = _encode(monkeypatch, tmp_path, "whole", ONE_SLAB, da, **kwargs)
    tiny = _encode(monkeypatch, tmp_path, "tiny", 1, da, **kwargs)
    assert spans == [(0, 13), (0, 13)]
    assert _tree(tiny) == _tree(whole)
    assert chronozarr.validate(tiny) == []


def test_peak_python_memory_does_not_grow_with_the_series(monkeypatch, tmp_path):
    """With slabs, the arrays traced during encoding stay the same size from 12 to 96
    timesteps; in one slab they grow with the series. Cells of 128 x 128 make a whole-series
    block 6 MB, well above allocator noise."""
    cs = 128
    step = BANDS * cs * cs * 2

    def peak(n_time: int, steps: int) -> int:
        da = make_da(make_truth(n_time, BANDS, 2 * cs, 2 * cs), ["b0", "b1"])
        monkeypatch.setattr(_writer, "SLAB_BYTES", steps * step)
        tracemalloc.start()
        chronozarr.encode(da, tmp_path / f"t{n_time}-{steps}", chunk_size=cs, volatility=True)
        _, traced = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        return traced

    peak(6, 4)  # warm up lazy imports and codec state outside the measurement
    short, long = peak(12, 6), peak(96, 6)
    one_slab = peak(96, 10**6)
    assert long < 1.5 * short, (short, long)
    assert one_slab > 3 * long, (long, one_slab)  # the measurement can see the difference
