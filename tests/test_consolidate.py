"""Local inline metadata remains compatible with the standard Zarr reader."""

import json
import warnings

import numpy as np
import pytest
import zarr
from zarr.errors import ZarrUserWarning

from chronozarr._consolidate import consolidate_zarr_metadata
from tests.narrow import array_at, group_at

pytestmark = pytest.mark.unit


def test_consolidation_uses_current_nested_nodes_and_leaves_other_files_alone(tmp_path):
    root = zarr.open_group(tmp_path, mode="w", zarr_format=3, attributes={"title": "store"})
    level = root.create_group("level", attributes={"title": "level"})
    details = level.create_group("details")
    values = np.arange(4, dtype="uint16")
    array = details.create_array("values", shape=(2,), chunks=(2,), dtype="uint16")
    array[:] = values[:2]
    with warnings.catch_warnings(action="ignore", category=ZarrUserWarning):
        zarr.consolidate_metadata(tmp_path, path="level")

    array.resize((4,))
    array[:] = values
    details.create_group("empty", attributes={"title": "empty"})
    opaque = tmp_path / "untracked" / "notes.bin"
    opaque.parent.mkdir()
    opaque.write_bytes(b"not a Zarr node")
    foreign_directory = opaque.parent / "zarr.json"
    foreign_directory.mkdir()
    incomplete = tmp_path / "incomplete-array"
    incomplete.mkdir()
    (incomplete / "zarr.json").write_text(
        '{"zarr_format": 3, "node_type": "array"}', encoding="utf-8"
    )
    child_root = (tmp_path / "level" / "zarr.json").read_bytes()

    with warnings.catch_warnings(action="error", category=ZarrUserWarning):
        consolidate_zarr_metadata(tmp_path)

    index = json.loads((tmp_path / "zarr.json").read_bytes())["consolidated_metadata"]
    assert index["kind"] == "inline"
    assert set(index["metadata"]) == {
        "level",
        "level/details",
        "level/details/empty",
        "level/details/values",
    }
    assert index["metadata"]["level"]["consolidated_metadata"]["metadata"] == {}
    assert index["metadata"]["level/details/empty"]["consolidated_metadata"]["metadata"] == {}
    consolidated = zarr.open_group(tmp_path, mode="r", use_consolidated=True)
    assert consolidated.attrs["title"] == "store"
    assert group_at(consolidated, "level").attrs["title"] == "level"
    assert group_at(consolidated, "level/details/empty").attrs["title"] == "empty"
    np.testing.assert_array_equal(array_at(consolidated, "level/details/values")[:], values)
    assert (tmp_path / "level" / "zarr.json").read_bytes() == child_root
    assert opaque.read_bytes() == b"not a Zarr node"
    assert foreign_directory.is_dir()


@pytest.mark.parametrize(
    ("node", "error"),
    [
        ({"node_type": "other"}, ValueError),
        ({"node_type": "group", "attributes": []}, TypeError),
    ],
    ids=["unknown-node-type", "malformed-group"],
)
def test_consolidation_keeps_the_root_when_a_child_has_invalid_metadata(tmp_path, node, error):
    root = zarr.open_group(tmp_path, mode="w", zarr_format=3)
    root.create_group("level")
    consolidate_zarr_metadata(tmp_path)
    root_metadata = tmp_path / "zarr.json"
    original = root_metadata.read_bytes()
    child = tmp_path / "invalid"
    child.mkdir()
    (child / "zarr.json").write_text(json.dumps({"zarr_format": 3, **node}), encoding="utf-8")

    with pytest.raises(error):
        consolidate_zarr_metadata(tmp_path)

    assert root_metadata.read_bytes() == original
