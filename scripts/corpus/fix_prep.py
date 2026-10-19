"""Prepare the inputs of the fix runs: what a newcomer would do after reading the refusal.

Each function writes into <CORPUS>/<id>/fixed/ and never touches raw/ (`fixed2/` and `fixed3/`
for the `ID|2` and `ID|3` steps). Run one with `python -I fix_prep.py ID` in the fetch venv (it
reads downloads: use -I). The fix that converts each prepared input is recorded in fixes.json
next to this file, and the minutes it costs a newcomer in classes.json.
"""

from __future__ import annotations

import csv
import datetime as dt
import os
import re
import sys
from pathlib import Path

CORPUS = Path(
    os.environ.get("CHRONOZARR_CORPUS", Path(__file__).resolve().parents[2] / "data" / "corpus")
)
PREP = {}


def prep(id: str):
    def deco(fn):
        PREP[id] = fn
        return fn

    return deco


def julian_date(year: int, doy: int) -> str:
    return (dt.date(year, 1, 1) + dt.timedelta(days=doy - 1)).isoformat()


def search(pattern: str, text: str) -> re.Match[str]:
    """`re.search` for names the corpus fixes: raise with both strings when nothing matches."""
    m = re.search(pattern, text)
    if not m:
        raise ValueError(f"{text} does not match {pattern}")
    return m


def write_csv(path: Path, rows: list[tuple], header: tuple) -> None:
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)


@prep("worldclim-tmax-monthly")
def worldclim(raw: Path, fixed: Path) -> None:
    rows = []
    for p in sorted(raw.glob("*.tif")):
        year, month = search(r"_(\d{4})-(\d{2})\.tif$", p.name).groups()
        rows.append((str(p), f"{year}-{month}-01", "tmax"))
    write_csv(fixed / "manifest.csv", rows, ("uri", "datetime", "bands"))


def warp_to_4326(
    src_path: Path,
    dst_path: Path,
    nodata=None,
    dtype: str | None = None,
    scale: float | None = None,
) -> None:
    """What `gdalwarp -t_srs EPSG:4326 -r near` does: the newcomer's reprojection of a file."""
    import rasterio
    from rasterio.warp import Resampling, calculate_default_transform, reproject

    with rasterio.open(src_path) as src:
        transform, width, height = calculate_default_transform(
            src.crs, "EPSG:4326", src.width, src.height, *src.bounds
        )
        profile = src.profile | {
            "crs": "EPSG:4326",
            "transform": transform,
            "width": width,
            "height": height,
            "compress": "deflate",
        }
        nd = src.nodata if src.nodata is not None else nodata
        if dtype:
            profile["dtype"] = dtype
        if nd is not None:
            profile["nodata"] = nd
        with rasterio.open(dst_path, "w", **profile) as dst:
            for b in range(1, src.count + 1):
                reproject(
                    rasterio.band(src, b),
                    rasterio.band(dst, b),
                    resampling=Resampling.nearest,
                    src_nodata=nd,
                    dst_nodata=nd,
                )
            for b in range(1, src.count + 1):
                dst.set_band_description(b, src.descriptions[b - 1])
            dst.scales = (scale,) * src.count if scale is not None else src.scales
            dst.offsets = src.offsets


@prep("modis-13a1-pc-cog-julian")
def modis13a1(raw: Path, fixed: Path) -> None:
    rows = []
    for p in sorted(raw.glob("*.tif")):
        year, doy = search(r"\.A(\d{4})(\d{3})\.", p.name).groups()
        out = fixed / p.name
        warp_to_4326(p, out)
        rows.append((str(out), julian_date(int(year), int(doy))))
    write_csv(fixed / "manifest.csv", rows, ("uri", "datetime"))


@prep("hls-s30-julian")
def hls(raw: Path, fixed: Path) -> None:
    """Re-tag the files whose CRS has no EPSG code (gdal_edit.py -a_srs EPSG:32617), then date
    them in a manifest."""
    import rasterio
    import rasterio.shutil  # ty: ignore[unresolved-import]  # compiled module, no stub

    rows = []
    for p in sorted(raw.glob("*.tif")):
        year, doy, hms = search(r"\.(\d{4})(\d{3})T(\d{6})\.", p.name).groups()
        out = fixed / p.name
        rasterio.shutil.copy(p, out, driver="GTiff", compress="deflate")
        with rasterio.open(out, "r+") as dst:
            if dst.crs.to_epsg() is None:
                dst.crs = "EPSG:32617"
        rows.append(
            (str(out), julian_date(int(year), int(doy)) + f"T{hms[:2]}:{hms[2:4]}:{hms[4:]}Z")
        )
    write_csv(fixed / "manifest.csv", rows, ("uri", "datetime"))


@prep("prism-daily-bil-zip")
def prism(raw: Path, fixed: Path) -> None:
    import rasterio.shutil  # ty: ignore[unresolved-import]  # compiled module, no stub

    for p in sorted((raw / "unzipped").glob("*.bil")):
        rasterio.shutil.copy(p, fixed / (p.stem + ".tif"), driver="GTiff", compress="deflate")


@prep("hrrr-t2m-grib2")
def hrrr(raw: Path, fixed: Path) -> None:
    """GRIB2 to GeoTIFF (gdal_translate), then to EPSG:4326 (gdalwarp): the HRRR Lambert grid has
    no EPSG code."""
    import rasterio.shutil  # ty: ignore[unresolved-import]  # compiled module, no stub

    tmp = fixed / "_tif"
    tmp.mkdir(exist_ok=True)
    for p in sorted(raw.rglob("*.grib2")):
        date = search(r"hrrr\.(\d{8})", str(p)).group(1)
        rasterio.shutil.copy(p, tmp / f"hrrr_t2m_{date}.tif", driver="GTiff", compress="deflate")
    for p in sorted(tmp.glob("*.tif")):
        warp_to_4326(p, fixed / p.name, dtype="float32")


@prep("daymet-lcc-nc")
def daymet(raw: Path, fixed: Path) -> None:
    """Warp each Daymet day from its Lambert conformal grid to EPSG:4326 GeoTIFFs named by date
    (no EPSG code exists for it)."""
    import numpy as np
    import rasterio
    import xarray as xr
    from rasterio.transform import Affine
    from rasterio.warp import Resampling, calculate_default_transform, reproject

    lcc = (
        "+proj=lcc +lat_1=25 +lat_2=60 +lat_0=42.5 +lon_0=-100 +x_0=0 +y_0=0 +ellps=WGS84 +units=m"
    )
    ds = xr.open_dataset(
        raw / "daymet_v4_daily_na_tmax_2020.nc",
        decode_times=xr.coders.CFDatetimeCoder(use_cftime=True),
    )
    x, y = ds["x"].values, ds["y"].values
    dx, dy = float(x[1] - x[0]), float(y[1] - y[0])
    src_tf = Affine(dx, 0, x[0] - dx / 2, 0, dy, y[0] - dy / 2)
    h, w = len(y), len(x)
    bounds = rasterio.transform.array_bounds(h, w, src_tf)
    dst_tf, dw, dh = calculate_default_transform(lcc, "EPSG:4326", w, h, *bounds)
    for i, t in enumerate(ds["time"].values):
        out = np.full((dh, dw), np.nan, dtype="float32")
        reproject(
            ds["tmax"].isel(time=i).values.astype("float32"),
            out,
            src_transform=src_tf,
            src_crs=lcc,
            dst_transform=dst_tf,
            dst_crs="EPSG:4326",
            resampling=Resampling.nearest,
            src_nodata=np.nan,
            dst_nodata=np.nan,
        )
        name = f"daymet_tmax_{t.year:04d}{t.month:02d}{t.day:02d}.tif"
        with rasterio.open(
            fixed / name,
            "w",
            driver="GTiff",
            height=dh,
            width=dw,
            count=1,
            dtype="float32",
            crs="EPSG:4326",
            transform=dst_tf,
            compress="deflate",
        ) as dst:
            dst.write(out, 1)


@prep("gee-multiband-dates")
def gee_multiband(raw: Path, fixed: Path) -> None:
    """Split the 23 date bands into one file each (gdal_translate -b N), then warp to EPSG:4326."""
    import rasterio

    tmp = fixed / "_split"
    tmp.mkdir(exist_ok=True)
    with rasterio.open(raw / "modis_ndvi_2020.tif") as src:
        profile = src.profile | {"count": 1, "compress": "deflate"}
        for i, desc in enumerate(src.descriptions, 1):
            with rasterio.open(tmp / f"ndvi_{desc[:10]}.tif", "w", **profile) as dst:
                dst.write(src.read(i), 1)
                dst.set_band_description(1, "NDVI")
    for p in sorted(tmp.glob("*.tif")):
        warp_to_4326(p, fixed / p.name, nodata=-3000)


@prep("gee-tiled-per-date")
def gee_tiled(raw: Path, fixed: Path) -> None:
    """Mosaic the 4 tiles of each date (gdal_merge.py), then warp to EPSG:4326."""
    import rasterio
    from rasterio.merge import merge

    tmp = fixed / "_mosaic"
    tmp.mkdir(exist_ok=True)
    by_date: dict[str, list[Path]] = {}
    for p in sorted(raw.glob("*.tif")):
        by_date.setdefault(search(r"NDVI_(\d{4}_\d{2}_\d{2})-", p.name).group(1), []).append(p)
    for date, paths in by_date.items():
        srcs = [rasterio.open(p) for p in paths]
        mosaic, transform = merge(srcs)
        profile = srcs[0].profile | {
            "height": mosaic.shape[1],
            "width": mosaic.shape[2],
            "transform": transform,
            "compress": "deflate",
        }
        for s_ in srcs:
            s_.close()
        with rasterio.open(tmp / f"ndvi_{date}.tif", "w", **profile) as dst:
            dst.write(mosaic)
    for p in sorted(tmp.glob("*.tif")):
        warp_to_4326(p, fixed / p.name, nodata=-3000)


@prep("oisst-ncei-daily-files")
def oisst_ncei(raw: Path, fixed: Path) -> None:
    import xarray as xr

    files = sorted(raw.rglob("*.nc"))
    ds = xr.open_mfdataset(files, combine="nested", concat_dim="time").squeeze("zlev", drop=True)
    ds.to_netcdf(fixed / "oisst_2020-01_02.nc")


@prep("modis-13a1-pc-cog-julian|2")
def modis13a1_scale(raw: Path, fixed: Path) -> None:
    """The MODIS files say scale 10000 (the HDF convention is divide): re-tag 0.0001
    (gdal_edit.py -scale 0.0001)."""
    import rasterio
    import rasterio.shutil  # ty: ignore[unresolved-import]  # compiled module, no stub

    rows = []
    for p in sorted(
        (fixed.parent / "fixed").glob("*.tif")
    ):  # the files already warped to EPSG:4326
        year, doy = search(r"\.A(\d{4})(\d{3})\.", p.name).groups()
        out = fixed / p.name
        rasterio.shutil.copy(p, out, driver="GTiff", compress="deflate")
        with rasterio.open(out, "r+") as dst:
            dst.scales = (0.0001,)
        rows.append((str(out), julian_date(int(year), int(doy))))
    write_csv(fixed / "manifest.csv", rows, ("uri", "datetime"))


def roll_lon(ds):
    """The usual two lines for 0..360 longitudes: wrap to -180..180 and sort."""
    rolled = ds.assign_coords(lon=(((ds.lon + 180) % 360) - 180)).sortby("lon")
    for var in rolled.data_vars.values():
        var.encoding.pop("missing_value", None)
    return rolled


@prep("oisst-psl-daily|2")
def oisst_psl_roll(raw: Path, fixed: Path) -> None:
    import xarray as xr

    roll_lon(xr.open_dataset(raw / "sst.day.mean.2020.nc")).to_netcdf(fixed / "sst_lon180.nc")


@prep("oisst-ncei-daily-files|2")
def oisst_ncei_roll(raw: Path, fixed: Path) -> None:
    import xarray as xr

    ds = xr.open_mfdataset(sorted(raw.rglob("*.nc")), combine="nested", concat_dim="time").squeeze(
        "zlev", drop=True
    )
    roll_lon(ds).to_netcdf(fixed / "oisst_lon180.nc")


@prep("cmip6-hadgem3-360day-nc|2")
def cmip6_hadgem_roll(raw: Path, fixed: Path) -> None:
    import xarray as xr

    ds = xr.open_dataset(
        next(raw.glob("*.nc")), decode_times=xr.coders.CFDatetimeCoder(use_cftime=True)
    )
    roll_lon(ds).to_netcdf(fixed / "tas_lon180.nc")


@prep("cmip6-cesm2-noleap-zarr|2")
def cmip6_cesm2_roll(raw: Path, fixed: Path) -> None:
    import xarray as xr

    ds = xr.open_zarr(
        next(raw.glob("*.zarr")),
        decode_times=xr.coders.CFDatetimeCoder(use_cftime=True),
        chunks=None,
    )
    rolled = roll_lon(ds).load()
    for var in rolled.variables.values():
        for key in (
            "chunks",
            "preferred_chunks",
            "compressors",
            "filters",
            "serializer",
            "shards",
        ):
            var.encoding.pop(key, None)
    rolled.to_zarr(fixed / "tas_lon180.zarr", mode="w")


@prep("era5-fiji-antimeridian-nc|2")
def era5_reproject(raw: Path, fixed: Path) -> None:
    """Rolling 165..195 to -180..180 would split Fiji in two; warp each step to EPSG:3832
    (PDC Mercator, centred on 150 E) instead."""
    import numpy as np
    import rasterio
    import xarray as xr
    from rasterio.transform import from_origin
    from rasterio.warp import Resampling, calculate_default_transform, reproject

    ds = xr.open_dataset(raw / "era5_fiji_2020-01.nc")
    lon, lat = ds["longitude"].values, ds["latitude"].values
    res = 0.25
    src_tf = from_origin(lon[0] - res / 2, lat[0] + res / 2, res, res)
    h, w = len(lat), len(lon)
    dst_tf, dw, dh = calculate_default_transform(
        "EPSG:4326",
        "EPSG:3832",
        w,
        h,
        lon[0] - res / 2,
        lat[-1] - res / 2,
        lon[-1] + res / 2,
        lat[0] + res / 2,
    )
    for i, t in enumerate(ds["valid_time"].values):
        out = np.full((dh, dw), np.nan, dtype="float32")
        reproject(
            ds["t2m"].isel(valid_time=i).values.astype("float32"),
            out,
            src_transform=src_tf,
            src_crs="EPSG:4326",
            dst_transform=dst_tf,
            dst_crs="EPSG:3832",
            resampling=Resampling.nearest,
            src_nodata=np.nan,
            dst_nodata=np.nan,
        )
        stamp = (
            np.datetime_as_string(t, unit="m").replace("-", "").replace(":", "").replace("T", "_")
        )
        with rasterio.open(
            fixed / f"era5_t2m_{stamp}.tif",
            "w",
            driver="GTiff",
            height=dh,
            width=dw,
            count=1,
            dtype="float32",
            crs="EPSG:3832",
            transform=dst_tf,
            compress="deflate",
        ) as dst:
            dst.write(out, 1)


def to_gregorian(ds):
    """Calendar fix for CMIP6 files: label every step with the same Y-M-D on the standard
    calendar."""
    out = ds.convert_calendar("standard", align_on="date", use_cftime=False)
    before = [(t.year, t.month, t.day) for t in ds["time"].values]
    after = [(t.year, t.month, t.day) for t in out["time"].to_index()]
    if before != after:
        raise SystemExit("calendar conversion changed a date")
    return out


@prep("cmip6-hadgem3-360day-nc|3")
def cmip6_hadgem_gregorian(raw: Path, fixed: Path) -> None:
    import xarray as xr

    ds = xr.open_dataset(
        next(raw.glob("*.nc")), decode_times=xr.coders.CFDatetimeCoder(use_cftime=True)
    )
    ds = ds.drop_vars(["time_bnds"])
    roll_lon(to_gregorian(ds)).to_netcdf(fixed / "tas_lon180.nc")


@prep("cmip6-cesm2-noleap-zarr|3")
def cmip6_cesm2_gregorian(raw: Path, fixed: Path) -> None:
    import xarray as xr

    ds = xr.open_zarr(
        next(raw.glob("*.zarr")),
        decode_times=xr.coders.CFDatetimeCoder(use_cftime=True),
        chunks=None,
    )
    ds = ds.drop_vars(["time_bnds"]).load()
    rolled = roll_lon(to_gregorian(ds))
    for var in rolled.variables.values():
        for key in (
            "chunks",
            "preferred_chunks",
            "compressors",
            "filters",
            "serializer",
            "shards",
        ):
            var.encoding.pop(key, None)
    rolled.to_zarr(fixed / "tas_lon180.zarr", mode="w")


def main(ids: list[str]) -> int:
    for id in ids or list(PREP):
        base, _, stage = id.partition("|")
        fixed = CORPUS / base / {"": "fixed", "2": "fixed2", "3": "fixed3"}[stage]
        fixed.mkdir(parents=True, exist_ok=True)
        PREP[id](CORPUS / base / "raw", fixed)
        print(f"{id}: wrote {len(list(fixed.iterdir()))} files to {fixed}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
