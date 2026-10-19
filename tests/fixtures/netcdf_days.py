"""NetCDF files shaped like NOAA's OISST daily files, for the tests of `convert` on NetCDF input.

Each file is int16 packed with a scale_factor and a _FillValue, on a grid whose latitudes ascend,
with a length-1 `zlev` dimension. `day_values(day)` is what a file stores for a day.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import xarray as xr

HEIGHT, WIDTH = 6, 8
FILL = -999


def day_values(day: int, width: int = WIDTH) -> np.ndarray:
    """Packed int16 values for one day: a ramp that differs per day, with a fill at the corner."""
    ramp = (np.arange(HEIGHT * width).reshape(HEIGHT, width) * 7 + day * 131) % 3000 - 200
    ramp[0, 0] = FILL
    return ramp.astype(np.int16)


def day_ok(day: int, width: int = WIDTH) -> np.ndarray:
    """A validity plane for `day` (1 = valid): the fill, and a pixel that moves from day to day."""
    ok = (day_values(day, width) != FILL).astype(np.uint8)
    ok[1, day % width] = 0
    return ok


def lat() -> np.ndarray:
    return 10.0 + 0.25 * (np.arange(HEIGHT) + 0.5)


def lon(lon0: float = 100.125, width: int = WIDTH) -> np.ndarray:
    return lon0 + 0.25 * np.arange(width)


def write_days(
    path: Path,
    days: list[int],
    *,
    variable: str = "sst",
    zlev: bool = True,
    scale: float = 0.01,
    units: str = "Celsius",
    lon0: float = 100.125,
    fill: int | None = FILL,
    start: str = "2020-01-01",
    calendar: str | None = None,
    width: int = WIDTH,
    dtype: str = "int16",
    also: tuple[str, ...] = (),
    ok: bool = False,
) -> Path:
    """One OISST-like NetCDF file with a step for each of `days`, offsets from `start` at 12:00.

    It has a length-1 `zlev` dimension unless `zlev` is False, the extra variables `also`, and
    the validity variable `ok` (`day_ok` per step) when `ok` is True.
    """
    time_attrs: dict[str, Any] = {"units": f"days since {start} 12:00:00"}
    if calendar is not None:
        time_attrs["calendar"] = calendar
    values = np.stack([day_values(d, width) for d in days]).astype(dtype)
    dims = ("time", "lat", "lon")
    coords: dict[str, Any] = {
        "time": ("time", [float(d) for d in days], time_attrs),
        "lat": ("lat", lat()),
        "lon": ("lon", lon(lon0, width)),
    }
    if zlev:
        values = values[:, np.newaxis]
        dims = ("time", "zlev", "lat", "lon")
        coords["zlev"] = ("zlev", [0.0], {"units": "meters", "long_name": "Sea surface height"})
    attrs = {"scale_factor": scale, "add_offset": 0.0, "units": units}
    names = (variable, *also)
    variables = {name: (dims, values, attrs) for name in names}
    if ok:
        planes = np.stack([day_ok(d, width) for d in days])
        variables["ok"] = (dims, planes[:, np.newaxis] if zlev else planes, {})
    ds = xr.Dataset(variables, coords=coords)
    encoding: dict[str, dict[str, Any]] = {}
    if fill is not None:
        encoding = {name: {"_FillValue": fill} for name in names}
    if ok:
        encoding["ok"] = {"_FillValue": None}
    ds.to_netcdf(path, engine="h5netcdf", encoding=encoding)
    return path


def write_series(folder: Path, days: list[int], **kwargs: Any) -> list[Path]:
    """One file per day in `folder`, named by the day number so that names sort by time."""
    folder.mkdir(parents=True, exist_ok=True)
    return [write_days(folder / f"oisst.2020{d:04d}.nc", [d], **kwargs) for d in days]
