"""Zarr and NetCDF conversion adapter through xarray."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import xarray as xr

from chronozarr import schema
from chronozarr._convert_source import (
    Grid,
    Source,
    SourceInfo,
    Step,
    _decide_validity,
    _declared_nodata,
    _epsg,
    _scaling,
    _settle_nan,
    _value_valid,
)
from chronozarr.schema import Band
from chronozarr.store import as_store

NETCDF_SUFFIXES = (".nc", ".nc4", ".cdf")


_DIM_ALIASES = {
    "time": ("time", "t", "datetime", "valid_time"),
    "band": ("band", "bands", "channel", "variable"),
    "y": ("y", "lat", "latitude", "northing"),
    "x": ("x", "lon", "longitude", "easting"),
}


def _parse_dims(spec: str | None) -> dict[str, str]:
    if not spec:
        return {}
    out: dict[str, str] = {}
    for part in spec.split(","):
        key, sep, name = part.partition("=")
        if not sep or key.strip() not in _DIM_ALIASES or not name.strip():
            raise ValueError(
                f"bad --dims entry {part!r}: use time=NAME,y=NAME,x=NAME[,band=NAME] "
                f"(keys {list(_DIM_ALIASES)})"
            )
        out[key.strip()] = name.strip()
    return out


def _open_dataset(source: str) -> xr.Dataset:
    if source.lower().endswith(NETCDF_SUFFIXES):
        if not Path(source).is_file():
            raise FileNotFoundError(f"NetCDF file {source} does not exist")
        return xr.open_dataset(source, mask_and_scale=False)
    if not source.startswith(("http://", "https://")) and not Path(source).exists():
        raise FileNotFoundError(f"input {source} does not exist")
    store = as_store(source)
    try:
        return xr.open_zarr(store, chunks=None, consolidated=True, mask_and_scale=False)
    except (KeyError, ValueError):
        if source.startswith(("http://", "https://")):
            raise ValueError(
                f"{source}: a remote Zarr input needs consolidated metadata (.zmetadata or "
                "consolidated zarr.json) because HTTP stores cannot be listed"
            ) from None
    # Local store without consolidated metadata: xarray falls back to listing and warns.
    return xr.open_zarr(store, chunks=None, consolidated=False, mask_and_scale=False)


def _first_present(names: Sequence[Any], aliases: Sequence[str]) -> str | None:
    lowered = {str(n).lower(): str(n) for n in names}
    return next((lowered[a] for a in aliases if a in lowered), None)


class XarraySource(Source):
    def __init__(
        self,
        source: str,
        variable: str | None,
        dims: dict[str, str],
        crs: str | None,
        nodata: float | int | None | str,
        mask_var: str | None = None,
    ) -> None:
        self.kind = "NetCDF file" if source.lower().endswith(NETCDF_SUFFIXES) else "Zarr store"
        self.source = source
        self.variable_name = variable
        dataset = _open_dataset(source)
        names = [n for n in dataset.data_vars if n != mask_var]
        if variable is None:
            if len(names) != 1:
                raise ValueError(f"{source} has variables {names}; choose one with --variable")
            variable = str(names[0])
        if variable not in dataset.data_vars:
            raise ValueError(f"variable {variable!r} is not in {source}: {names}")
        da = dataset[variable]
        self.variable = variable
        resolved = {}
        for key, aliases in _DIM_ALIASES.items():
            found = dims.get(key) or _first_present(list(da.dims), aliases)
            if found is not None and found not in da.dims:
                raise ValueError(f"--dims {key}={found}: {variable} has dims {list(da.dims)}")
            if found is not None:
                resolved[key] = found
        for key in ("time", "y", "x"):
            if key not in resolved:
                raise ValueError(
                    f"cannot find the {key} dimension of {variable} (dims {list(da.dims)}); "
                    f"name it with --dims {key}=NAME"
                )
        if "band" not in resolved and len(da.dims) != 3:
            raise ValueError(
                f"{variable} has dims {list(da.dims)}; expected time, y, x and optionally a band "
                "dimension (name it with --dims band=NAME)"
            )
        if len(da.dims) != len(resolved):
            extra = sorted(str(d) for d in set(da.dims) - set(resolved.values()))
            raise ValueError(
                f"{variable} has unexpected dimensions {extra}; select or squeeze them first"
            )
        self._dims = resolved
        if da.dtype.name not in schema.DTYPES:
            raise ValueError(
                f"{variable} is {da.dtype}; chronozarr stores hold {list(schema.DTYPES)}. "
                "Cast it first"
            )

        time_values = np.asarray(da[resolved["time"]].values)
        if not np.issubdtype(time_values.dtype, np.datetime64):
            raise ValueError(
                f"the time coordinate {resolved['time']!r} is {time_values.dtype}, not datetimes; "
                "decode it (CF units) or convert it to datetime64"
            )
        order = np.argsort(time_values, kind="stable")
        self._order = [int(i) for i in order]
        sorted_times = time_values[order].astype("datetime64[ms]")
        if len(sorted_times) > 1 and not (np.diff(sorted_times.astype(np.int64)) > 0).all():
            raise ValueError(f"the time coordinate of {variable} has duplicate values")
        self.times = list(sorted_times)

        x = np.asarray(da[resolved["x"]].values, dtype=np.float64)
        y = np.asarray(da[resolved["y"]].values, dtype=np.float64)
        if len(x) < 2 or len(y) < 2:
            raise ValueError("need at least 2 pixels along x and y to derive the grid")
        dx, dy = float(x[1] - x[0]), float(y[1] - y[0])
        if dx <= 0:
            raise ValueError("x must increase; flip the data first")
        if not (np.allclose(np.diff(x), dx) and np.allclose(np.diff(y), dy)):
            raise ValueError("x/y coordinates are not regularly spaced")
        self._flip_y = dy > 0
        y_top = float(y[-1]) if self._flip_y else float(y[0])
        pixel_h = abs(dy)
        transform = (dx, 0.0, float(x[0]) - dx / 2, 0.0, -pixel_h, y_top + pixel_h / 2)
        crs_text = crs or self._crs_from_attrs(da, dataset)
        if crs_text is None:
            raise ValueError(f"{source} does not declare a CRS; pass --crs EPSG:xxxxx")
        from rasterio.crs import CRS  # ty: ignore[unresolved-import]  # compiled, no stubs

        grid = Grid(_epsg(CRS.from_user_input(crs_text), "CRS"), transform, len(y), len(x))

        self._mask = None
        if mask_var is not None:
            self._mask = self._mask_variable(dataset, mask_var, variable, resolved)
        declared = {
            token
            for key in ("_FillValue", "missing_value")
            for value in np.atleast_1d(da.attrs.get(key, [])).tolist()
            if (token := _declared_nodata(value, da.dtype)) is not None
        }
        validity = _decide_validity(
            da.dtype,
            declared,
            override=nodata,
            explicit=[] if mask_var is None else [f"mask variable {mask_var}"],
            footprint=False,
        )
        self._sentinels: tuple[float | int, ...]
        if nodata == "auto":
            self._sentinels = tuple(t for t in declared if not isinstance(t, str))
        else:
            self._sentinels = () if validity.nodata is None else (validity.nodata,)

        if "band" in resolved:
            band_names = tuple(str(b) for b in da[resolved["band"]].values)
        else:
            band_names = (variable,)
        scale, offset = _scaling(
            self._scalar_attr(da, "scale_factor", 1.0),
            self._scalar_attr(da, "add_offset", 0.0),
            f"variable {variable!r}",
        )
        units = da.attrs.get("units")
        bands = tuple(
            Band(name=n, scale=scale, offset=offset, units=str(units) if units else None)
            for n in band_names
        )
        self.da = da
        self.info = SourceInfo(
            grid,
            len(band_names),
            da.dtype,
            validity.nodata,
            band_names,
            bands,
            mask=validity.mask,
            validity=validity.why,
        )

    @staticmethod
    def _scalar_attr(da: xr.DataArray, key: str, default: float) -> float:
        raw = da.attrs.get(key)
        if raw is None:
            return default
        values = np.atleast_1d(raw)
        if values.size != 1 or values.dtype.kind not in "iuf":
            raise ValueError(
                f"{da.name}: {key} must be one number (CF attributes are per variable), "
                f"got {raw!r}"
            )
        return float(values[0])

    def _mask_variable(
        self, dataset: xr.Dataset, name: str, variable: str, resolved: dict[str, str]
    ) -> xr.DataArray:
        if name not in dataset.variables:
            raise ValueError(
                f"mask variable {name!r} is not in {self.source}: "
                f"{sorted(str(v) for v in dataset.variables)}"
            )
        if name == variable:
            raise ValueError(f"mask variable {name!r} is the data variable; name another")
        mask = dataset[name]
        wanted = {resolved[k] for k in ("time", "y", "x")}
        if len(mask.dims) != 3 or set(mask.dims) != wanted:
            raise ValueError(
                f"mask variable {name!r} has dims {list(mask.dims)}; it must have exactly "
                f"{sorted(wanted)} like {variable} (one plane per timestep, shared by all bands)"
            )
        if mask.dtype.kind not in "biu":
            raise ValueError(
                f"mask variable {name!r} is {mask.dtype}; use a boolean or integer variable "
                "where nonzero means valid (invert a 'nonzero = bad' flag first)"
            )
        return mask

    @staticmethod
    def _crs_from_attrs(da: xr.DataArray, dataset: xr.Dataset) -> str | None:
        for holder in (da.attrs, dataset.attrs):
            for key in ("crs", "crs_wkt", "spatial_ref"):
                if holder.get(key):
                    return str(holder[key])
        for name in ("spatial_ref", "crs"):
            if name in dataset.variables:
                attrs = dataset[name].attrs
                for key in ("crs_wkt", "spatial_ref", "crs"):
                    if attrs.get(key):
                        return str(attrs[key])
        return None

    def fingerprint(self) -> Any:
        grid = self.info.grid
        return {
            "kind": "xarray",
            "source": self.source,
            "variable": self.variable,
            "grid": [grid.crs, list(grid.transform), grid.height, grid.width],
            "bands": [b.to_attrs() for b in self.info.bands],
            "times": [str(t) for t in self.times],
            "dtype": self.info.dtype.name,
            "nodata": self.info.nodata,
            "mask": self.info.mask,
            "validity": self.info.validity,
            "mask_var": None if self._mask is None else self._mask.name,
        }

    def read(self, t: int) -> Step:
        d = self._dims
        piece = self.da.isel({d["time"]: self._order[t]})
        order = [d["band"], d["y"], d["x"]] if "band" in d else [d["y"], d["x"]]
        values = np.asarray(piece.transpose(*order).values)
        if "band" not in d:
            values = values[np.newaxis]
        if self._flip_y:
            values = values[:, ::-1, :]
        values = np.ascontiguousarray(values)
        if not values.flags.writeable:
            values = values.copy()
        valid = None
        if self.info.mask:
            ok = _value_valid(values, self._sentinels)
            if self._mask is not None:
                plane = self._mask.isel({d["time"]: self._order[t]}).transpose(d["y"], d["x"])
                ok &= (plane.values[::-1] if self._flip_y else plane.values) != 0
            valid = ok.astype(np.uint8)
        _settle_nan(values, valid, self.info.nodata, f"{self.source} timestep {t}")
        return Step(values, valid)
