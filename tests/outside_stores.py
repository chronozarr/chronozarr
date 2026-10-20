"""Zarr stores shaped like those other writers produce, for the readers' tests.

Each builder writes a local store with xarray and zarr-python, as the original tools do, and
returns the arrays it wrote so that a test compares what chronozarr reads with the source.

- `write_ndpyramid_coarsen` and `write_ndpyramid_reproject`: the legacy `datasets` list of
  ndpyramid 0.4 (no `proj:` or `spatial:` attributes, a CF `spatial_ref` variable, x and y
  coordinate arrays, time as CF days). The reprojected pyramid runs coarse to fine and keeps all
  timesteps in one chunk.
- `write_eopf_reflectance`: the `measurements/reflectance` group of an ESA EOPF Sentinel-2
  product (multiscales v1 `layout` with `spatial:transform`, one 2-D array per band, no time,
  scale and offset in `_eopf_attrs`, registrations spelled with `refs/tags/v1` and `geo-proj`).
"""

from __future__ import annotations

import base64
import struct
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import xarray as xr
import zarr
from zarr.errors import ZarrUserWarning

from tests.narrow import array_at

GEOGRAPHIC_WKT = (
    'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563,'
    'AUTHORITY["EPSG","7030"]],AUTHORITY["EPSG","6326"]],PRIMEM["Greenwich",0,'
    'AUTHORITY["EPSG","8901"]],UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],'
    'AXIS["Latitude",NORTH],AXIS["Longitude",EAST],AUTHORITY["EPSG","4326"]]'
)
WEB_MERCATOR_WKT = (
    'PROJCS["WGS 84 / Pseudo-Mercator",GEOGCS["WGS 84",DATUM["WGS_1984",'
    'SPHEROID["WGS 84",6378137,298.257223563,AUTHORITY["EPSG","7030"]],AUTHORITY["EPSG","6326"]],'
    'PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],UNIT["degree",0.0174532925199433,'
    'AUTHORITY["EPSG","9122"]],AUTHORITY["EPSG","4326"]],PROJECTION["Mercator_1SP"],'
    'PARAMETER["central_meridian",0],PARAMETER["scale_factor",1],PARAMETER["false_easting",0],'
    'PARAMETER["false_northing",0],UNIT["metre",1,AUTHORITY["EPSG","9001"]],AXIS["Easting",EAST],'
    'AXIS["Northing",NORTH],AUTHORITY["EPSG","3857"]]'
)
MERCATOR_EXTENT = 20037508.342789244
TIMES = np.array(["2020-01-01", "2020-02-01", "2020-03-01"], dtype="datetime64[ns]")


@dataclass
class Written:
    """What a builder wrote: the arrays by level path, and the band names in stored order."""

    path: Path
    levels: dict[str, np.ndarray] = field(default_factory=dict)
    bands: list[str] = field(default_factory=list)


def _consolidate(path: Path) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ZarrUserWarning)  # consolidated metadata is deliberate
        zarr.consolidate_metadata(str(path))


def fill_value_attr(value: float, kind: str = "f") -> str:
    """`_FillValue` the way xarray writes it into Zarr v3 attributes: base64 of 8 bytes."""
    packed = {"f": "<d", "i": "<q", "u": "<Q"}[kind]
    return base64.standard_b64encode(struct.pack(packed, value)).decode()


def _field(rng: np.random.Generator, n_time: int, height: int, width: int) -> np.ndarray:
    values = rng.normal(15.0, 8.0, size=(n_time, height, width)).astype("float32")
    values[:, :3, :5] = np.nan
    return values


def _coarsen(values: np.ndarray, factor: int) -> np.ndarray:
    """Block mean over factor x factor, NaN where every pixel is NaN (trim boundary)."""
    n_time, height, width = values.shape
    h, w = height // factor * factor, width // factor * factor
    blocks = values[:, :h, :w].reshape(n_time, h // factor, factor, w // factor, factor)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return np.nanmean(blocks, axis=(2, 4)).astype("float32")


def _spatial_ref(wkt: str, grid_mapping: str) -> xr.DataArray:
    return xr.DataArray(
        np.int64(0), attrs={"crs_wkt": wkt, "spatial_ref": wkt, "grid_mapping_name": grid_mapping}
    )


def write_ndpyramid_coarsen(
    path: Path, *, n_time: int = 3, height: int = 48, width: int = 64, seed: int = 3
) -> Written:
    """`pyramid_coarsen(factors=[1, 2, 4])` on a geographic grid: level 0 is the finest."""
    rng = np.random.default_rng(seed)
    base = _field(rng, n_time, height, width)
    written = Written(path, bands=["tmax"])
    step = 360.0 / width
    for level, factor in enumerate((1, 2, 4)):
        values = _coarsen(base, factor) if factor > 1 else base
        h, w = values.shape[1:]
        x = -180.0 + step * factor * (np.arange(w) + 0.5)
        y = 90.0 - step * factor * (np.arange(h) + 0.5)
        dataset = xr.Dataset(
            {
                "tmax": (
                    ("time", "y", "x"),
                    values,
                    {"scale_factor": 1.0, "add_offset": 0.0, "coordinates": "spatial_ref"},
                )
            },
            coords={
                "time": TIMES[:n_time],
                "x": x,
                "y": y,
                "spatial_ref": _spatial_ref(GEOGRAPHIC_WKT, "latitude_longitude"),
            },
        )
        dataset.to_zarr(
            path,
            group=str(level),
            mode="a",
            zarr_format=3,
            consolidated=False,
            encoding={"tmax": {"chunks": (1, max(h // 2, 4), max(w // 2, 4))}},
        )
        written.levels[str(level)] = values
    root = zarr.open_group(str(path), mode="r+", zarr_format=3, use_consolidated=False)
    root.attrs["multiscales"] = [
        {
            "datasets": [{"path": "0"}, {"path": "1"}, {"path": "2"}],
            "type": "reduce",
            "metadata": {"method": "pyramid_coarsen", "version": "0.4.0"},
        }
    ]
    _consolidate(path)
    return written


def write_ndpyramid_reproject(path: Path, *, n_time: int = 3, seed: int = 4) -> Written:
    """`pyramid_reproject(levels=3, pixels_per_tile=16)`: Web Mercator, coarse level first."""
    rng = np.random.default_rng(seed)
    written = Written(path, bands=["tmax"])
    datasets: list[dict[str, Any]] = []
    for level in range(3):
        size = 16 * 2**level
        values = _field(rng, n_time, size, size)
        centres = (np.arange(size) + 0.5) * (2 * MERCATOR_EXTENT / size)
        dataset = xr.Dataset(
            {
                "tmax": (
                    ("time", "y", "x"),
                    values,
                    {"scale_factor": 1.0, "add_offset": 0.0, "coordinates": "spatial_ref"},
                )
            },
            coords={
                "time": TIMES[:n_time],
                "x": ("x", (-MERCATOR_EXTENT + centres).astype("float32")),
                "y": ("y", (MERCATOR_EXTENT - centres).astype("float32")),
                "spatial_ref": _spatial_ref(WEB_MERCATOR_WKT, "mercator"),
            },
        )
        dataset.to_zarr(
            path,
            group=str(level),
            mode="a",
            zarr_format=3,
            consolidated=False,
            encoding={"tmax": {"chunks": (n_time, 16, 16)}, "time": {"dtype": "int32"}},
        )
        written.levels[str(level)] = values
        datasets.append(
            {"path": str(level), "level": level, "crs": "EPSG:3857", "pixels_per_tile": 16}
        )
    root = zarr.open_group(str(path), mode="r+", zarr_format=3, use_consolidated=False)
    root.attrs["multiscales"] = [
        {"datasets": datasets, "type": "reduce", "metadata": {"method": "pyramid_reproject"}}
    ]
    _consolidate(path)
    return written


EOPF_LEVEL_BANDS = {
    "r10m": ("b02", "b03", "b04", "b08"),
    "r20m": ("b02", "b03", "b04", "b8a", "b11"),
    "r60m": ("b01", "b02", "b03", "b04", "b8a", "b11"),
}
EOPF_BANDS = ("b02", "b03", "b04")  # the bands every level has
EOPF_ORIGIN = (399960.0, 4600020.0)
EOPF_RESOLUTIONS = (("r10m", 10.0), ("r20m", 20.0), ("r60m", 60.0))
EOPF_SIZE = 120


def _eopf_registrations() -> list[dict[str, str]]:
    def entry(uuid: str, name: str, repo: str, description: str) -> dict[str, str]:
        return {
            "uuid": uuid,
            "schema_url": f"https://raw.githubusercontent.com/{repo}/refs/tags/v1/schema.json",
            "spec_url": f"https://github.com/{repo}/blob/v1/README.md",
            "name": name,
            "description": description,
        }

    return [
        entry(
            "d35379db-88df-4056-af3a-620245f8e347",
            "multiscales",
            "zarr-conventions/multiscales",
            "Multiscale layout of zarr datasets",
        ),
        entry(
            "689b58e2-cf7b-45e0-9fff-9cfc0883d6b4",
            "spatial:",
            "zarr-conventions/spatial",
            "Spatial coordinate and transformation information",
        ),
        entry(
            "f17cb550-5864-4468-aeb7-f3180cfb622f",
            "proj:",
            "zarr-experimental/geo-proj",
            "Coordinate reference system information for geospatial data",
        ),
    ]


def write_eopf_reflectance(path: Path, *, seed: int = 5, encoded: bool = False) -> Written:
    """An EOPF `measurements/reflectance` group: r10m, r20m and r60m, each with its own bands.

    Like the product, a level has the bands of its resolution, so only `EOPF_BANDS` are at every
    level, and `Written.levels` holds those as stored. `_eopf_attrs` carries the CF scale 0.0001
    and offset -0.1 of the `<u2` digital counts. By default the arrays hold float32 reflectance
    already decoded from the counts, as the public product does: 0.0 marks a missing pixel at
    r10m and r20m and NaN at r60m (the overview levels of the product use NaN). With `encoded`
    the arrays hold the uint16 counts, and 0 marks a missing pixel at every level.
    """
    rng = np.random.default_rng(seed)
    written = Written(path, bands=list(EOPF_BANDS))
    root = zarr.open_group(str(path), mode="w", zarr_format=3)
    layout: list[dict[str, Any]] = []
    for asset, resolution in EOPF_RESOLUTIONS:
        size = int(EOPF_SIZE * 10.0 / resolution)
        transform = [resolution, 0.0, EOPF_ORIGIN[0], 0.0, -resolution, EOPF_ORIGIN[1]]
        layout.append(
            {"asset": asset, "spatial:shape": [size, size], "spatial:transform": transform}
        )
        group = root.create_group(asset)
        group.attrs.update(
            {
                "grid_mapping": "spatial_ref",
                "spatial:dimensions": ["y", "x"],
                "spatial:registration": "pixel",
                "spatial:shape": [size, size],
                "spatial:transform": transform,
                "proj:code": "EPSG:32633",
            }
        )
        names = EOPF_LEVEL_BANDS[asset]
        counts = rng.integers(1001, 9000, size=(len(names), size, size))  # never 0.0 once decoded
        counts[:, :4, :6] = 0
        nan_fill = not encoded and asset == "r60m"
        dtype = "uint16" if encoded else "float32"
        for i, band in enumerate(names):
            if encoded:
                values = counts[i]
            else:
                values = np.where(
                    counts[i] > 0, counts[i] * 0.0001 - 0.1, np.nan if nan_fill else 0.0
                )
            array = group.create_array(
                band,
                shape=(size, size),
                chunks=(size // 2, size // 2),
                dtype=dtype,
                fill_value=0,
                dimension_names=("y", "x"),
                attributes={
                    "_eopf_attrs": {
                        "scale_factor": 0.0001,
                        "add_offset": -0.1,
                        "dtype": "<u2",
                        "fill_value": 0,
                        "valid_min": 1,
                        "valid_max": 65535,
                    },
                    "dtype": "<u2",
                    "units": "digital_counts",
                    "grid_mapping": "spatial_ref",
                    "_FillValue": fill_value_attr(np.nan if nan_fill else 0.0),
                },
            )
            array[:] = values.astype(dtype)
        x = EOPF_ORIGIN[0] + resolution * (np.arange(size) + 0.5)
        y = EOPF_ORIGIN[1] - resolution * (np.arange(size) + 0.5)
        for name, coordinate in (("x", x), ("y", y)):
            axis = group.create_array(
                name, shape=(size,), dtype="float32", dimension_names=(name,), fill_value=np.nan
            )
            axis[:] = coordinate.astype("float32")
        group.create_array(
            "spatial_ref",
            shape=(),
            dtype="int64",
            fill_value=0,
            attributes={"crs_wkt": 'PROJCS["WGS 84 / UTM zone 33N"]'},
        )
        written.levels[asset] = np.stack([np.asarray(array_at(group, n)[:]) for n in EOPF_BANDS])
    root.attrs.update(
        {
            "zarr_conventions": _eopf_registrations(),
            "multiscales": {"layout": layout},
            "spatial:dimensions": ["y", "x"],
            "spatial:registration": "pixel",
            "proj:code": "EPSG:32633",
        }
    )
    _consolidate(path)
    return written
