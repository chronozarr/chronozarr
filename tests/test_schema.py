"""Attribute schema parsing and store validation."""

from __future__ import annotations

import json
import shutil
import sys

import numpy as np
import pytest
import zarr
from zarr.core.metadata import ArrayV3Metadata
from zarr.storage import MemoryStore

import chronozarr
from chronozarr import schema
from tests.crs_samples import LCC_WKT2
from tests.narrow import array_at, group_at
from tests.synthetic import build_store, make_truth

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module", params=[True, False], ids=["sharded", "unsharded"])
def good_store(request, tmp_path_factory):
    path = tmp_path_factory.mktemp("schema") / "store"
    build_store(path, make_truth(3, 2, 700, 600), shard=request.param)
    return path


@pytest.fixture
def store_copy(good_store, tmp_path):
    copy = tmp_path / "copy"
    shutil.copytree(good_store, copy)
    return copy


def _edit_root(path, edit):
    root = zarr.open_group(str(path), mode="r+", zarr_format=3)
    for key in ("chronozarr", "multiscales"):
        value = root.attrs[key]
        edit(key, value)
        root.attrs[key] = value


def test_encoded_store_conforms(good_store):
    assert chronozarr.validate(good_store) == []


@pytest.mark.parametrize(
    ("crs", "wrong_version", "expected_version"),
    [("EPSG:32631", "0.4.0", "0.3.0"), (LCC_WKT2, "0.3.0", "0.4.0")],
    ids=["epsg", "no-epsg"],
)
def test_writer_version_rule_fails_validation_but_does_not_prevent_decoding(
    tmp_path, crs, wrong_version, expected_version
):
    path = tmp_path / "version"
    truth = make_truth(2, 2, 8, 8)
    chronozarr.encode(
        truth,
        path,
        times=np.array(["2024-01-01", "2024-02-01"], dtype="datetime64[ms]"),
        crs=crs,
        transform=[10, 0, 0, 0, -10, 80],
        bands=["red", "nir"],
        chunk_size=4,
    )
    _edit_root(
        path,
        lambda key, block: (
            block.update(spec_version=wrong_version) if key == "chronozarr" else None
        ),
    )
    np.testing.assert_array_equal(chronozarr.open_store(path).read(0), truth[0])
    assert any(
        "spec_version" in message and f"MUST write '{expected_version}'" in message
        for message in chronozarr.validate(path)
    )


def test_every_array_declares_dimension_names(good_store):
    root = zarr.open_group(str(good_store), mode="r", zarr_format=3)
    expected = {
        "volatility": ("row", "col"),
        "0/data": ("time", "band", "y", "x"),
        "0/time": ("time",),
        "0/band": ("band",),
        "0/x": ("x",),
        "0/y": ("y",),
        "1/data": ("time", "band", "y", "x"),
    }
    for path, names in expected.items():
        metadata = array_at(root, path).metadata
        assert isinstance(metadata, ArrayV3Metadata), path
        assert metadata.dimension_names == names, path


def test_storage_layout(good_store):
    root = zarr.open_group(str(good_store), mode="r", zarr_format=3)
    data = array_at(root, "0/data")
    assert data.dtype == np.uint16
    assert data.fill_value == 0
    assert data.chunks == (1, 2, 512, 512)
    on_disk = json.loads((good_store / "0" / "data" / "zarr.json").read_text())
    bytes_codec = {"name": "bytes", "configuration": {"endian": "little"}}
    zstd_codec = {"name": "zstd", "configuration": {"level": 5, "checksum": False}}
    if data.shards is None:
        assert on_disk["codecs"] == [bytes_codec, zstd_codec]
        assert on_disk["chunk_grid"]["configuration"]["chunk_shape"] == [1, 2, 512, 512]
    else:
        assert data.shards == (3, 2, 512, 512)
        assert on_disk["chunk_grid"]["configuration"]["chunk_shape"] == [3, 2, 512, 512]
        assert on_disk["codecs"] == [
            {
                "name": "sharding_indexed",
                "configuration": {
                    "chunk_shape": [1, 2, 512, 512],
                    "codecs": [bytes_codec, zstd_codec],
                    "index_codecs": [bytes_codec, {"name": "crc32c"}],
                    "index_location": "end",
                },
            }
        ]
    assert on_disk["chunk_key_encoding"] == {
        "name": "default",
        "configuration": {"separator": "/"},
    }
    assert list(np.asarray(array_at(root, "0/band")[:])) == ["B04", "B08"]


def test_consolidated_metadata_is_written_and_readable(good_store):
    root_json = json.loads((good_store / "zarr.json").read_text())
    paths = set(root_json["consolidated_metadata"]["metadata"])
    assert {"0/data", "1/data", "volatility", "0/time", "1/y"} <= paths
    assert chronozarr.open_store(good_store).levels[1].shape == (3, 2, 350, 300)


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda b: b.update(spec_version="8.0.0"), "chronozarr convert"),
        (lambda b: b.pop("times"), "missing required key 'times'"),
        (lambda b: b.update(times=["2024-02", "2024-01"]), "strictly increasing"),
        (lambda b: b.update(times=["not-a-date"]), "not an ISO-8601"),
        (lambda b: b.update(bands=[{"name": "x"}, {"name": "x"}]), "must be unique"),
        (lambda b: b.update(nodata="zero"), "expected a finite number or null"),
        (lambda b: b.update(crs=""), "non-empty string"),
        (lambda b: b.update(variable=""), "expected a non-empty array name"),
        (lambda b: b.update(temporal={"encoding": "none"}), "outside v0.3"),
    ],
)
def test_bad_chronozarr_block_is_rejected_by_reader_and_validator(store_copy, edit, message):
    _edit_root(store_copy, lambda key, value: edit(value) if key == "chronozarr" else None)
    with pytest.raises(schema.SchemaError, match=message):
        chronozarr.open_store(store_copy)
    problems = chronozarr.validate(store_copy)
    assert len(problems) == 1
    assert message in problems[0]


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda ms: ms.clear(), "missing required key 'layout'"),
        (lambda ms: ms["layout"].reverse(), "consecutive group paths"),
        (lambda ms: ms["layout"][1].update(derived_from="9"), "previous level"),
    ],
)
def test_bad_multiscales_is_rejected(store_copy, edit, message):
    _edit_root(store_copy, lambda key, value: edit(value) if key == "multiscales" else None)
    with pytest.raises(schema.SchemaError, match=message):
        chronozarr.open_store(store_copy)
    assert any(message in p for p in chronozarr.validate(store_copy))


def test_writer_never_emits_pixels_per_tile(good_store):
    """zarr-layer reads the key as a global Web Mercator pyramid marker (spec 3.4)."""
    root_json = (good_store / "zarr.json").read_text()
    assert (
        "pixels_per_tile" not in root_json
    )  # not in the attributes, not in the consolidated copy
    assert chronozarr.open_store(good_store).levels[0].cell_shape == (512, 512)


def test_cell_size_is_the_chunk_size_of_the_data_array():
    memory = MemoryStore()
    plain = zarr.create_array(
        memory, name="a", shape=(3, 2, 20, 20), chunks=(1, 2, 8, 8), dtype="u2"
    )
    sharded = zarr.create_array(
        memory,
        name="b",
        shape=(3, 2, 20, 20),
        chunks=(1, 2, 4, 4),
        shards=(3, 2, 8, 8),
        dtype="u2",
    )
    assert schema.cell_size(plain, "level 0/data") == 8
    assert schema.cell_size(sharded, "level 0/data") == 4


@pytest.mark.parametrize(
    ("shape", "chunks", "message"),
    [
        ((3, 2, 20, 20), (1, 2, 8, 4), r"chunks must be \(1, 2, cs, cs\), got \(1, 2, 8, 4\)"),
        ((3, 2, 20, 20), (1, 1, 8, 8), r"chunks must be \(1, 2, cs, cs\), got \(1, 1, 8, 8\)"),
        ((3, 2, 20, 20), (2, 2, 8, 8), r"chunks must be"),
        ((3, 20, 20), (1, 8, 8), "expected 4 dimensions"),
    ],
)
def test_cell_size_rejects_a_layout_that_is_not_one_cell_per_chunk(shape, chunks, message):
    array = zarr.create_array(MemoryStore(), name="a", shape=shape, chunks=chunks, dtype="u2")
    with pytest.raises(schema.SchemaError, match=message):
        schema.cell_size(array, "level 0/data")


def test_validator_reports_levels_with_a_different_cell_size(tmp_path):
    path = tmp_path / "store"
    build_store(path, make_truth(3, 2, 70, 60), shard=False, chunk_size=16)
    assert chronozarr.validate(path) == []
    level1 = path / "1" / "data" / "zarr.json"
    document = json.loads(level1.read_text())
    document["chunk_grid"]["configuration"]["chunk_shape"] = [1, 2, 8, 8]
    level1.write_text(json.dumps(document))
    problems = chronozarr.validate(path)
    assert any("1/data: chunks must be (1, 2, 16, 16), got (1, 2, 8, 8)" in p for p in problems)


def test_validator_reports_structural_problems(store_copy):
    root = zarr.open_group(str(store_copy), mode="r+", zarr_format=3, use_consolidated=False)
    array_at(root, "1/data").attrs["spatial:shape"] = [1, 1]
    group_at(root, "1").attrs["transform"] = [30.0, 0.0, 746090.0, 0.0, -30.0, 2540440.0]
    problems = chronozarr.validate(store_copy)
    assert any("not the level-0 transform scaled by 2^1" in p for p in problems)
    assert any("spatial:shape differs from geometry" in p for p in problems)
    assert any("1/data: consolidated metadata is stale" in p for p in problems)


def test_validator_flags_missing_arrays_and_wrong_coordinates(store_copy):
    shutil.rmtree(store_copy / "0" / "x")
    root = zarr.open_group(str(store_copy), mode="r+", zarr_format=3, use_consolidated=False)
    array_at(root, "1/time")[:] = np.array([0, 1, 2], dtype="int64")
    report = schema.validate_report(store_copy)
    assert any(
        "0/x: listed in consolidated metadata but missing on disk" in p for p in report.problems
    )
    assert any("level 1/time: values differ" in p for p in report.problems)
    assert not any("array 'x' is missing" in p for p in report.problems)
    assert any("level 0: coordinate arrays absent: x" in n for n in report.notes)


def test_validator_reports_non_store(tmp_path):
    (tmp_path / "empty").mkdir()
    (problem,) = chronozarr.validate(tmp_path / "empty")
    assert "no Zarr v3 group found" in problem


def test_validator_flags_wrong_volatility(store_copy):
    shutil.rmtree(store_copy / "volatility")
    problems = chronozarr.validate(store_copy)
    assert any("array 'volatility' is missing" in p for p in problems)


def test_variable_name_comes_from_attrs_not_a_hardcoded_default(store_copy):
    _edit_root(
        store_copy,
        lambda key, value: value.update(variable="reflectance") if key == "chronozarr" else None,
    )
    with pytest.raises(schema.SchemaError, match="level 0: array 'reflectance' is missing"):
        chronozarr.open_store(store_copy)
    problems = chronozarr.validate(store_copy)
    assert "level 0: array 'reflectance' is missing" in problems
    assert "level 1: array 'reflectance' is missing" in problems


def test_geometry_aliases_are_optional_and_must_match_when_present(store_copy):
    root = zarr.open_group(str(store_copy), mode="r+", zarr_format=3, use_consolidated=False)
    data = array_at(root, "0/data")
    for key in ("spatial:bbox", "spatial:shape", "crs", "transform", "_CRS"):
        del data.attrs[key]
    problems = chronozarr.validate(store_copy)
    assert not [p for p in problems if "attribute" in p]
    assert chronozarr.open_store(store_copy).levels[0].shape == (3, 2, 700, 600)

    data.attrs["spatial:bbox"] = [0.0, 0.0, 1.0, 1.0]
    assert any("spatial:bbox differs from geometry" in p for p in chronozarr.validate(store_copy))


@pytest.mark.parametrize(
    ("crs", "unit"),
    [("EPSG:4326", "degree"), ("EPSG:32631", "m"), ("EPSG:2278", "US survey foot")],
)
def test_axis_unit_is_the_unit_the_crs_names(crs, unit):
    pytest.importorskip("rasterio")
    assert schema.axis_unit(crs) == unit


def test_axis_unit_without_rasterio_does_not_guess_metres(monkeypatch):
    monkeypatch.setitem(sys.modules, "rasterio.crs", None)  # `from rasterio.crs import CRS` fails
    assert schema.axis_unit("EPSG:4326") == "CRS-unit"
