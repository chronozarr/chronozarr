"""Narrowing helpers for values the libraries type as unions or Optionals.

zarr types `Group[path]` as `Array | Group` and `attrs[key]` as a JSON union, and the readers
return `None` for an absent mask or coverage plane. Each helper asserts the shape a test expects
and returns the narrowed type, so a wrong shape fails with a message and not an AttributeError.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any, TypeVar

import zarr

T = TypeVar("T")


def required(value: T | None) -> T:
    """`value`, asserted to be present."""
    assert value is not None, "expected a value, got None"
    return value


def array_at(group: zarr.Group, path: str) -> zarr.Array:
    """The array at `path` (slash-separated) below `group`."""
    node = group[path]
    assert isinstance(node, zarr.Array), f"{path!r} is not an array"
    return node


def group_at(group: zarr.Group, path: str) -> zarr.Group:
    """The group at `path` (slash-separated) below `group`."""
    node = group[path]
    assert isinstance(node, zarr.Group), f"{path!r} is not a group"
    return node


def attrs_block(node: zarr.Group | zarr.Array, key: str) -> dict[str, Any]:
    """A deep copy of the JSON object under `node.attrs[key]`, safe to edit and write back."""
    value = node.attrs[key]
    assert isinstance(value, Mapping), f"attrs[{key!r}] is not an object"
    return copy.deepcopy(dict(value))
