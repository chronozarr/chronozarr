"""Attribute schema parsing and store validation."""

from __future__ import annotations

import json
import shutil

import numpy as np
import pytest
import zarr

import chronozarr
from chronozarr import schema
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


def test_written_attrs_have_the_specified_shape(good_store):
    root = zarr.open_group(str(good_store), mode="r", zarr_format=3)
    attrs = root.attrs.asdict()
    block = attrs["chronozarr"]
    assert block["spec_version"] == "0.2.0"
    assert block["variable"] == "data"
    assert block["times"] == [
        "2024-01-01T00:00:00Z",
        "2024-02-01T00:00:00Z",
        "2024-03-01T00:00:00Z",
    ]
    assert block["bands"] == [{"name": "B04"}, {"name": "B08"}]
    assert block["band_names"] == ["B04", "B08"]
    assert block["nodata"] == 0
    assert block["crs"] == "EPSG:32631"
    assert block["temporal"] == {
        "encoding": "star-delta",
        "anchor_interval": 2,
        "anchor_indices": [0, 2],
        "delta_reference": {"1": 0},
    }
    assert block["volatility_path"] == "volatility"
    assert not {"mask_variable", "coverage_variable", "provenance"} & set(block)
    assert [lv["path"] for lv in block["levels"]] == ["0", "1"]
    assert block["levels"][1] == {
        "path": "1",
        "resolution": 20.0,
        "transform": [20.0, 0.0, 746090.0, 0.0, -20.0, 2540440.0],
        "shape": [3, 2, 350, 300],
        "grid": [1, 1],
    }
    (multiscale,) = attrs["multiscales"]
    assert multiscale["datasets"] == [
        {"path": "0", "pixels_per_tile": 512, "crs": "EPSG:32631"},
        {"path": "1", "pixels_per_tile": 512, "crs": "EPSG:32631"},
    ]
    assert multiscale["type"] == "reduce"
    assert multiscale["metadata"] == {
        "method": "block_mean",
        "version": "chronozarr 0.2.0",
        "args": [],
    }
    assert "method" not in multiscale

    data_attrs = root["0"]["data"].attrs.asdict()
    assert data_attrs["_ARRAY_DIMENSIONS"] == ["time", "band", "y", "x"]
    assert data_attrs["proj:code"] == "EPSG:32631"
    assert data_attrs["spatial:dimensions"] == ["y", "x"]
    assert data_attrs["spatial:shape"] == [700, 600]
    assert data_attrs["spatial:transform"] == [10.0, 0.0, 746090.0, 0.0, -10.0, 2540440.0]
    assert data_attrs["spatial:bbox"] == [746090.0, 2533440.0, 752090.0, 2540440.0]
    coarse = root["1"]["data"].attrs.asdict()
    assert coarse["spatial:shape"] == [350, 300]
    assert coarse["spatial:transform"] == [20.0, 0.0, 746090.0, 0.0, -20.0, 2540440.0]
    assert coarse["spatial:bbox"] == [746090.0, 2533440.0, 752090.0, 2540440.0]

    level_attrs = root["1"].attrs.asdict()
    assert level_attrs == {
        "crs": "EPSG:32631",
        "transform": [20.0, 0.0, 746090.0, 0.0, -20.0, 2540440.0],
        "resolution": 20.0,
    }


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
        assert root[path].metadata.dimension_names == names, path


def test_storage_layout(good_store):
    root = zarr.open_group(str(good_store), mode="r", zarr_format=3)
    data = root["0"]["data"]
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
    assert list(root["0"]["band"][:]) == ["B04", "B08"]


def test_consolidated_metadata_is_written_and_readable(good_store):
    root_json = json.loads((good_store / "zarr.json").read_text())
    paths = set(root_json["consolidated_metadata"]["metadata"])
    assert {"0/data", "1/data", "volatility", "0/time", "1/y"} <= paths
    assert chronozarr.open_store(good_store).levels[1].shape == (3, 2, 350, 300)


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda b: b.update(spec_version="0.3.0"), "spec_version: unsupported version '0.3.0'"),
        (lambda b: b.pop("times"), "missing required key 'times'"),
        (
            lambda b: b.update(times=["2024-02-01", "2024-01-01", "2024-03-01"]),
            "strictly increasing",
        ),
        (lambda b: b.update(times=["not-a-date", "2024-02", "2024-03"]), "not an ISO-8601"),
        (lambda b: b.update(bands=["B04", "B04"]), "must be unique"),
        (lambda b: b.update(nodata="zero"), "expected a finite number or null"),
        (lambda b: b.update(crs=""), "non-empty string"),
        (lambda b: b.update(variable=""), "expected a non-empty array name"),
        (lambda b: b["temporal"].update(encoding="chain-delta"), "expected one of"),
        (lambda b: b["temporal"].update(anchor_interval=0), "expected int >= 1"),
        (lambda b: b["temporal"].update(anchor_indices=[0, 1]), "anchor_indices"),
        (
            lambda b: b["temporal"].update(delta_reference={"1": 1}),
            "timestep 1 references 1, which is not an anchor",
        ),
        (
            lambda b: b["temporal"].update(delta_reference={}),
            "are neither anchors nor listed",
        ),
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
        (lambda ms: ms.clear(), "exactly one entry"),
        (lambda ms: ms[0]["datasets"].reverse(), "listed as '0', '1'"),
        (lambda ms: ms[0]["datasets"][1].update(crs="EPSG:4326"), "expected 'EPSG:32631'"),
        (lambda ms: ms[0]["datasets"][1].update(pixels_per_tile=256), "identical at every level"),
    ],
)
def test_bad_multiscales_is_rejected(store_copy, edit, message):
    _edit_root(store_copy, lambda key, value: edit(value) if key == "multiscales" else None)
    with pytest.raises(schema.SchemaError, match=message):
        chronozarr.open_store(store_copy)
    assert any(message in p for p in chronozarr.validate(store_copy))


def test_validator_reports_structural_problems(store_copy):
    root = zarr.open_group(str(store_copy), mode="r+", zarr_format=3, use_consolidated=False)
    root["1"]["data"].attrs["spatial:shape"] = [1, 1]
    root["1"].attrs["transform"] = [30.0, 0.0, 746090.0, 0.0, -30.0, 2540440.0]
    problems = chronozarr.validate(store_copy)
    assert any("not the level-0 transform scaled by 2^1" in p for p in problems)
    assert any("attribute spatial:shape must be [350, 300]" in p for p in problems)
    assert any("1/data: consolidated metadata is stale" in p for p in problems)


def test_validator_flags_missing_arrays_and_wrong_coordinates(store_copy):
    shutil.rmtree(store_copy / "0" / "x")
    root = zarr.open_group(str(store_copy), mode="r+", zarr_format=3, use_consolidated=False)
    root["1"]["time"][:] = np.array([0, 1, 2], dtype="int64")
    problems = chronozarr.validate(store_copy)
    assert any("0/x: listed in consolidated metadata but missing on disk" in p for p in problems)
    assert any("level 0: array 'x' is missing" in p for p in problems)
    assert any("level 1/time: values differ" in p for p in problems)


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


def test_optional_data_attrs_are_not_required_but_must_be_correct_when_present(store_copy):
    root = zarr.open_group(str(store_copy), mode="r+", zarr_format=3, use_consolidated=False)
    data = root["0"]["data"]
    for key in ("proj:code", "spatial:bbox", "spatial:transform", "spatial:shape", "crs"):
        del data.attrs[key]
    problems = chronozarr.validate(store_copy)
    assert not [p for p in problems if "attribute" in p]
    assert chronozarr.open_store(store_copy).levels[0].shape == (3, 2, 700, 600)

    data.attrs["spatial:bbox"] = [0.0, 0.0, 1.0, 1.0]
    assert any("attribute spatial:bbox must be" in p for p in chronozarr.validate(store_copy))


def test_delta_reference_rule_accepts_any_anchor_within_one_interval():
    anchors = [0, 6, 12]
    nearest = {1: 0, 2: 0, 3: 0, 4: 6, 5: 6, 7: 6, 8: 6, 9: 6, 10: 12, 11: 12}
    preceding = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 7: 6, 8: 6, 9: 6, 10: 6, 11: 6}
    assert schema.delta_reference_problem(nearest, anchors, 13, 6) is None
    assert schema.delta_reference_problem(preceding, anchors, 13, 6) is None
    assert schema.delta_reference_problem({**preceding, 5: 6}, anchors, 13, 6) is None


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda r: r.pop(7), "[7] are neither anchors nor listed"),
        (lambda r: r.update({6: 0}), "[6] are anchors or outside"),
        (lambda r: r.update({40: 0}), "[40] are anchors or outside"),
        (lambda r: r.update({7: 8}), "timestep 7 references 8, which is not an anchor"),
        (lambda r: r.update({8: 0}), "timestep 8 references anchor 0 at distance 8"),
        (lambda r: r.update({1: 12}), "timestep 1 references anchor 12 at distance 11"),
    ],
)
def test_delta_reference_rule_names_the_violation(change, message):
    reference = {1: 0, 2: 0, 3: 0, 4: 6, 5: 6, 7: 6, 8: 6, 9: 6, 10: 12, 11: 12}
    change(reference)
    assert message in (schema.delta_reference_problem(reference, [0, 6, 12], 13, 6) or "")


def test_an_interval_of_one_has_no_references():
    assert schema.delta_reference_problem({}, [0, 1, 2], 3, 1) is None
    assert "anchors or outside" in (schema.delta_reference_problem({1: 0}, [0, 1, 2], 3, 1) or "")
