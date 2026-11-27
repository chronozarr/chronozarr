"""Empty time selections retain array structure with optional validity planes."""

import numpy as np
import pytest

import chronozarr
from tests.synthetic import make_da

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("physical", [False, True])
@pytest.mark.parametrize("shard", [False, True])
@pytest.mark.parametrize("has_mask", [False, True])
def test_empty_time_selection_keeps_coordinates_and_mask(tmp_path, physical, shard, has_mask):
    values = np.arange(12, dtype=np.int16).reshape(2, 1, 2, 3)
    mask = np.ones((2, 2, 3), dtype=np.uint8) if has_mask else None
    path = tmp_path / "store"
    chronozarr.encode(
        make_da(values, ["signal"]),
        path,
        mask=mask,
        shard=shard,
        chunk_size=16,
        n_lods=1,
    )
    store = chronozarr.open_store(path)

    result = store.to_xarray(times=[], physical=physical)

    assert result.shape == (0, 1, 2, 3)
    assert result.dims == ("time", "band", "y", "x")
    assert result.dtype == np.dtype("float32" if physical else "int16")
    assert result.time.dtype.kind == "M"
    assert result.band.values.tolist() == ["signal"]
    assert result.x.values.tolist() == [746095, 746105, 746115]
    assert result.y.values.tolist() == [2540435, 2540425]
    if has_mask:
        assert result.coords["mask"].shape == (0, 2, 3)
        assert result.coords["mask"].dtype == np.dtype("uint8")
    else:
        assert "mask" not in result.coords


def test_coverage_plane_accepts_empty_time_selection(tmp_path):
    values = np.ones((2, 1, 2, 3), dtype=np.uint16)
    path = tmp_path / "store"
    chronozarr.encode(
        make_da(values),
        path,
        coverage=np.ones((2, 2, 3), dtype=np.uint8),
        chunk_size=16,
        n_lods=1,
    )
    store = chronozarr.open_store(path)
    plane = store.levels[0].coverage
    assert plane is not None

    result = store._read_plane(plane, [], slice(None), slice(None))

    assert result.shape == (0, 2, 3)
    assert result.dtype == np.dtype("uint8")
