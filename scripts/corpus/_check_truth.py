"""Compare a store dump (from _dump_store.py) with the source files, read independently.

Run as `python -I _check_truth.py ID RAW_DIR DUMP.json OUT.json` in a venv with rasterio, xarray,
netCDF4, zarr, cftime, pyproj. It never imports chronozarr: the sources are read with rasterio or
xarray, at the pixels the dump sampled, located by world coordinates in the store's CRS.

Checks: date count and order against the dates the scientist knows (file names or the time axis),
CRS equality, and the physical values at three dates times up to three pixels (plus pixels the
source marks invalid, which the store must show as NaN).
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pyproj
import rasterio
import rasterio.transform
import xarray as xr


def day_string(t) -> str:
    """YYYY-MM-DD for datetime64, cftime and datetime objects; adds THH:MM when not midnight."""
    if isinstance(t, np.datetime64):
        t = datetime.fromisoformat(str(t.astype("datetime64[s]")))
    text = f"{t.year:04d}-{t.month:02d}-{t.day:02d}"
    if (t.hour, t.minute) != (0, 0):
        text += f"T{t.hour:02d}:{t.minute:02d}"
    return text


def julian(year: int, doy: int) -> str:
    return (datetime(year, 1, 1) + timedelta(days=doy - 1)).strftime("%Y-%m-%d")


def search(pattern: str, text: str) -> re.Match[str]:
    """`re.search` for names the corpus fixes: raise with both strings when nothing matches."""
    m = re.search(pattern, text)
    if not m:
        raise ValueError(f"{text} does not match {pattern}")
    return m


def to_source_xy(store_crs: pyproj.CRS, src_crs: pyproj.CRS, xs: np.ndarray, ys: np.ndarray):
    if store_crs.equals(src_crs, ignore_axis_order=True):
        return xs, ys
    tr = pyproj.Transformer.from_crs(store_crs, src_crs, always_xy=True)
    return tr.transform(xs, ys)


# --- truth models --------------------------------------------------------------------------------


@dataclass
class Frame:
    date: str
    parts: list = field(default_factory=list)  # [(path, [1-based band indexes])]


class RasterTruth:
    """Frames made of raster files read with rasterio (GeoTIFF, BIL, GRIB2)."""

    kind = "raster"

    def __init__(self, frames: list[Frame], extra_nodata: float | None = None):
        self.extra_nodata = extra_nodata
        self.frames = frames
        self.dates = [f.date for f in frames]
        with rasterio.open(frames[0].parts[0][0]) as src:
            self.crs = pyproj.CRS.from_user_input(src.crs.to_wkt())
        self.n_bands = len(frames[0].parts[0][1])

    def sample(self, i: int, xs: np.ndarray, ys: np.ndarray, store_crs: pyproj.CRS) -> np.ndarray:
        out = np.full((self.n_bands, len(xs)), np.nan)
        done = np.zeros(len(xs), dtype=bool)
        for path, idxs in self.frames[i].parts:
            with rasterio.open(path) as src:
                px, py = to_source_xy(
                    store_crs, pyproj.CRS.from_user_input(src.crs.to_wkt()), xs, ys
                )
                rows, cols = rasterio.transform.rowcol(src.transform, px, py)
                rows, cols = np.asarray(rows), np.asarray(cols)
                inside = (
                    (rows >= 0) & (rows < src.height) & (cols >= 0) & (cols < src.width) & ~done
                )
                if not inside.any():
                    continue
                data = src.read(idxs)[:, rows[inside], cols[inside]].astype("float64")
                nodata = src.nodata
                for k, b in enumerate(idxs):
                    if nodata is not None and not np.isnan(nodata):
                        data[k][data[k] == nodata] = np.nan
                    if self.extra_nodata is not None:
                        data[k][data[k] == self.extra_nodata] = np.nan
                    scale, offset = src.scales[b - 1], src.offsets[b - 1]
                    data[k] = data[k] * scale + offset
                out[:, inside] = data
                done |= inside
        return out


class GridTruth:
    """A (time, y, x) variable of a NetCDF file or Zarr store, located by its coordinate arrays."""

    kind = "grid"

    def __init__(
        self,
        ds: xr.Dataset,
        var: str,
        x: str,
        y: str,
        t: str,
        crs: str,
        dates: list[str] | None = None,
    ):
        self.da = ds[var]
        self.xname, self.yname, self.tname = x, y, t
        self.crs = pyproj.CRS.from_user_input(crs)
        self.geographic = self.crs.is_geographic
        self.dates = dates or [day_string(v) for v in ds[t].values]
        self.xc = np.asarray(ds[x].values, dtype="float64")
        self.yc = np.asarray(ds[y].values, dtype="float64")
        self.n_bands = 1

    def sample(self, i: int, xs: np.ndarray, ys: np.ndarray, store_crs: pyproj.CRS) -> np.ndarray:
        px, py = to_source_xy(store_crs, self.crs, xs, ys)
        if self.geographic:
            dx = (self.xc[None, :] - np.asarray(px)[:, None] + 180.0) % 360.0 - 180.0
            ix = np.abs(dx).argmin(axis=1)
            ex = np.abs(dx[np.arange(len(ix)), ix])
        else:
            ix = np.abs(self.xc[None, :] - np.asarray(px)[:, None]).argmin(axis=1)
            ex = np.abs(self.xc[ix] - np.asarray(px))
        iy = np.abs(self.yc[None, :] - np.asarray(py)[:, None]).argmin(axis=1)
        ey = np.abs(self.yc[iy] - np.asarray(py))
        cell_x = abs(float(np.median(np.diff(self.xc)))) if len(self.xc) > 1 else np.inf
        cell_y = abs(float(np.median(np.diff(self.yc)))) if len(self.yc) > 1 else np.inf
        plane = np.asarray(self.da.isel({self.tname: i}).squeeze().values, dtype="float64")
        vals = plane[iy, ix]
        vals[(ex > 0.51 * cell_x) | (ey > 0.51 * cell_y)] = np.nan
        return vals[None, :]


class FilesGridTruth:
    """One NetCDF file per timestep, each holding a (1, ..., y, x) variable."""

    kind = "grid-files"

    def __init__(self, paths: list[Path], var: str, x: str, y: str, t: str, crs: str):
        self.paths, self.var, self.xname, self.yname, self.tname, self.crs_text = (
            paths,
            var,
            x,
            y,
            t,
            crs,
        )
        self.crs = pyproj.CRS.from_user_input(crs)
        self.n_bands = 1
        self.dates = []
        for p in paths:
            with xr.open_dataset(p) as ds:
                self.dates.append(day_string(ds[t].values.ravel()[0]))

    def sample(self, i, xs, ys, store_crs):
        with xr.open_dataset(self.paths[i]) as ds:
            ds = ds.load()
        inner = GridTruth(
            ds,
            self.var,
            self.xname,
            self.yname,
            self.tname,
            self.crs_text,
            dates=["x"] * ds.sizes[self.tname],
        )
        return inner.sample(0, xs, ys, store_crs)


# --- per-dataset truth builders ------------------------------------------------------------------


def frames_from(files: list[Path], date_of, bands: list[int] | None = None) -> list[Frame]:
    frames = []
    for p in sorted(files, key=lambda q: (date_of(q), q.name)):
        with rasterio.open(p) as src:
            idxs = bands or list(range(1, src.count + 1))
        frames.append(Frame(date_of(p), [(p, idxs)]))
    return frames


def match_date(pattern: str, fmt: str):
    rx = re.compile(pattern)

    def date_of(p: Path) -> str:
        m = rx.search(p.name)
        if not m:
            raise ValueError(f"{p.name} does not match {pattern}")
        return datetime.strptime("".join(m.groups()), fmt).strftime("%Y-%m-%d")

    return date_of


# Physical value ranges a scientist expects after scale and offset; a store value outside is wrong
# even when it equals what the file's own tags give (undeclared -9999, a MODIS scale of 10000).
EXPECTED = {
    "chirps-africa-monthly": (0.0, 3000.0),
    "worldclim-tmax-monthly": (-60.0, 60.0),
    "prism-daily-bil-zip": (0.0, 1000.0),
    "modis-13a1-pc-cog-julian": (-1.0, 1.0),
    "landsat-ndvi-derived-nan": (-1.0, 1.0),
    "gee-multiband-dates": (-2000.0, 10000.0),
    "gee-tiled-per-date": (-2000.0, 10000.0),
    "hls-s30-julian": (-0.1, 1.6),
    "oisst-psl-daily": (-3.0, 40.0),
    "oisst-ncei-daily-files": (-3.0, 40.0),
    "cmip6-hadgem3-360day-nc": (180.0, 340.0),
    "cmip6-cesm2-noleap-zarr": (180.0, 340.0),
    "era5-fiji-antimeridian-nc": (200.0, 330.0),
    "daymet-lcc-nc": (-60.0, 60.0),
    "hrrr-t2m-grib2": (-80.0, 60.0),  # the GDAL GRIB driver reports degrees C
}


def build_fixed(id: str, fixed: Path):
    """Truth for a fix run: the input is the prepared copy in `fixed/`, as chronozarr got it."""
    ymd = "%Y%m%d"
    if id in (
        "modis-13a1-pc-cog-julian",
        "hls-s30-julian",
    ):  # fixed and fixed2 hold the same file names
        return RasterTruth(
            frames_from(
                list(fixed.glob("*.tif")),
                lambda p: julian(*map(int, search(r"\.A?(\d{4})(\d{3})[.T]", p.name).groups())),
            )
        )
    if id == "prism-daily-bil-zip":
        return RasterTruth(
            frames_from(list(fixed.glob("*.tif")), match_date(r"_(\d{8})\.tif$", ymd))
        )
    if id in ("hrrr-t2m-grib2", "daymet-lcc-nc"):
        return RasterTruth(
            frames_from(list(fixed.glob("*.tif")), match_date(r"_(\d{8})\.tif$", ymd))
        )
    if id in ("gee-multiband-dates", "gee-tiled-per-date"):
        return RasterTruth(
            frames_from(
                list(fixed.glob("*.tif")), match_date(r"ndvi_(\d{4})_(\d{2})_(\d{2})\.tif$", ymd)
            )
        )
    if id == "oisst-ncei-daily-files":
        name = "oisst_lon180.nc" if fixed.name == "fixed2" else "oisst_2020-01_02.nc"
        return GridTruth(xr.open_dataset(fixed / name), "sst", "lon", "lat", "time", "EPSG:4326")
    if id == "oisst-psl-daily":
        return GridTruth(
            xr.open_dataset(fixed / "sst_lon180.nc"), "sst", "lon", "lat", "time", "EPSG:4326"
        )
    if id == "cmip6-hadgem3-360day-nc":
        ds = xr.open_dataset(
            fixed / "tas_lon180.nc", decode_times=xr.coders.CFDatetimeCoder(use_cftime=True)
        )
        return GridTruth(ds, "tas", "lon", "lat", "time", "EPSG:4326")
    if id == "cmip6-cesm2-noleap-zarr":
        ds = xr.open_zarr(
            fixed / "tas_lon180.zarr",
            decode_times=xr.coders.CFDatetimeCoder(use_cftime=True),
            chunks=None,
        )
        return GridTruth(ds, "tas", "lon", "lat", "time", "EPSG:4326")
    if id == "era5-fiji-antimeridian-nc":
        return RasterTruth(
            frames_from(
                list(fixed.glob("*.tif")),
                lambda p: datetime.strptime(
                    search(r"_(\d{8}_\d{4})\.tif", p.name).group(1), "%Y%m%d_%H%M"
                ).strftime("%Y-%m-%dT%H:%M"),
            )
        )
    raise ValueError(f"no fixed-input truth for {id}")


def build(id: str, raw: Path):
    if raw.name in ("fixed", "fixed2", "fixed3"):
        return build_fixed(id, raw)
    if id == "chirps-africa-monthly":
        return RasterTruth(
            frames_from(list(raw.glob("*.tif")), match_date(r"\.(\d{4})\.(\d{2})\.tif", "%Y%m"))
        )
    if id == "worldclim-tmax-monthly":
        return RasterTruth(
            frames_from(list(raw.glob("*.tif")), match_date(r"_(\d{4})-(\d{2})\.tif", "%Y%m"))
        )
    if id == "nsidc-seaice-n-daily":
        return RasterTruth(
            frames_from(list(raw.rglob("*.tif")), match_date(r"N_(\d{8})_", "%Y%m%d"))
        )
    if id == "nsidc-seaice-s-monthly":
        return RasterTruth(
            frames_from(list(raw.rglob("*.tif")), match_date(r"S_(\d{6})_", "%Y%m"))
        )
    if id == "prism-daily-bil-zip":
        return RasterTruth(
            frames_from(
                list((raw / "unzipped").glob("*.bil")), match_date(r"_(\d{8})\.bil$", "%Y%m%d")
            )
        )
    if id == "modis-13a1-pc-cog-julian":
        return RasterTruth(
            frames_from(
                list(raw.glob("*.tif")),
                lambda p: julian(*map(int, search(r"\.A(\d{4})(\d{3})\.", p.name).groups())),
            )
        )
    if id == "landsat-c2-productid":
        return RasterTruth(
            frames_from(list(raw.glob("*.TIF")), match_date(r"_\d{6}_(\d{8})_\d{8}_02", "%Y%m%d"))
        )
    if id == "landsat-ndvi-derived-nan":
        return RasterTruth(
            frames_from(list(raw.glob("*.tif")), match_date(r"ndvi_(\d{8})", "%Y%m%d"))
        )
    if id == "hls-s30-julian":
        return RasterTruth(
            frames_from(
                list(raw.glob("*.tif")),
                lambda p: julian(*map(int, search(r"\.(\d{4})(\d{3})T", p.name).groups())),
            )
        )
    if id == "gee-tiled-per-date":
        by_date: dict[str, list[Path]] = {}
        for p in raw.glob("*.tif"):
            d = match_date(r"NDVI_(\d{4})_(\d{2})_(\d{2})-", "%Y%m%d")(p)
            by_date.setdefault(d, []).append(p)
        return RasterTruth(
            [Frame(d, [(p, [1]) for p in sorted(ps)]) for d, ps in sorted(by_date.items())]
        )
    if id == "gee-multiband-dates":
        path = raw / "modis_ndvi_2020.tif"
        with rasterio.open(path) as src:
            descs = list(src.descriptions)
        frames = [
            Frame(
                datetime.strptime(
                    "".join(search(r"^(\d{4})_(\d{2})_(\d{2})_NDVI", d).groups()), "%Y%m%d"
                ).strftime("%Y-%m-%d"),
                [(path, [i])],
            )
            for i, d in enumerate(descs, 1)
        ]
        return RasterTruth(frames)
    if id == "hrrr-t2m-grib2":
        files = list(raw.rglob("*.grib2"))
        return RasterTruth(
            frames_from(
                files,
                lambda p: datetime.strptime(
                    search(r"hrrr\.(\d{8})", str(p)).group(1), "%Y%m%d"
                ).strftime("%Y-%m-%d"),
            )
        )
    if id == "oisst-psl-daily":
        return GridTruth(
            xr.open_dataset(raw / "sst.day.mean.2020.nc"), "sst", "lon", "lat", "time", "EPSG:4326"
        )
    if id == "oisst-ncei-daily-files":
        return FilesGridTruth(sorted(raw.rglob("*.nc")), "sst", "lon", "lat", "time", "EPSG:4326")
    if id == "cmip6-hadgem3-360day-nc":
        ds = xr.open_dataset(
            next(raw.glob("*.nc")), decode_times=xr.coders.CFDatetimeCoder(use_cftime=True)
        )
        return GridTruth(ds, "tas", "lon", "lat", "time", "EPSG:4326")
    if id == "cmip6-cesm2-noleap-zarr":
        ds = xr.open_zarr(
            next(raw.glob("*.zarr")),
            decode_times=xr.coders.CFDatetimeCoder(use_cftime=True),
            chunks=None,
        )
        return GridTruth(ds, "tas", "lon", "lat", "time", "EPSG:4326")
    if id == "era5-fiji-antimeridian-nc":
        return GridTruth(
            xr.open_dataset(raw / "era5_fiji_2020-01.nc"),
            "t2m",
            "longitude",
            "latitude",
            "valid_time",
            "EPSG:4326",
        )
    if id == "daymet-lcc-nc":
        lcc = (
            "+proj=lcc +lat_1=25 +lat_2=60 +lat_0=42.5 +lon_0=-100 +x_0=0 +y_0=0 "
            "+ellps=WGS84 +units=m"
        )
        return GridTruth(
            xr.open_dataset(
                raw / "daymet_v4_daily_na_tmax_2020.nc",
                decode_times=xr.coders.CFDatetimeCoder(use_cftime=True),
            ),
            "tmax",
            "x",
            "y",
            "time",
            lcc,
        )
    return (
        None  # HDF4-EOS (MODIS MOD13A3) and SMAP HDF5: no independent reader in this environment
    )


def compare(truth, dump: dict, expected: tuple | None = None) -> dict:
    out: dict = {}
    store_times = [t[:10] if t[11:16] in ("00:00", "") else t[:16] for t in dump["times"]]
    unique_days = len({d[:10] for d in truth.dates}) == len(truth.dates)
    # sub-daily sources (several steps a day) are matched on the full timestamp, daily ones on
    # the day
    store_days = [t[:10] if unique_days else t[:16] for t in dump["times"]]
    truth_days = [
        d[:10] if unique_days else (d if "T" in d else d + "T00:00") for d in truth.dates
    ]
    out["n_times_store"] = len(store_days)
    out["n_times_source"] = len(truth_days)
    out["dates_equal"] = store_days == truth_days
    out["dates_same_set"] = sorted(store_days) == sorted(truth_days)
    out["store_first_last"] = [store_times[0], store_times[-1]] if store_times else []
    out["source_first_last"] = [truth.dates[0], truth.dates[-1]]
    if store_days != truth_days:
        missing = sorted(set(truth_days) - set(store_days))
        extra = sorted(set(store_days) - set(truth_days))
        out["dates_missing_from_store"] = missing[:10]
        out["dates_extra_in_store"] = extra[:10]
    store_crs_text = dump["attrs"].get("crs")
    out["store_crs"] = store_crs_text
    try:
        store_crs = pyproj.CRS.from_user_input(store_crs_text)
        out["crs_equal"] = bool(store_crs.equals(truth.crs, ignore_axis_order=True))
    except Exception as err:
        store_crs = None
        out["crs_equal"] = False
        out["crs_error"] = f"{type(err).__name__}: {err}"
    out["source_crs"] = truth.crs.to_string()
    out["store_bands"] = dump["bands"]
    out["store_dims"] = dump["dims"]
    out["x_range"] = [min(dump["x"]), max(dump["x"])]
    out["y_descending"] = bool(dump["y"][0] > dump["y"][-1])
    out["x_beyond_180"] = bool(
        truth.crs.is_geographic and max(abs(v) for v in out["x_range"]) > 180.0001
    )
    if store_crs is None:
        out["values"] = {"checked": False, "reason": "store CRS unusable"}
        return out
    xs = np.asarray(dump["x"])[np.asarray(dump["cols"])]
    ys = np.asarray(dump["y"])[np.asarray(dump["rows"])]
    compared = 0
    max_err = 0.0
    bad: list[dict] = []
    invalid_checked = 0
    invalid_bad = 0
    per_date = []
    store_all: list = []
    src_all: list = []
    for sample in dump["samples"]:
        day = store_days[sample["t"]]
        if day not in truth_days:
            per_date.append({"t": sample["t"], "date": day, "note": "date not in source"})
            continue
        i = truth_days.index(day)
        src = truth.sample(i, xs, ys, store_crs)
        stv = np.array(
            [[np.nan if v is None else v for v in band] for band in sample["values"]],
            dtype="float64",
        )
        if stv.shape[0] != src.shape[0]:
            per_date.append(
                {
                    "t": sample["t"],
                    "date": day,
                    "note": f"band count store {stv.shape[0]} source {src.shape[0]}",
                }
            )
            continue
        store_all.append(stv[np.isfinite(stv)])
        src_all.append(src[np.isfinite(src)])
        valid = np.isfinite(src).all(axis=0)
        take = np.flatnonzero(valid)[:3]
        n_cmp = 0
        for k in take:
            a, b = src[:, k], stv[:, k]
            err = float(np.nanmax(np.abs(a - b))) if np.isfinite(b).any() else float("inf")
            tol = 1e-4 * float(np.max(np.abs(a))) + 1e-6
            n_cmp += 1
            compared += 1
            max_err = max(max_err, err if np.isfinite(err) else 1e30)
            if not (np.isfinite(b).all() and err <= tol):
                bad.append(
                    {
                        "date": day,
                        "pixel": [int(dump["rows"][k]), int(dump["cols"][k])],
                        "source": a.tolist(),
                        "store": b.tolist(),
                    }
                )
        inv = np.flatnonzero(~valid)[:20]
        for k in inv:
            invalid_checked += 1
            if np.isfinite(stv[:, k]).any() and not np.isfinite(src[:, k]).any():
                invalid_bad += 1
        per_date.append(
            {
                "t": sample["t"],
                "date": day,
                "compared": n_cmp,
                "valid_candidates": int(valid.sum()),
                "of": len(valid),
            }
        )
    if expected is not None:
        lo, hi = expected
        st = np.concatenate(store_all) if store_all else np.array([])
        sr = np.concatenate(src_all) if src_all else np.array([])
        out["range"] = {
            "expected_physical": [lo, hi],
            "store_n": int(st.size),
            "store_outside": int(((st < lo) | (st > hi)).sum()),
            "store_min_max": [float(st.min()), float(st.max())] if st.size else None,
            "source_n": int(sr.size),
            "source_outside": int(((sr < lo) | (sr > hi)).sum()),
            "source_min_max": [float(sr.min()), float(sr.max())] if sr.size else None,
        }
    out["values"] = {
        "checked": True,
        "compared": compared,
        "max_abs_err": max_err,
        "mismatches": bad[:5],
        "n_mismatch": len(bad),
        "invalid_checked": invalid_checked,
        "invalid_shown_as_valid": invalid_bad,
        "per_date": per_date,
    }
    return out


def main(id: str, raw: str, dump_path: str, out_path: str, extra_nodata: str | None = None) -> int:
    dump = json.loads(Path(dump_path).read_text())
    truth = build(id, Path(raw))
    if truth is not None and extra_nodata is not None:
        truth.extra_nodata = float(extra_nodata)
    if truth is None:
        result = {
            "checked": False,
            "reason": "no independent reader for this source format in the check environment",
        }
    else:
        result = compare(truth, dump, EXPECTED.get(id))
    Path(out_path).write_text(json.dumps(result, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:6]))
