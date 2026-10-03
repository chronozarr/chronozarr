"""CSV and JSON conversion manifest parsing."""

from __future__ import annotations

import csv
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy as np

from chronozarr._convert_source import Bounds, check_bounds


@dataclass(frozen=True)
class Entry:
    """One timestep of a manifest."""

    uri: str
    time: np.datetime64


@dataclass(frozen=True)
class Manifest:
    """A parsed manifest: entries sorted by time, the band names it gives (None: take them from
    the sources) and the `bounds` of a JSON manifest (None: none given)."""

    entries: list[Entry]
    bands: tuple[str, ...] | None
    bounds: Bounds | None


def parse_time(text: str, where: str) -> np.datetime64:
    """ISO-8601 date or datetime (Z or an offset allowed) as UTC milliseconds."""
    try:
        moment = datetime.fromisoformat(text.strip())
    except ValueError as exc:
        raise ValueError(
            f"{where}: cannot parse datetime {text!r}; use ISO-8601 like 2024-03-01 or "
            "2024-03-01T10:30:00Z"
        ) from exc
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC).replace(tzinfo=None)
    return np.datetime64(moment, "ms")


def _resolve_uri(uri: str, base: Path) -> str:
    if "://" in uri or uri.startswith("/vsi") or Path(uri).is_absolute():
        return uri
    return str((base / uri).resolve())


def _split_bands(value: object, where: str) -> tuple[str, ...] | None:
    if value is None or value == "" or value == []:
        return None
    if isinstance(value, str):
        names = [n.strip() for n in value.split(";") if n.strip()]
    elif isinstance(value, list) and all(isinstance(n, str) for n in value):
        names = [str(n).strip() for n in value]
    else:
        raise ValueError(f"{where}: bands must be a list of names (CSV: separated by ';')")
    return tuple(names)


def read_manifest(path: Path) -> Manifest:
    """The entries, band names and bounds of a CSV or JSON manifest.

    CSV: header `uri,datetime[,bands]`, band names separated by `;`. JSON: a list of
    `{"uri", "datetime", "bands"?}` objects, or `{"bands"?: [...], "bounds"?: [w, s, e, n],
    "items": [...]}`; `bounds` is the extent of every frame in the units of the CRS, for frames
    with no georeferencing (see `convert`). Relative URIs are resolved against the manifest's
    directory. Every row that names bands must agree.
    """
    if not path.is_file():
        raise FileNotFoundError(f"manifest {path} does not exist")
    rows: list[Mapping[str, Any]]
    top_bands: object = None
    top_bounds: object = None
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            missing = {"uri", "datetime"} - set(reader.fieldnames or [])
            if missing:
                raise ValueError(
                    f"{path}: CSV header must contain uri and datetime (optionally bands); "
                    f"missing {sorted(missing)}, found {reader.fieldnames}"
                )
            rows = list(reader)
    elif path.suffix.lower() == ".json":
        document = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(document, dict):
            top_bands = document.get("bands")
            top_bounds = document.get("bounds")
            rows = document.get("items", [])
        else:
            rows = document
        if not isinstance(rows, list):
            raise ValueError(f"{path}: expected a list of items or an object with 'items'")
    else:
        raise ValueError(f"{path}: a manifest must be .csv or .json")
    if not rows:
        raise ValueError(f"{path}: the manifest has no rows")

    names = _split_bands(top_bands, f"{path} bands")
    entries = []
    for i, row in enumerate(rows, start=1):
        where = f"{path} row {i}"
        if not isinstance(row, Mapping) or not row.get("uri") or not row.get("datetime"):
            raise ValueError(f"{where}: needs both 'uri' and 'datetime'")
        row_names = _split_bands(row.get("bands"), where)
        if row_names is not None:
            if names is not None and row_names != names:
                raise ValueError(
                    f"{where}: bands {list(row_names)} differ from {list(names)}; every row must "
                    "name the same bands in the same order"
                )
            names = row_names
        entries.append(
            Entry(
                _resolve_uri(str(row["uri"]), path.resolve().parent),
                parse_time(str(row["datetime"]), where),
            )
        )
    entries.sort(key=lambda e: e.time)
    for earlier, later in pairwise(entries):
        if earlier.time == later.time:
            raise ValueError(
                f"{path}: two rows share the datetime {later.time}: {earlier.uri} and "
                f"{later.uri}; a store has one timestep per time"
            )
    bounds = None if top_bounds is None else check_bounds(top_bounds, f"{path} bounds")
    return Manifest(entries, names, bounds)
