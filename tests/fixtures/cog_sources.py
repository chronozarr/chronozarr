"""Small COG sources for the fidelity tests, with every validity mechanism GDAL has.

Each builder writes one GeoTIFF per timestep into a folder and returns a `CogSet`: the paths, the
exact values written, and what the source says is valid per band, all from the arrays here (not
read back from GDAL), so tests can compare a converted store and an exported COG pixel by pixel.

Special pixels move one column or row per timestep so masks differ between timesteps:

* invalid-nonzero: marked invalid by a mask, an alpha band or a nodata value, but holding a value
  that is not zero (777 for integers);
* valid-zero: holds 0 and is valid, which a nodata sentinel of 0 would wrongly hide.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
import xarray as xr
from rasterio.enums import ColorInterp
from rasterio.transform import Affine

from tests.synthetic import CRS, TRANSFORM, make_times

N_TIME, HEIGHT, WIDTH = 4, 40, 50
DATES = ["2024-01-01", "2024-02-01", "2024-03-01", "2024-04-01"]


@dataclass(frozen=True)
class CogSet:
    """Files plus the truth they encode. Arrays are (time, band, y, x) over the data bands."""

    paths: list[Path]
    data: np.ndarray  # values written, NaN included
    valid: np.ndarray  # bool: the source's own validity, per band
    scales: tuple[float, ...]
    offsets: tuple[float, ...]
    units: tuple[str | None, ...]
    descriptions: tuple[str | None, ...]
    nodata: float | None  # declared in the files (first timestep)

    @property
    def shared_valid(self) -> np.ndarray:
        """(time, y, x): valid in every band, which is all one `mask` plane can say."""
        return np.asarray(self.valid.all(axis=1))

    def dates(self) -> list[str]:
        return DATES[: len(self.paths)]


def write_manifest(path: Path, cogs: CogSet, bands: str | None = None) -> Path:
    header = "uri,datetime" + (",bands" if bands else "")
    rows = [
        f"{tif},{date}" + (f",{bands}" if bands else "")
        for tif, date in zip(cogs.paths, cogs.dates(), strict=True)
    ]
    path.write_text("\n".join([header, *rows[::-1]]) + "\n")  # reversed: manifests need not sort
    return path


def _write_one(
    path: Path,
    array: np.ndarray,
    *,
    nodata: float | None = None,
    mask: np.ndarray | None = None,
    colorinterp: list[ColorInterp] | None = None,
    scales: tuple[float, ...] | None = None,
    offsets: tuple[float, ...] | None = None,
    units: tuple[str | None, ...] | None = None,
    descriptions: tuple[str | None, ...] | None = None,
    transform: tuple[float, ...] = TRANSFORM,
) -> None:
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=array.shape[1],
        width=array.shape[2],
        count=array.shape[0],
        dtype=array.dtype,
        crs=CRS,
        transform=Affine(*transform),
        nodata=nodata,
    ) as dst:
        dst.write(array)
        if mask is not None:
            dst.write_mask(np.where(mask, 255, 0).astype(np.uint8))
        if colorinterp is not None:
            dst.colorinterp = colorinterp
        if scales is not None:
            dst.scales = list(scales)
        if offsets is not None:
            dst.offsets = list(offsets)
        if units is not None:
            dst.units = [u or "" for u in units]
        if descriptions is not None:
            dst.descriptions = list(descriptions)


def _shifted(columns: int) -> tuple[float, ...]:
    a, b, c, d, e, f = TRANSFORM
    return (a, b, c + columns * a, d, e, f)


def _rng(seed: int) -> np.random.Generator:
    return np.random.default_rng(seed)


def _uint16(n_band: int, seed: int) -> np.ndarray:
    return _rng(seed).integers(100, 6000, size=(N_TIME, n_band, HEIGHT, WIDTH)).astype(np.uint16)


def scaled_masked(folder: Path, shift: dict[int, int] | None = None) -> CogSet:
    """uint16 reflectance, per-band scale and offset, an internal mask, no nodata tag.

    The mask marks (3+t, 4) invalid although it holds 777; (5, 6+t) holds 0 and is valid.
    `shift` maps a timestep to a number of pixels its grid is moved east (so it must be warped).
    """
    data = _uint16(2, seed=1)
    valid = np.ones(data.shape, dtype=bool)
    for t in range(N_TIME):
        data[t, :, 3 + t, 4] = 777
        valid[t, :, 3 + t, 4] = False
        data[t, :, 5, 6 + t] = 0
    scales, offsets = (0.0001, 0.0002), (-0.1, 0.05)
    units, descriptions = ("reflectance", "reflectance"), ("red", "nir")
    paths = []
    for t in range(N_TIME):
        path = folder / f"masked_{t}.tif"
        _write_one(
            path,
            data[t],
            mask=valid[t, 0],
            scales=scales,
            offsets=offsets,
            units=units,
            descriptions=descriptions,
            transform=_shifted((shift or {}).get(t, 0)),
        )
        paths.append(path)
    return CogSet(paths, data, valid, scales, offsets, units, descriptions, None)


def rgba(folder: Path) -> CogSet:
    """uint8 red, green, blue plus an alpha band. Data bands are the first three.

    Alpha is 0 at (3+t, 4) where the colour is 200, 128 (partly transparent, still valid) at
    (2, 2+t), and the colour is 0 with alpha 255 (valid zero) at (5, 6+t).
    """
    rgb = _rng(2).integers(10, 200, size=(N_TIME, 3, HEIGHT, WIDTH)).astype(np.uint8)
    alpha = np.full((N_TIME, 1, HEIGHT, WIDTH), 255, dtype=np.uint8)
    valid = np.ones(rgb.shape, dtype=bool)
    for t in range(N_TIME):
        rgb[t, :, 3 + t, 4] = 200
        alpha[t, 0, 3 + t, 4] = 0
        valid[t, :, 3 + t, 4] = False
        alpha[t, 0, 2, 2 + t] = 128
        rgb[t, :, 5, 6 + t] = 0
    descriptions = ("red", "green", "blue")
    paths = []
    for t in range(N_TIME):
        path = folder / f"rgba_{t}.tif"
        _write_one(
            path,
            np.concatenate([rgb[t], alpha[t]]),
            colorinterp=[ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha],
            descriptions=(*descriptions, "alpha"),
        )
        paths.append(path)
    return CogSet(paths, rgb, valid, (1.0,) * 3, (0.0,) * 3, (None,) * 3, descriptions, None)


def nodata_zero(folder: Path) -> CogSet:
    """uint16 with nodata 0 and scale, offset, units. Validity differs per band.

    (3+t, 4) is 0 in both bands; (9, 9+t) is 0 in band 0 only.
    """
    data = _uint16(2, seed=3)
    for t in range(N_TIME):
        data[t, :, 3 + t, 4] = 0
        data[t, 0, 9, 9 + t] = 0
    valid = data != 0
    scales, offsets = (0.0001, 0.0001), (-0.1, -0.1)
    units, descriptions = ("reflectance", "reflectance"), ("B04", "B08")
    paths = []
    for t in range(N_TIME):
        path = folder / f"nodata0_{t}.tif"
        _write_one(
            path,
            data[t],
            nodata=0,
            scales=scales,
            offsets=offsets,
            units=units,
            descriptions=descriptions,
        )
        paths.append(path)
    return CogSet(paths, data, valid, scales, offsets, units, descriptions, 0)


def valid_zero(folder: Path) -> CogSet:
    """uint16 with no nodata and no mask: every pixel is valid, including the zero at (5, 6+t)."""
    data = _uint16(2, seed=4)
    for t in range(N_TIME):
        data[t, :, 5, 6 + t] = 0
    paths = []
    for t in range(N_TIME):
        path = folder / f"plain_{t}.tif"
        _write_one(path, data[t])
        paths.append(path)
    valid = np.ones(data.shape, dtype=bool)
    return CogSet(paths, data, valid, (1.0, 1.0), (0.0, 0.0), (None, None), (None, None), None)


def int16_negative(folder: Path) -> CogSet:
    """int16 with negative values, nodata -9999 at (3+t, 4), scale 0.5 and offset -10."""
    data = _rng(5).integers(-2000, 2000, size=(N_TIME, 2, HEIGHT, WIDTH)).astype(np.int16)
    for t in range(N_TIME):
        data[t, :, 3 + t, 4] = -9999
    valid = data != -9999
    scales, offsets = (0.5, 0.5), (-10.0, -10.0)
    units, descriptions = ("degC", "degC"), ("tmin", "tmax")
    paths = []
    for t in range(N_TIME):
        path = folder / f"int16_{t}.tif"
        _write_one(
            path,
            data[t],
            nodata=-9999,
            scales=scales,
            offsets=offsets,
            units=units,
            descriptions=descriptions,
        )
        paths.append(path)
    return CogSet(paths, data, valid, scales, offsets, units, descriptions, -9999)


def _float32(seed: int) -> np.ndarray:
    return _rng(seed).normal(0.0, 50.0, size=(N_TIME, 2, HEIGHT, WIDTH)).astype(np.float32)


def float32_nan(folder: Path) -> CogSet:
    """float32 with negative values and NaN as the declared nodata.

    (3+t, 4) is NaN in both bands; (9, 9+t) is NaN in band 0 only.
    """
    data = _float32(6)
    for t in range(N_TIME):
        data[t, :, 3 + t, 4] = np.nan
        data[t, 0, 9, 9 + t] = np.nan
    valid = ~np.isnan(data)
    scales, offsets = (0.5, 0.5), (1.0, 1.0)
    units, descriptions = ("m", "m"), ("depth", "stage")
    paths = []
    for t in range(N_TIME):
        path = folder / f"float_nan_{t}.tif"
        _write_one(
            path,
            data[t],
            nodata=float("nan"),
            scales=scales,
            offsets=offsets,
            units=units,
            descriptions=descriptions,
        )
        paths.append(path)
    return CogSet(paths, data, valid, scales, offsets, units, descriptions, float("nan"))


def float32_finite_nodata(folder: Path) -> CogSet:
    """float32 with nodata -9999 at (3+t, 4) in both bands and (9, 9+t) in band 0."""
    data = _float32(7)
    for t in range(N_TIME):
        data[t, :, 3 + t, 4] = -9999.0
        data[t, 0, 9, 9 + t] = -9999.0
    valid = data != np.float32(-9999.0)
    paths = []
    for t in range(N_TIME):
        path = folder / f"float_fill_{t}.tif"
        _write_one(path, data[t], nodata=-9999.0)
        paths.append(path)
    return CogSet(paths, data, valid, (1.0, 1.0), (0.0, 0.0), (None, None), (None, None), -9999.0)


def nodata_changes(folder: Path) -> CogSet:
    """uint16, two bands, whose nodata is 0 for the first two timesteps and 7 for the last two.

    (3+t, 4) holds that timestep's nodata in both bands and (9, 9+t) in band 0 only. (5, 6) holds
    7 in the first two timesteps (valid there) and 0 in the last two (valid there).
    """
    data = _uint16(2, seed=8)
    nodata_of = [0, 0, 7, 7]
    for t in range(N_TIME):
        data[t, :, 3 + t, 4] = nodata_of[t]
        data[t, 0, 9, 9 + t] = nodata_of[t]
        data[t, :, 5, 6] = 7 if nodata_of[t] == 0 else 0
    valid = np.stack([data[t] != nodata_of[t] for t in range(N_TIME)])
    paths = []
    for t in range(N_TIME):
        path = folder / f"mixed_{t}.tif"
        _write_one(path, data[t], nodata=nodata_of[t])
        paths.append(path)
    return CogSet(paths, data, valid, (1.0, 1.0), (0.0, 0.0), (None, None), (None, None), 0)


def float32_undeclared_nan(folder: Path) -> CogSet:
    """float32 with NaN at (3+t, 4) but no nodata declared: GDAL calls those pixels valid."""
    data = _float32(9)
    for t in range(N_TIME):
        data[t, :, 3 + t, 4] = np.nan
    paths = []
    for t in range(N_TIME):
        path = folder / f"float_undeclared_{t}.tif"
        _write_one(path, data[t])
        paths.append(path)
    valid = np.ones(data.shape, dtype=bool)
    return CogSet(paths, data, valid, (1.0, 1.0), (0.0, 0.0), (None, None), (None, None), None)


@dataclass(frozen=True)
class ZarrSet:
    """A CF-attributed Zarr source and the truth it encodes. Arrays are (time, band, y, x)."""

    path: Path
    data: np.ndarray
    valid: np.ndarray  # bool per band, from _FillValue, missing_value and the mask variable
    scale: float
    offset: float
    units: str
    bands: tuple[str, ...]

    @property
    def shared_valid(self) -> np.ndarray:
        return np.asarray(self.valid.all(axis=1))


def cf_zarr(
    path: Path,
    *,
    fill: int | None = -9999,
    missing: int | None = None,
    flag_var: bool = False,
    flip_y: bool = False,
) -> ZarrSet:
    """int16 variable `v` with CF `scale_factor`, `add_offset`, `units` and fills.

    `_FillValue` marks (3+t, 4) in both bands, `missing_value` marks (9, 9+t) in band 0, and with
    `flag_var` a variable `ok` (time, y, x; 0 = invalid) marks (11, 11+t). Zarr v2 on disk.
    With `flip_y` the file is south-up; the returned truth is still north-up, as a store reads.
    """
    data = _rng(10).integers(-2000, 2000, size=(N_TIME, 2, HEIGHT, WIDTH)).astype(np.int16)
    valid = np.ones(data.shape, dtype=bool)
    flag = np.ones((N_TIME, HEIGHT, WIDTH), dtype=np.uint8)
    for t in range(N_TIME):
        if fill is not None:
            data[t, :, 3 + t, 4] = fill
            valid[t, :, 3 + t, 4] = False
        if missing is not None:
            data[t, 0, 9, 9 + t] = missing
            valid[t, 0, 9, 9 + t] = False
        if flag_var:
            flag[t, 11, 11 + t] = 0
            valid[t, :, 11, 11 + t] = False
    a, _, c, _, e, f = TRANSFORM
    coords = {
        "time": make_times(N_TIME),
        "band": ["tmin", "tmax"],
        "y": f + e * (np.arange(HEIGHT) + 0.5),
        "x": c + a * (np.arange(WIDTH) + 0.5),
    }
    attrs: dict[str, object] = {
        "crs": CRS,
        "scale_factor": 0.01,
        "add_offset": 1.5,
        "units": "m",
    }
    if fill is not None:
        attrs["_FillValue"] = np.int16(fill)
    if missing is not None:
        attrs["missing_value"] = np.int16(missing)
    ds = xr.DataArray(
        data, dims=("time", "band", "y", "x"), coords=coords, attrs=attrs
    ).to_dataset(name="v")
    if flag_var:
        ds["ok"] = xr.DataArray(flag, dims=("time", "y", "x"))
    if flip_y:
        ds = ds.isel(y=slice(None, None, -1))
    ds.to_zarr(path, zarr_format=2, consolidated=True)
    return ZarrSet(path, data, valid, 0.01, 1.5, "m", ("tmin", "tmax"))
