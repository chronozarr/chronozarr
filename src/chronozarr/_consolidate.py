"""Write the inline metadata for a local Zarr v3 group."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from zarr.core.buffer import default_buffer_prototype
from zarr.core.group import ConsolidatedMetadata, GroupMetadata
from zarr.core.metadata import ArrayV3Metadata
from zarr.core.sync import sync
from zarr.storage import LocalStore


def _read_group(path: Path, document: dict[str, Any]) -> GroupMetadata:
    # Rebuild from the current node documents even when an old inline index exists.
    document.pop("consolidated_metadata", None)
    group = GroupMetadata.from_dict(document)
    index = ConsolidatedMetadata(metadata={})
    # Only groups have child nodes. Never descend into an array's chunk keys.
    for child in path.iterdir():
        if not child.is_dir():
            continue
        metadata_path = child / "zarr.json"
        if metadata_path.is_dir():
            continue
        try:
            node = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
            continue
        match node:
            case {"node_type": "group"}:
                index.metadata[child.name] = _read_group(child, node)
            case {"node_type": "array"}:
                try:
                    array = ArrayV3Metadata.from_dict(node)
                except KeyError:
                    # Zarr's member listing also skips arrays with missing metadata fields.
                    continue
                index.metadata[child.name] = array
            case _:
                raise ValueError(f"{metadata_path}: invalid Zarr node type")
    return replace(group, consolidated_metadata=index)


def consolidate_zarr_metadata(path: Path) -> None:
    """Refresh the root's inline index from the current on-disk node metadata.

    Chronozarr deliberately uses inline v3 metadata (spec 3.1). Build it with Zarr's metadata
    types so publication does not need a process-wide warning scope around its consolidator.
    """
    root = path / "zarr.json"
    document = json.loads(root.read_text(encoding="utf-8"))
    if (
        not isinstance(document, dict)
        or document.get("zarr_format") != 3
        or document.get("node_type") != "group"
    ):
        raise ValueError(f"{root}: consolidation requires a Zarr v3 group")

    metadata = _read_group(path, document)
    serialized = metadata.to_buffer_dict(default_buffer_prototype())["zarr.json"]
    # Use the same atomic local-store write as Zarr's consolidator, including its platform retries.
    sync(LocalStore(path).set("zarr.json", serialized))
