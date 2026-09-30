"""`export_cog`: decoded true-value COGs readable by GDAL."""

from __future__ import annotations

import json
import shutil
import subprocess

import numpy as np
import pytest

import chronozarr
from chronozarr.export import _file_stem, export_cog, select_times
from tests.synthetic import BANDS, build_store, make_truth

pytestmark = pytest.mark.unit

TIMES = [
    "2024-01-01T00:00:00Z",
    "2024-02-01T00:00:00Z",
    "2024-03-01T00:00:00Z",
    "2024-03-20T00:00:00Z",
    "2025-01-15T00:00:00Z",
]


# --- select_times ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ([], [0, 1, 2, 3, 4]),
        (["all"], [0, 1, 2, 3, 4]),
        (["0,2"], [0, 2]),
        (["4", "1"], [1, 4]),
        (["-1"], [4]),
        (["1:3"], [1, 2]),
        (["::2"], [0, 2, 4]),
        (["2024-03"], [2, 3]),
        (["2024-03-20"], [3]),
        (["2024-02..2024-03"], [1, 2, 3]),
        (["2024-03..2025-01"], [2, 3, 4]),
        (["0, 2024-03"], [0, 2, 3]),
    ],
)
def test_select_times(spec, expected):
    assert select_times(spec, TIMES) == expected


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        (["7"], "out of range"),
        (["-6"], "out of range"),
        (["2030-01"], "no timestep matches"),
        (["2024"], "out of range"),  # a bare number is an index, not a year
        (["a:b"], "bad index slice"),
        (["2024..2025"], "bad time range"),
        (["banana"], "cannot parse"),
        (["5:9"], "no timesteps selected"),
    ],
)
def test_select_times_rejects_bad_tokens(spec, message):
    with pytest.raises(ValueError, match=message):
        select_times(spec, TIMES)


def test_file_stem_keeps_the_clock_only_when_needed():
    assert _file_stem("2024-03-01T00:00:00Z") == "2024-03-01"
    assert _file_stem("2024-03-01T00:00:00.000Z") == "2024-03-01"
    assert _file_stem("2024-03-01T12:30:00Z") == "2024-03-01T123000Z"


# --- export_cog -----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def store_and_truth(tmp_path_factory):
    truth = make_truth(5, 2, 40, 50)
    path = tmp_path_factory.mktemp("export") / "store"
    build_store(path, truth, shard=True, chunk_size=16, anchor_interval=2)
    return path, truth


def test_every_timestep_roundtrips_through_rasterio(store_and_truth, tmp_path):
    rasterio = pytest.importorskip("rasterio")
    path, truth = store_and_truth
    paths = export_cog(path, tmp_path / "cogs")
    assert [p.name for p in paths] == [
        "L0_2024-01-01.tif",
        "L0_2024-02-01.tif",
        "L0_2024-03-01.tif",
        "L0_2024-04-01.tif",
        "L0_2024-05-01.tif",
    ]
    store = chronozarr.open_store(path)
    for t, tif in enumerate(paths):
        with rasterio.open(tif) as src:
            assert np.array_equal(src.read(), truth[t])  # includes non-anchor timesteps
            assert src.crs.to_string() == store.attrs.crs
            assert tuple(src.transform)[:6] == store.levels[0].transform
            assert src.descriptions == tuple(BANDS)
            assert src.nodata == 0
            assert src.dtypes == ("uint16", "uint16")
            assert src.tags()["CHRONOZARR_TIME"] == store.attrs.times[t]
            assert src.tags()["CHRONOZARR_TIMESTEP"] == str(t)
            assert src.profile["tiled"]
            assert src.compression.name == "deflate"


def test_level_and_time_selection(store_and_truth, tmp_path):
    rasterio = pytest.importorskip("rasterio")
    path, _ = store_and_truth
    store = chronozarr.open_store(path)
    assert len(store.levels) > 1
    paths = export_cog(store, tmp_path, level=1, times=[3])
    assert [p.name for p in paths] == ["L1_2024-04-01.tif"]
    with rasterio.open(paths[0]) as src:
        assert np.array_equal(src.read(), store.read(3, 1))
        assert tuple(src.transform)[:6] == store.levels[1].transform
        assert src.tags()["CHRONOZARR_LEVEL"] == "1"


def test_existing_files_and_bad_level_are_errors(store_and_truth, tmp_path):
    pytest.importorskip("rasterio")
    path, _ = store_and_truth
    export_cog(path, tmp_path, times=[0])
    with pytest.raises(FileExistsError, match="already exist"):
        export_cog(path, tmp_path, times=[0])
    with pytest.raises(ValueError, match="level 9 out of range"):
        export_cog(path, tmp_path / "other", level=9)


@pytest.mark.skipif(shutil.which("gdalinfo") is None, reason="GDAL command line not installed")
def test_gdalinfo_reports_a_cloud_optimized_layout(store_and_truth, tmp_path):
    pytest.importorskip("rasterio")
    path, truth = store_and_truth
    (tif,) = export_cog(path, tmp_path, times=[2])
    out = subprocess.run(
        ["gdalinfo", "-json", "-stats", str(tif)], check=True, capture_output=True, text=True
    )
    info = json.loads(out.stdout)
    assert info["metadata"]["IMAGE_STRUCTURE"]["LAYOUT"] == "COG"
    assert info["size"] == [50, 40]
    assert [b["description"] for b in info["bands"]] == BANDS
    assert info["bands"][0]["noDataValue"] == 0
    assert info["bands"][0]["type"] == "UInt16"
    valid = truth[2, 0][truth[2, 0] != 0]
    assert info["bands"][0]["maximum"] == float(valid.max())
