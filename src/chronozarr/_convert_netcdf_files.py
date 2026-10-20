"""Find the NetCDF files of a time series in a directory or glob.

NOAA, NASA and ESGF deliver a series as one file per day, month or decade. `convert` reads such a
folder or glob as one source; each file is dated by the time coordinate inside it, so nothing is
read from the file names. Raster discovery (`_convert_discover`) does not list NetCDF files.
"""

from __future__ import annotations

import glob
from pathlib import Path

from chronozarr._convert_discover import GLOB_CHARS, RASTER_SUFFIXES, ZIP_SUFFIX
from chronozarr._convert_xarray import NETCDF_SUFFIXES


def find_netcdf_files(text: str) -> list[str] | None:
    """The NetCDF files that `text` names as one series, as sorted absolute paths; None when it
    names something else.

    `text` is a directory (not searched recursively) or a glob (`dir/**/*.nc` recurses) with
    NetCDF files (`NETCDF_SUFFIXES`) and no raster files, which would make it a raster series.
    One file, a Zarr store, a manifest, a URL, an S3 prefix and a path that does not exist are
    other kinds of source: None. A glob that ends in a NetCDF suffix and matches nothing raises
    ValueError.
    """
    lowered = text.lower()
    if "://" in text or lowered.endswith((".csv", ".json", ".zarr")):
        return None
    path = Path(text).expanduser()
    if path.is_dir():
        found = [p for p in path.iterdir() if p.is_file()]
    elif path.is_file() or not any(ch in text for ch in GLOB_CHARS):
        return None
    else:
        found = [Path(m) for m in glob.glob(str(path), recursive=True) if Path(m).is_file()]
    netcdf = sorted(str(p.resolve()) for p in found if p.suffix.lower() in NETCDF_SUFFIXES)
    has_rasters = any(p.suffix.lower() in (*RASTER_SUFFIXES, ZIP_SUFFIX) for p in found)
    if netcdf and not has_rasters:
        return netcdf
    if not found and lowered.endswith(NETCDF_SUFFIXES):
        raise ValueError(
            f"no NetCDF files found matching '{text}'. Check the path; quote the glob so the "
            "shell does not expand it, and use 'dir/**/*.nc' to search subdirectories"
        )
    return None
