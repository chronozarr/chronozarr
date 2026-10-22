"""Fetch the Phase 0 "messy real data" corpus.

One function per dataset, registered with `@dataset(...)`. Every function writes into
`<CORPUS>/<id>/raw/` and is resumable: finished files are skipped, partial downloads end in
`.part` and are replaced. Run one with `--only <id>`; `--list` prints ids; `--manifest`
rewrites scripts/corpus/manifest.json from the registry plus the measured bytes on disk.

Downloaded data is untrusted: run this as `python -I scripts/corpus/fetch.py ...` from a venv that
has rasterio, xarray, netCDF4, h5py, zarr, gcsfs, adlfs, earthaccess, pystac-client,
planetary-computer, requests (data/corpus/.venv-fetch).

Rule of thumb for "raw": the files a scientist has on disk after downloading, after the one
unpacking step they would always do (gunzip, unzip). Subsetting by space or time only to stay
inside the budget. Datasets marked `derived` or `emulated` could not be fetched as delivered (no
public source or no account); the manifest says how they were made.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import gzip
import json
import os
import shutil
import sys
import time
import zipfile
from collections.abc import Callable
from pathlib import Path

import requests

CORPUS = Path(
    os.environ.get("CHRONOZARR_CORPUS", Path(__file__).resolve().parents[2] / "data" / "corpus")
)
MANIFEST = Path(__file__).resolve().parent / "manifest.json"
UA = {"User-Agent": "chronozarr-corpus-fetch/1.0"}
MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

REGISTRY: dict[str, dict] = {}


def dataset(id: str, **meta) -> Callable:
    """Register a fetch function with the manifest fields that do not depend on the files."""

    def deco(fn: Callable[[Path], None]) -> Callable[[Path], None]:
        REGISTRY[id] = {"id": id, "fn": fn, **meta}
        return fn

    return deco


# --- shared helpers -----------------------------------------------------------------------------


def raw_dir(id: str) -> Path:
    path = CORPUS / id / "raw"
    path.mkdir(parents=True, exist_ok=True)
    return path


def scratch_dir(id: str) -> Path:
    """Where downloads that are not themselves the raw files (gz, zip, full scenes) are kept."""
    path = CORPUS / id / "_download"
    path.mkdir(parents=True, exist_ok=True)
    return path


def download(
    url: str, dest: Path, session: requests.Session | None = None, tries: int = 5
) -> bool:
    """Stream `url` to `dest` through a .part file. Returns False on 404, raises after `tries`."""
    if dest.exists() and dest.stat().st_size > 0:
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    get = (session or requests).get
    last: Exception | None = None
    for attempt in range(tries):
        try:
            with get(url, stream=True, timeout=(20, 120), headers=UA) as resp:
                if resp.status_code == 404:
                    return False
                resp.raise_for_status()
                with part.open("wb") as fh:
                    for chunk in resp.iter_content(1 << 20):
                        fh.write(chunk)
            expected = resp.headers.get("content-length")
            if (
                expected
                and part.stat().st_size != int(expected)
                and not resp.headers.get("content-encoding")
            ):
                raise OSError(f"short read {part.stat().st_size} of {expected}")
            part.replace(dest)
            return True
        except (requests.RequestException, OSError) as err:
            last = err
            time.sleep(2 + 3 * attempt)
    raise RuntimeError(f"{url}: {last}")


def run_parallel(fn: Callable, items: list, workers: int = 8) -> list:
    with cf.ThreadPoolExecutor(workers) as pool:
        return list(pool.map(fn, items))


def daterange(start: dt.date, end: dt.date) -> list[dt.date]:
    return [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]


def finished(raw: Path) -> bool:
    return (raw / ".." / ".done").resolve().exists()


def mark_done(raw: Path) -> None:
    (raw.parent / ".done").write_text(
        dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ") + "\n"
    )


# --- 1. CHIRPS Africa monthly (dotted dates, gz) -------------------------------------------------


@dataset(
    "chirps-africa-monthly",
    title="CHIRPS v2.0 Africa monthly precipitation, 2019-2020",
    sources=["https://data.chc.ucsb.edu/products/CHIRPS-2.0/africa_monthly/tifs/"],
    license="CHIRPS: public domain (UCSB Climate Hazards Center, cite Funk et al. 2015)",
    fmt="folder of GeoTIFF, one per month, delivered as .tif.gz and gunzipped",
    crs="EPSG:4326 (north-up, latitude descending)",
    tags=["dotted-date-names", "nodata--9999", "float32", "gz-unpacked"],
    on_disk=(
        "24 files chirps-v2.0.2019.01.tif ... chirps-v2.0.2020.12.tif in one folder, gunzipped "
        "from the CHC download"
    ),
    newcomer={"input": "raw", "flags": []},
)
def fetch_chirps(raw: Path) -> None:
    dl = scratch_dir("chirps-africa-monthly")
    months = [(y, m) for y in (2019, 2020) for m in range(1, 13)]

    def one(ym: tuple[int, int]) -> None:
        y, m = ym
        name = f"chirps-v2.0.{y}.{m:02d}.tif"
        out = raw / name
        if out.exists():
            return
        gz = dl / (name + ".gz")
        url = f"https://data.chc.ucsb.edu/products/CHIRPS-2.0/africa_monthly/tifs/{name}.gz"
        if not download(url, gz):
            raise RuntimeError(f"404 {url}")
        with gzip.open(gz, "rb") as src, (raw / (name + ".part")).open("wb") as dst:
            shutil.copyfileobj(src, dst)
        (raw / (name + ".part")).replace(out)

    run_parallel(one, months, 6)


# --- 2. WorldClim monthly tmax (YYYY-MM names, zip) ----------------------------------------------


@dataset(
    "worldclim-tmax-monthly",
    title=(
        "WorldClim 2.1 historical monthly maximum temperature (CRU-TS 4.06), 2020-2021, 10 "
        "arc-minute"
    ),
    sources=[
        "https://geodata.ucdavis.edu/climate/worldclim/2_1/hist/cts4.06/10m/wc2.1_cruts4.06_10m_tmax_2020-2021.zip"
    ],
    license="WorldClim 2.1: CC BY 4.0 (Fick and Hijmans 2017)",
    fmt="folder of GeoTIFF, one per month, unzipped",
    crs="EPSG:4326 (north-up, latitude descending)",
    tags=[
        "YYYY-MM-names",
        "float32",
        "nodata-NaN-declared",
        "band-description-is-the-file-name",
        "control-case",
    ],
    on_disk="24 files wc2.1_10m_tmax_2020-01.tif ... 2021-12.tif unzipped into one folder",
    newcomer={"input": "raw", "flags": []},
)
def fetch_worldclim(raw: Path) -> None:
    dl = scratch_dir("worldclim-tmax-monthly")
    zpath = dl / "wc2.1_cruts4.06_10m_tmax_2020-2021.zip"
    url = "https://geodata.ucdavis.edu/climate/worldclim/2_1/hist/cts4.06/10m/wc2.1_cruts4.06_10m_tmax_2020-2021.zip"
    download(url, zpath)
    with zipfile.ZipFile(zpath) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            target = raw / Path(info.filename).name
            if not target.exists():
                with zf.open(info) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)


# --- 3. NSIDC Sea Ice Index daily, north, nested folders, EPSG:3411 ------------------------------


@dataset(
    "nsidc-seaice-n-daily",
    title=(
        "NSIDC Sea Ice Index v4 daily sea ice concentration, Northern Hemisphere, 2020-01-01 to "
        "2020-11-15"
    ),
    sources=["https://noaadata.apps.nsidc.org/NOAA/G02135/north/daily/geotiff/"],
    license="NSIDC / NOAA@NSIDC open data (cite Fetterer et al., G02135 v4)",
    fmt="GeoTIFF in per-month subfolders as the NSIDC server lays them out",
    crs="EPSG:3411 NSIDC polar stereographic north (Hughes 1980 ellipsoid)",
    tags=[
        "YYYYMMDD-names",
        "nested-folders",
        "polar-stereographic",
        "daily-300+",
        "integer-flags",
    ],
    on_disk="320 files N_20200101_concentration_v4.0.tif ... in 2020/01_Jan/ ... 2020/11_Nov/",
    newcomer={"input": "raw", "flags": []},
)
def fetch_nsidc_n(raw: Path) -> None:
    base = "https://noaadata.apps.nsidc.org/NOAA/G02135/north/daily/geotiff"
    days = daterange(dt.date(2020, 1, 1), dt.date(2020, 11, 15))

    def one(day: dt.date) -> str | None:
        rel = (
            f"{day.year}/{day.month:02d}_{MONTH_ABBR[day.month - 1]}/"
            f"N_{day:%Y%m%d}_concentration_v4.0.tif"
        )
        ok = download(f"{base}/{rel}", raw / rel)
        return None if ok else rel

    missing = [m for m in run_parallel(one, days, 12) if m]
    if missing:
        (raw.parent / "MISSING_ON_SERVER.txt").write_text("\n".join(missing) + "\n")


# --- 4. NSIDC Sea Ice Index monthly, south, EPSG:3412, YYYYMM, five steps ------------------------


@dataset(
    "nsidc-seaice-s-monthly",
    title=(
        "NSIDC Sea Ice Index v4 monthly sea ice concentration, Southern Hemisphere, 2020-01 to "
        "2020-05"
    ),
    sources=["https://noaadata.apps.nsidc.org/NOAA/G02135/south/monthly/geotiff/"],
    license="NSIDC / NOAA@NSIDC open data (cite Fetterer et al., G02135 v4)",
    fmt="GeoTIFF in per-month subfolders as the NSIDC server lays them out",
    crs="EPSG:3412 NSIDC polar stereographic south (Hughes 1980 ellipsoid)",
    tags=["YYYYMM-names", "nested-folders", "polar-stereographic", "tiny-5-steps"],
    on_disk="5 files S_202001_concentration_v4.0.tif ... S_202005_... in 01_Jan/ ... 05_May/",
    newcomer={"input": "raw", "flags": []},
)
def fetch_nsidc_s(raw: Path) -> None:
    base = "https://noaadata.apps.nsidc.org/NOAA/G02135/south/monthly/geotiff"
    for m in range(1, 6):
        rel = f"{m:02d}_{MONTH_ABBR[m - 1]}/S_2020{m:02d}_concentration_v4.0.tif"
        if not download(f"{base}/{rel}", raw / rel):
            raise RuntimeError(f"404 {base}/{rel}")


# --- 5. PRISM daily precipitation, BIL in zip, NAD83 ---------------------------------------------


@dataset(
    "prism-daily-bil-zip",
    title="PRISM 4 km daily precipitation, contiguous US, 2020-01-01 to 2020-02-14, BIL in zip",
    sources=["https://services.nacse.org/prism/data/get/us/4km/ppt/YYYYMMDD?format=bil"],
    license=(
        "PRISM Climate Group, Oregon State University: free for non-commercial use with citation "
        "(prism.oregonstate.edu/terms)"
    ),
    fmt=(
        "one zip per day holding .bil, .hdr, .prj, .stx, .bil.aux.xml, .stn.csv, .info.txt; also "
        "unzipped"
    ),
    crs="NAD83 geographic (EPSG:4269)",
    tags=[
        "BIL-not-GeoTIFF",
        "zip",
        "NAD83-geographic",
        "YYYYMMDD-names",
        "nodata--9999",
        "sidecar-files",
    ],
    on_disk=(
        "zips/ has 46 zips as downloaded; unzipped/ has their 322 files (one .bil per day) side "
        "by side"
    ),
    newcomer={"input": "raw/unzipped", "flags": []},
)
def fetch_prism(raw: Path) -> None:
    zips = raw / "zips"
    unz = raw / "unzipped"
    unz.mkdir(parents=True, exist_ok=True)
    days = daterange(dt.date(2020, 1, 1), dt.date(2020, 2, 15))

    def one(day: dt.date) -> None:
        name = f"prism_ppt_us_25m_{day:%Y%m%d}.zip"
        dest = zips / name
        if not download(
            f"https://services.nacse.org/prism/data/get/us/4km/ppt/{day:%Y%m%d}?format=bil", dest
        ):
            raise RuntimeError(f"404 {name}")
        with zipfile.ZipFile(dest) as zf:
            for info in zf.infolist():
                target = unz / Path(info.filename).name
                if not target.exists():
                    with zf.open(info) as src, target.open("wb") as out:
                        shutil.copyfileobj(src, out)

    run_parallel(one, days, 4)


# --- Earthdata Login helpers ---------------------------------------------------------------------


def earthdata_download(
    results: list, dest: Path, only: Callable[[str], bool] = lambda _: True
) -> None:
    """Download the data links of CMR granule results that `only` accepts. Uses ~/.netrc."""
    import earthaccess

    earthaccess.login(strategy="netrc")
    dest.mkdir(parents=True, exist_ok=True)
    links = [link for g in results for link in g.data_links() if only(link)]
    todo = [link for link in links if not (dest / link.rsplit("/", 1)[-1]).exists()]
    if todo:
        earthaccess.download(todo, str(dest), threads=4)


# --- 6. MODIS MOD13A1 NDVI COGs from Planetary Computer (sinusoidal, julian dates) ---------------

PC_STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"


def pc_search(**kwargs) -> list:
    import planetary_computer
    import pystac_client

    cat = pystac_client.Client.open(PC_STAC, modifier=planetary_computer.sign_inplace)
    return sorted(
        cat.search(**kwargs).items(),
        key=lambda i: (
            i.datetime
            or dt.datetime.fromisoformat(i.properties["start_datetime"].replace("Z", "+00:00"))
        ),
    )


def pc_signed(href: str) -> str:
    import planetary_computer

    return planetary_computer.sign(href)


@dataset(
    "modis-13a1-pc-cog-julian",
    title=(
        "MODIS Terra MOD13A1 v061 500 m NDVI, tile h12v04, 2020, 16-day composites (COG GeoTIFF)"
    ),
    sources=["https://planetarycomputer.microsoft.com/dataset/modis-13A1-061"],
    license="NASA LP DAAC MODIS data: open, no restrictions (cite DOI 10.5067/MODIS/MOD13A1.061)",
    fmt="folder of COG GeoTIFF, one per composite, original Planetary Computer file names",
    crs="MODIS sinusoidal (SR-ORG:6974)",
    tags=[
        "julian-date-names",
        "processing-timestamp-in-name",
        "MODIS-sinusoidal",
        "int16-scale-0.0001",
        "nodata--3000",
    ],
    on_disk=(
        "23 files MOD13A1.A2020001.h12v04.061.<proc>_500m_16_days_NDVI.tif ... A2020353, "
        "2400x2400 int16"
    ),
    newcomer={"input": "raw", "flags": []},
)
def fetch_modis_pc(raw: Path) -> None:
    items = pc_search(
        collections=["modis-13A1-061"],
        datetime="2020-01-01/2020-12-31",
        query={"modis:horizontal-tile": {"eq": 12}, "modis:vertical-tile": {"eq": 4}},
    )
    items = [i for i in items if i.id.startswith("MOD13A1.A2020")]
    if len(items) != 23:
        raise RuntimeError(
            f"expected 23 MOD13A1 composites for h12v04 in 2020, found {len(items)}"
        )

    def one(item) -> None:
        href = item.assets["500m_16_days_NDVI"].href
        name = href.split("?")[0].rsplit("/", 1)[-1]
        download(pc_signed(href), raw / name)

    run_parallel(one, items, 6)


# --- 7. MODIS MOD13A3 HDF4-EOS from LP DAAC (Earthdata) ------------------------------------------


@dataset(
    "modis-mod13a3-hdf4",
    title="MODIS Terra MOD13A3 v061 1 km monthly vegetation indices, tile h09v05, 2020 (HDF4-EOS)",
    sources=["https://lpdaac.usgs.gov/products/mod13a3v061/ (granules via NASA Earthdata CMR)"],
    license=(
        "NASA LP DAAC MODIS data: open, no restrictions (cite DOI 10.5067/MODIS/MOD13A3.061); "
        "Earthdata login to download"
    ),
    fmt="folder of HDF4-EOS (.hdf), one per month, as delivered",
    crs="MODIS sinusoidal",
    tags=[
        "HDF4-EOS",
        "julian-date-names",
        "MODIS-sinusoidal",
        "subdatasets",
        "int16-scale-0.0001",
    ],
    on_disk=(
        "12 files MOD13A3.A2020001.h09v05.061.<proc>.hdf ... A2020336, ~20 MB each, 13 "
        "subdatasets each"
    ),
    newcomer={"input": "raw", "flags": []},
)
def fetch_mod13a3(raw: Path) -> None:
    import earthaccess

    earthaccess.login(strategy="netrc")
    res = earthaccess.search_data(
        short_name="MOD13A3",
        version="061",
        temporal=("2020-01-01", "2020-12-31"),
        bounding_box=(-100.5, 35.5, -99.5, 36.5),
    )
    earthdata_download(res, raw, only=lambda link: ".h09v05." in link and link.endswith(".hdf"))
    if len(list(raw.glob("*.hdf"))) != 12:
        raise RuntimeError(
            f"expected 12 MOD13A3 h09v05 files, have {len(list(raw.glob('*.hdf')))}"
        )


# --- 8, 9. Landsat 8 collection 2 level 2 over eastern North Carolina (Planetary Computer) -------

LANDSAT_QUERY = {
    "collections": ["landsat-c2-l2"],
    "datetime": "2020-01-01/2020-12-31",
    "query": {
        "landsat:wrs_path": {"eq": "015"},
        "landsat:wrs_row": {"eq": "035"},
        "platform": {"eq": "landsat-8"},
        "eo:cloud_cover": {"lt": 25},
    },
}
WINDOW = 1500  # pixels per side of every crop


def read_window(href: str, window) -> tuple:
    import rasterio

    with (
        rasterio.Env(
            GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
            CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.TIF,.tiff",
        ),
        rasterio.open(pc_signed(href)) as src,
    ):
        return (
            src.read(1, window=window),
            src.window_transform(window),
            src.profile,
            src.tags(),
            src.descriptions,
        )


def write_crop(
    path: Path, data, transform, profile: dict, tags: dict, nodata=None, descriptions=None
) -> None:
    import rasterio

    prof = {
        "driver": "GTiff",
        "height": data.shape[0],
        "width": data.shape[1],
        "count": 1,
        "dtype": str(data.dtype),
        "crs": profile["crs"],
        "transform": transform,
        "tiled": True,
        "blockxsize": 512,
        "blockysize": 512,
        "compress": "deflate",
    }
    if nodata is not None:
        prof["nodata"] = nodata
    part = path.with_name(path.name + ".part")
    with rasterio.open(part, "w", **prof) as dst:
        dst.write(data, 1)
        if tags:
            dst.update_tags(**tags)
        if descriptions and descriptions[0]:
            dst.set_band_description(1, descriptions[0])
    part.replace(path)


@dataset(
    "landsat-c2-productid",
    title=(
        "Landsat 8 collection 2 level 2 surface reflectance red (SR_B4), WRS-2 path 015 row 035, "
        "10 scenes of 2020, 1500 px windows"
    ),
    sources=["https://planetarycomputer.microsoft.com/dataset/landsat-c2-l2"],
    license="USGS Landsat: public domain",
    fmt="folder of GeoTIFF, USGS product-ID names, each cut to its own 1500 x 1500 pixel window",
    crs="EPSG:32618 UTM 18N",
    tags=[
        "product-id-names",
        "two-dates-in-name",
        "grid-origin-changes-between-dates",
        "UTM",
        "uint16",
        "nodata-0",
    ],
    on_disk=(
        "10 files LC08_L2SP_015035_<acq>_<proc>_02_T1_SR_B4.TIF; origins differ by whole 30 m "
        "pixels (scene footprints shift); cropped to the same pixel window of each scene"
    ),
    newcomer={"input": "raw", "flags": []},
)
def fetch_landsat(raw: Path) -> None:
    items = pc_search(**LANDSAT_QUERY)
    if len(items) < 8:
        raise RuntimeError(f"only {len(items)} Landsat scenes")

    def one(item) -> None:
        from rasterio.windows import Window

        href = item.assets["red"].href
        name = href.split("?")[0].rsplit("/", 1)[-1]
        if (raw / name).exists():
            return
        data, transform, profile, tags, desc = read_window(
            href,
            Window(3000, 3000, WINDOW, WINDOW),  # ty: ignore[too-many-positional-arguments]  # attrs class, ty sees no __init__
        )
        write_crop(
            raw / name,
            data,
            transform,
            profile,
            tags,
            nodata=profile.get("nodata"),
            descriptions=desc,
        )

    run_parallel(one, items, 4)


@dataset(
    "landsat-ndvi-derived-nan",
    title=(
        "NDVI computed from Landsat 8 collection 2 surface reflectance, eastern North Carolina "
        "(WRS-2 path 015 row 035), 10 scenes of 2020"
    ),
    sources=[
        "https://planetarycomputer.microsoft.com/dataset/landsat-c2-l2 (red, nir08, qa_pixel "
        "assets)"
    ],
    license="USGS Landsat: public domain",
    fmt=(
        "folder of float32 GeoTIFF written by rasterio, one per scene (derived, as a scientist "
        "computes NDVI)"
    ),
    crs="EPSG:32618 UTM 18N",
    tags=[
        "derived",
        "float32-NaN-no-nodata-tag",
        "ndvi_YYYYMMDD-names",
        "cloud-masked-to-NaN",
        "same-grid",
    ],
    on_disk=(
        "10 files ndvi_YYYYMMDD.tif, float32, NaN where cloud, shadow or fill, no nodata tag, one "
        "common 1500 x 1500 30 m grid"
    ),
    newcomer={"input": "raw", "flags": []},
)
def fetch_landsat_ndvi(raw: Path) -> None:
    import numpy as np
    import rasterio
    import rasterio.windows
    from rasterio.windows import Window

    items = pc_search(**LANDSAT_QUERY)
    first_href = items[0].assets["red"].href
    with (
        rasterio.Env(
            GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
            CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.TIF,.tiff",
        ),
        rasterio.open(pc_signed(first_href)) as ref,
    ):
        win0 = Window(3000, 3000, WINDOW, WINDOW)  # ty: ignore[too-many-positional-arguments]  # attrs class, ty sees no __init__
        west, south, east, north = rasterio.windows.bounds(win0, ref.transform)
        crs = ref.crs

    def one(item) -> None:
        out = raw / f"ndvi_{item.datetime:%Y%m%d}.tif"
        if out.exists():
            return
        arrays = {}
        transform = None
        for band in ("red", "nir08", "qa_pixel"):
            href = item.assets[band].href
            with (
                rasterio.Env(
                    GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
                    CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.TIF,.tiff",
                ),
                rasterio.open(pc_signed(href)) as src,
            ):
                win = (
                    rasterio.windows.from_bounds(west, south, east, north, src.transform)
                    .round_offsets()
                    .round_lengths()
                )
                arrays[band] = src.read(1, window=win, boundless=False)
                transform = src.window_transform(win)
        red = arrays["red"].astype("float32") * 2.75e-05 - 0.2
        nir = arrays["nir08"].astype("float32") * 2.75e-05 - 0.2
        qa = arrays["qa_pixel"]
        bad = (
            (arrays["red"] == 0) | (arrays["nir08"] == 0) | ((qa & 0b00011110) != 0)
        )  # fill, dilated cloud, cirrus, cloud, shadow
        with np.errstate(invalid="ignore", divide="ignore"):
            ndvi = (nir - red) / (nir + red)
        ndvi[bad] = np.nan
        write_crop(out, ndvi.astype("float32"), transform, {"crs": crs}, {})

    run_parallel(one, items, 3)


# --- 10, 11. Google Earth Engine style exports (emulated from the real MODIS NDVI above) ---------


def modis_ndvi_files() -> list[Path]:
    src = CORPUS / "modis-13a1-pc-cog-julian" / "raw"
    files = sorted(src.glob("*NDVI.tif"))
    if len(files) != 23:
        raise RuntimeError(
            f"fetch modis-13a1-pc-cog-julian first ({len(files)} files found, need 23)"
        )
    return files


def modis_date(path: Path) -> dt.date:
    token = path.name.split(".")[1]  # A2020001
    return dt.datetime.strptime(token[1:], "%Y%j").date()


@dataset(
    "gee-multiband-dates",
    title=(
        "Earth Engine style export: one multi-band GeoTIFF whose 23 bands are the dates of MODIS "
        "MOD13A1 NDVI 2020"
    ),
    sources=[
        "https://planetarycomputer.microsoft.com/dataset/modis-13A1-061 (real values), repacked "
        "the way ee.Image.toBands() exports them"
    ],
    license="NASA LP DAAC MODIS data: open, no restrictions",
    fmt=(
        "one multi-band GeoTIFF, band descriptions YYYY_MM_DD_NDVI as toBands() names them, no "
        "scale/offset/nodata tags"
    ),
    crs="MODIS sinusoidal",
    tags=[
        "emulated",
        "dates-are-bands",
        "band-descriptions-carry-dates",
        "int16",
        "no-nodata-tag",
        "no-scale-tag",
    ],
    on_disk=(
        "1 file modis_ndvi_2020.tif, 23 bands int16, 800 x 800, descriptions 2020_01_01_NDVI ... "
        "2020_12_18_NDVI"
    ),
    newcomer={"input": "raw/modis_ndvi_2020.tif", "flags": []},
)
def fetch_gee_multiband(raw: Path) -> None:
    import numpy as np
    import rasterio
    from rasterio.windows import Window

    files = modis_ndvi_files()
    win = Window(800, 800, 800, 800)  # ty: ignore[too-many-positional-arguments]  # attrs class, ty sees no __init__
    stack = []
    names = []
    for path in files:
        with rasterio.open(path) as src:
            stack.append(src.read(1, window=win))
            transform, crs = src.window_transform(win), src.crs
        names.append(f"{modis_date(path):%Y_%m_%d}_NDVI")
    out = raw / "modis_ndvi_2020.tif"
    part = raw / "modis_ndvi_2020.tif.part"
    with rasterio.open(
        part,
        "w",
        driver="GTiff",
        height=800,
        width=800,
        count=len(stack),
        dtype="int16",
        crs=crs,
        transform=transform,
        tiled=True,
        blockxsize=256,
        blockysize=256,
    ) as dst:
        dst.write(np.stack(stack))
        for i, name in enumerate(names, 1):
            dst.set_band_description(i, name)
    part.replace(out)


@dataset(
    "gee-tiled-per-date",
    title=(
        "Earth Engine style export: each of 8 dates of MODIS NDVI split into 4 tiles named "
        "prefix-ROW-COL.tif"
    ),
    sources=[
        "https://planetarycomputer.microsoft.com/dataset/modis-13A1-061 (real values), tiled the "
        "way Export.image.toDrive names large exports"
    ],
    license="NASA LP DAAC MODIS data: open, no restrictions",
    fmt="GeoTIFF tiles, NDVI_YYYY_MM_DD-<row offset>-<col offset>.tif, 4 per date",
    crs="MODIS sinusoidal",
    tags=[
        "emulated",
        "tiled-exports",
        "several-files-per-date",
        "underscore-dates",
        "int16",
        "no-nodata-tag",
    ],
    on_disk=(
        "32 files NDVI_2020_01_01-0000000000-0000000000.tif ... -0000000500-0000000500.tif, 500 x "
        "500 each, one folder"
    ),
    newcomer={"input": "raw", "flags": []},
)
def fetch_gee_tiled(raw: Path) -> None:
    import rasterio
    from rasterio.windows import Window

    files = modis_ndvi_files()[:8]
    for path in files:
        date = modis_date(path)
        with rasterio.open(path) as src:
            for row in (0, 500):
                for col in (0, 500):
                    win = Window(800 + col, 800 + row, 500, 500)  # ty: ignore[too-many-positional-arguments]  # attrs class, ty sees no __init__
                    out = raw / f"NDVI_{date:%Y_%m_%d}-{row:010d}-{col:010d}.tif"
                    if out.exists():
                        continue
                    part = out.with_name(out.name + ".part")
                    with rasterio.open(
                        part,
                        "w",
                        driver="GTiff",
                        height=500,
                        width=500,
                        count=1,
                        dtype="int16",
                        crs=src.crs,
                        transform=src.window_transform(win),
                        tiled=True,
                        blockxsize=256,
                        blockysize=256,
                    ) as dst:
                        dst.write(src.read(1, window=win), 1)
                        dst.set_band_description(1, "NDVI")
                    part.replace(out)


# --- 12. NOAA OISST v2.1 daily from PSL: one NetCDF, 0..360, ascending lat, packed ---------------
# (time in days since 1800)


@dataset(
    "oisst-psl-daily",
    title=(
        "NOAA OISST v2.1 daily sea surface temperature, 2020, North Atlantic window, one NetCDF "
        "from PSL"
    ),
    sources=["https://downloads.psl.noaa.gov/Datasets/noaa.oisst.v2.highres/sst.day.mean.2020.nc"],
    license="NOAA PSL: open data, cite Huang et al. 2021 (OISST v2.1)",
    fmt="one CF NetCDF4 file, variable sst (time, lat, lon), cut to lat 20-60 N and lon 280-360 E",
    crs="EPSG:4326 geographic, longitudes 0..360, latitude ascending (CF, no grid_mapping)",
    tags=[
        "NetCDF",
        "lon-0-360",
        "lat-ascending",
        "float32-NaN-fill",
        "days-since-1800",
        "daily-300+",
        "no-crs-declared",
    ],
    on_disk=(
        "1 file sst.day.mean.2020.nc, 366 daily steps, 160 x 320 at 0.25 degrees, float32 with "
        "NaN fill"
    ),
    newcomer={"input": "raw/sst.day.mean.2020.nc", "flags": ["--variable", "sst"]},
)
def fetch_oisst_psl(raw: Path) -> None:
    import xarray as xr

    dl = scratch_dir("oisst-psl-daily")
    full = dl / "sst.day.mean.2020.nc"
    download(
        "https://downloads.psl.noaa.gov/Datasets/noaa.oisst.v2.highres/sst.day.mean.2020.nc", full
    )
    with xr.open_dataset(full) as ds:
        sub = ds.sel(lat=slice(20, 60), lon=slice(280, 360))
        part = raw / "sst.day.mean.2020.nc.part"
        sub.load().to_netcdf(part)
    part.replace(raw / "sst.day.mean.2020.nc")


# --- 13. NOAA OISST v2.1 daily from NCEI: one NetCDF per day ------------------------------------


@dataset(
    "oisst-ncei-daily-files",
    title=(
        "NOAA OISST v2.1 AVHRR daily sea surface temperature, 2020-01-01 to 2020-02-29, one "
        "NetCDF per day"
    ),
    sources=[
        "https://www.ncei.noaa.gov/data/sea-surface-temperature-optimum-interpolation/v2.1/access/avhrr/"
    ],
    license="NOAA NCEI: open data, cite Huang et al. 2021 (OISST v2.1)",
    fmt=(
        "one NetCDF4 file per day in YYYYMM folders, variables sst, anom, err, ice with dims "
        "(time, zlev, lat, lon)"
    ),
    crs="EPSG:4326 geographic global, longitudes 0..360, latitude ascending (CF, no grid_mapping)",
    tags=[
        "NetCDF-many-files",
        "lon-0-360",
        "lat-ascending",
        "packed-int16",
        "singleton-time-and-zlev",
        "several-variables",
        "YYYYMMDD-names",
    ],
    on_disk="60 files oisst-avhrr-v02r01.20200101.nc ... 20200229.nc in 202001/ and 202002/",
    newcomer={"input": "raw/**/*.nc", "flags": ["--variable", "sst"]},
)
def fetch_oisst_ncei(raw: Path) -> None:
    base = "https://www.ncei.noaa.gov/data/sea-surface-temperature-optimum-interpolation/v2.1/access/avhrr"
    days = daterange(dt.date(2020, 1, 1), dt.date(2020, 2, 29))

    def one(day: dt.date) -> None:
        rel = f"{day:%Y%m}/oisst-avhrr-v02r01.{day:%Y%m%d}.nc"
        if not download(f"{base}/{rel}", raw / rel):
            raise RuntimeError(f"404 {rel}")

    run_parallel(one, days, 6)


# --- 14, 15. CMIP6 from the public Pangeo/Google Cloud Zarr --------------------------------------


def cmip6_open(source_id: str, member_id: str):
    import pandas as pd
    import xarray as xr

    df = pd.read_csv("https://storage.googleapis.com/cmip6/pangeo-cmip6.csv")
    q = df[
        (df.table_id == "Amon")
        & (df.variable_id == "tas")
        & (df.experiment_id == "historical")
        & (df.member_id == member_id)
        & (df.source_id == source_id)
    ]
    if len(q) != 1:
        raise RuntimeError(f"expected one CMIP6 store for {source_id} {member_id}, found {len(q)}")
    return xr.open_zarr(
        q.zstore.iloc[0], consolidated=True, storage_options={"token": "anon"}
    ), q.zstore.iloc[0]


@dataset(
    "cmip6-hadgem3-360day-nc",
    title=(
        "CMIP6 HadGEM3-GC31-LL historical monthly near-surface air temperature (tas), 1950-2014, "
        "as one NetCDF"
    ),
    sources=[
        "gs://cmip6/CMIP6/CMIP/MOHC/HadGEM3-GC31-LL/historical/r1i1p1f3/Amon/tas/gn/v20190624/ "
        "(Pangeo CMIP6 Zarr, public)"
    ],
    license="CMIP6 MOHC output: CC BY 4.0 (cite the HadGEM3-GC31-LL CMIP6 data DOI)",
    fmt=(
        "one CMIP6-style NetCDF4 file (time, lat, lon) with lat_bnds, lon_bnds, time_bnds and a "
        "scalar height; written by xarray from the public Zarr"
    ),
    crs=(
        "EPSG:4326 geographic, longitudes 0..360, latitude ascending (CF, no grid_mapping), 1.25 "
        "x 1.875 degrees"
    ),
    tags=[
        "NetCDF",
        "calendar-360_day",
        "cftime-time-axis",
        "lon-0-360",
        "lat-ascending",
        "bounds-variables",
        "lat-lon-from-bounds",
        "monthly-780-steps",
    ],
    on_disk=(
        "1 file tas_Amon_HadGEM3-GC31-LL_historical_r1i1p1f3_gn_195001-201412.nc, 780 monthly "
        "steps, time in days since 1850-01-01 on a 360_day calendar"
    ),
    newcomer={
        "input": "raw/tas_Amon_HadGEM3-GC31-LL_historical_r1i1p1f3_gn_195001-201412.nc",
        "flags": ["--variable", "tas"],
    },
)
def fetch_cmip6_360(raw: Path) -> None:
    ds, _ = cmip6_open("HadGEM3-GC31-LL", "r1i1p1f3")
    sub = ds.sel(time=slice("1950-01", "2014-12")).load()
    units = {"units": "days since 1850-01-01", "calendar": "360_day", "dtype": "float64"}
    encoding = {
        "time": dict(units),
        "time_bnds": dict(units),
        "tas": {
            "zlib": True,
            "complevel": 1,
            "shuffle": True,
            "_FillValue": 1e20,
            "chunksizes": (1, sub.sizes["lat"], sub.sizes["lon"]),
        },
    }
    out = raw / "tas_Amon_HadGEM3-GC31-LL_historical_r1i1p1f3_gn_195001-201412.nc"
    part = out.with_name(out.name + ".part")
    sub.to_netcdf(part, encoding=encoding)
    part.replace(out)


@dataset(
    "cmip6-cesm2-noleap-zarr",
    title=(
        "CMIP6 CESM2 historical monthly near-surface air temperature (tas), 1950-2014, as a Zarr "
        "store written by xarray"
    ),
    sources=[
        "gs://cmip6/CMIP6/CMIP/NCAR/CESM2/historical/r1i1p1f1/Amon/tas/gn/v20190308/ (Pangeo "
        "CMIP6 Zarr, public)"
    ],
    license="CMIP6 NCAR output: CC BY 4.0 (cite the CESM2 CMIP6 data DOI)",
    fmt="Zarr store written by xarray `to_zarr` from the public Zarr, subset to 1950-2014",
    crs=(
        "EPSG:4326 geographic, longitudes 0..360, latitude ascending (CF, no grid_mapping), 0.94 "
        "x 1.25 degrees"
    ),
    tags=[
        "Zarr",
        "calendar-noleap",
        "cftime-time-axis",
        "lon-0-360",
        "lat-ascending",
        "monthly-780-steps",
    ],
    on_disk=(
        "1 directory tas_Amon_CESM2_historical_r1i1p1f1_gn_195001-201412.zarr, 780 monthly steps"
    ),
    newcomer={
        "input": "raw/tas_Amon_CESM2_historical_r1i1p1f1_gn_195001-201412.zarr",
        "flags": ["--variable", "tas"],
    },
)
def fetch_cmip6_noleap(raw: Path) -> None:
    ds, _ = cmip6_open("CESM2", "r1i1p1f1")
    sub = ds.sel(time=slice("1950-01", "2014-12")).load()
    for var in sub.variables.values():
        var.encoding.pop("chunks", None)
        var.encoding.pop("preferred_chunks", None)
        var.encoding.pop("compressors", None)
        var.encoding.pop("filters", None)
        var.encoding.pop("serializer", None)
        var.encoding.pop("shards", None)
    out = raw / "tas_Amon_CESM2_historical_r1i1p1f1_gn_195001-201412.zarr"
    if out.exists():
        shutil.rmtree(out)
    sub.to_zarr(out, mode="w")


# --- 16. ERA5 over Fiji, CDS-style NetCDF crossing the antimeridian ------------------------------


@dataset(
    "era5-fiji-antimeridian-nc",
    title=(
        "ERA5 2 m temperature and total precipitation over Fiji, 6-hourly, 2020-01-01 to "
        "2020-01-20, CDS-style NetCDF crossing 180 degrees"
    ),
    sources=[
        "gs://gcp-public-data-arco-era5/ar/full_37-1h-0p25deg-chunk-1.zarr-v3 (ARCO-ERA5, public)"
    ],
    license=(
        "Copernicus ERA5: Licence to Use Copernicus Products (CC BY 4.0 compatible, cite Hersbach "
        "et al. 2020)"
    ),
    fmt=(
        "one NetCDF4 file with CDS-beta names: valid_time, latitude, longitude, t2m, tp; area 5 S "
        "to 30 S and 165 E to 195 E"
    ),
    crs=(
        "EPSG:4326 geographic, longitudes 165..195 running through 180, latitude descending, 0.25 "
        "degrees"
    ),
    tags=[
        "emulated-CDS-layout",
        "NetCDF",
        "antimeridian",
        "lat-descending",
        "lon-beyond-180",
        "several-variables",
        "valid_time-axis",
        "6-hourly-80-steps",
    ],
    on_disk=(
        "1 file era5_fiji_2020-01.nc, 80 steps (6-hourly), variables t2m (K) and tp (m), "
        "valid_time in seconds since 1970-01-01"
    ),
    newcomer={"input": "raw/era5_fiji_2020-01.nc", "flags": ["--variable", "t2m"]},
)
def fetch_era5_fiji(raw: Path) -> None:
    import pandas as pd
    import xarray as xr

    ds = xr.open_zarr(
        "gs://gcp-public-data-arco-era5/ar/full_37-1h-0p25deg-chunk-1.zarr-v3",
        chunks=None,
        consolidated=True,
        storage_options={"token": "anon"},
    )
    times = pd.date_range("2020-01-01T00", "2020-01-20T18", freq="6h")
    sub = ds[["2m_temperature", "total_precipitation"]].sel(
        time=times, latitude=slice(-5, -30), longitude=slice(165, 195)
    )
    sub = sub.chunk({"time": 1}).load()
    sub = sub.rename({"time": "valid_time", "2m_temperature": "t2m", "total_precipitation": "tp"})
    sub = sub.assign_coords(number=0, expver="0001")
    sub["t2m"].attrs = {
        "long_name": "2 metre temperature",
        "units": "K",
        "standard_name": "unknown",
    }
    sub["tp"].attrs = {
        "long_name": "Total precipitation",
        "units": "m",
        "standard_name": "unknown",
    }
    sub["latitude"].attrs = {
        "long_name": "latitude",
        "units": "degrees_north",
        "standard_name": "latitude",
    }
    sub["longitude"].attrs = {
        "long_name": "longitude",
        "units": "degrees_east",
        "standard_name": "longitude",
    }
    sub.attrs = {
        "GRIB_centre": "ecmf",
        "GRIB_edition": 1,
        "Conventions": "CF-1.7",
        "institution": "European Centre for Medium-Range Weather Forecasts",
    }
    for var in sub.variables.values():
        for key in (
            "chunks",
            "preferred_chunks",
            "compressors",
            "filters",
            "serializer",
            "shards",
            "_FillValue",
        ):
            var.encoding.pop(key, None)
    sub["valid_time"].encoding.update(
        {"units": "seconds since 1970-01-01", "calendar": "proleptic_gregorian", "dtype": "int64"}
    )
    out = raw / "era5_fiji_2020-01.nc"
    part = out.with_name(out.name + ".part")
    sub.to_netcdf(
        part,
        encoding={"t2m": {"zlib": True, "complevel": 1}, "tp": {"zlib": True, "complevel": 1}},
    )
    part.replace(out)


# --- 17. Daymet daily tmax over Kansas, Lambert conformal, from Planetary Computer's Zarr --------


@dataset(
    "daymet-lcc-nc",
    title="Daymet v4 daily maximum temperature 2020, 200 km box in Kansas, as one NetCDF",
    sources=[
        "https://planetarycomputer.microsoft.com/dataset/daymet-daily-na (Zarr, public with "
        "signed token)"
    ],
    license="ORNL DAAC Daymet: open data, cite Thornton et al. (doi:10.3334/ORNLDAAC/2129)",
    fmt=(
        "one CF NetCDF4 file as ORNL delivers Daymet: dims (time, y, x), time_bnds, 2-D lat and "
        "lon, lambert_conformal_conic grid mapping; written by xarray from the Zarr"
    ),
    crs=(
        "Daymet Lambert conformal conic (lat_1 25, lat_2 60, lat_0 42.5, lon_0 -100, WGS84), 1 "
        "km, in metres"
    ),
    tags=[
        "NetCDF",
        "Lambert-conformal",
        "calendar-365-day-no-Dec-31",
        "2-D-lat-lon-variables",
        "daily-300+",
        "grid_mapping-variable",
    ],
    on_disk=(
        "1 file daymet_v4_daily_na_tmax_2020.nc, 365 daily steps (Dec 31 of the leap year is not "
        "in Daymet), 200 x 200 at 1 km"
    ),
    newcomer={"input": "raw/daymet_v4_daily_na_tmax_2020.nc", "flags": ["--variable", "tmax"]},
)
def fetch_daymet(raw: Path) -> None:
    import planetary_computer
    import pyproj
    import pystac_client
    import xarray as xr

    cat = pystac_client.Client.open(PC_STAC, modifier=planetary_computer.sign_inplace)
    asset = cat.get_collection("daymet-daily-na").assets["zarr-abfs"]
    ds = xr.open_zarr(
        asset.href,
        **asset.extra_fields["xarray:open_kwargs"],
        storage_options=asset.extra_fields["xarray:storage_options"],
    )
    lcc = pyproj.CRS.from_proj4(
        "+proj=lcc +lat_1=25 +lat_2=60 +lat_0=42.5 +lon_0=-100 +x_0=0 +y_0=0 +ellps=WGS84 +units=m"
    )
    cx, cy = pyproj.Transformer.from_crs("EPSG:4326", lcc, always_xy=True).transform(-97.5, 38.5)
    sub = ds[["tmax", "time_bnds", "lambert_conformal_conic"]].sel(
        time=slice("2020-01-01", "2020-12-31"),
        x=slice(cx - 100_000, cx + 100_000),
        y=slice(cy + 100_000, cy - 100_000),
    )
    sub = sub.load()
    for var in sub.variables.values():
        for key in (
            "chunks",
            "preferred_chunks",
            "compressors",
            "filters",
            "serializer",
            "shards",
        ):
            var.encoding.pop(key, None)
    out = raw / "daymet_v4_daily_na_tmax_2020.nc"
    part = out.with_name(out.name + ".part")
    sub.to_netcdf(part)
    part.replace(out)


# --- 18. HRRR 2 m temperature, GRIB2, Lambert conformal ------------------------------------------


@dataset(
    "hrrr-t2m-grib2",
    title="NOAA HRRR 2 m temperature analysis at 00 UTC, 12 days from 2024-01-15, GRIB2",
    sources=[
        "https://noaa-hrrr-bdp-pds.s3.amazonaws.com/ (AWS Open Data, "
        "hrrr.YYYYMMDD/conus/hrrr.t00z.wrfsfcf00.grib2, one message by idx byte range)"
    ],
    license="NOAA HRRR: open data (NOAA Big Data Program), no restrictions",
    fmt=(
        "GRIB2 files in hrrr.YYYYMMDD/conus/ folders as the S3 bucket lays them out, holding the "
        "single TMP 2 m message that herbie or curl byte ranges fetch"
    ),
    crs="HRRR Lambert conformal conic (3 km, CONUS)",
    tags=[
        "GRIB2",
        "Lambert-conformal",
        "date-only-in-folder-name",
        "same-file-name-every-day",
        "float32",
    ],
    on_disk=(
        "12 files hrrr.20240115/conus/hrrr.t00z.wrfsfcf00.grib2 ... hrrr.20240126/conus/..., 1059 "
        "x 1799 each"
    ),
    newcomer={"input": "raw/**/*.grib2", "flags": []},
)
def fetch_hrrr(raw: Path) -> None:
    for day in daterange(dt.date(2024, 1, 15), dt.date(2024, 1, 26)):
        rel = f"hrrr.{day:%Y%m%d}/conus/hrrr.t00z.wrfsfcf00.grib2"
        out = raw / rel
        if out.exists():
            continue
        url = f"https://noaa-hrrr-bdp-pds.s3.amazonaws.com/{rel}"
        rows = [
            line.split(":")
            for line in requests.get(url + ".idx", timeout=60, headers=UA).text.splitlines()
        ]
        k = next(i for i, r in enumerate(rows) if r[3] == "TMP" and r[4] == "2 m above ground")
        start, end = int(rows[k][1]), int(rows[k + 1][1]) - 1
        resp = requests.get(url, headers={**UA, "Range": f"bytes={start}-{end}"}, timeout=120)
        resp.raise_for_status()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(resp.content)


# --- 19. SMAP L3 soil moisture, HDF5, EASE-Grid 2.0 (Earthdata) ----------------------------------


@dataset(
    "smap-l3-hdf5",
    title=(
        "SMAP L3 radiometer global daily 36 km soil moisture (SPL3SMP v009), 2020-06-01 to "
        "2020-06-05, HDF5"
    ),
    sources=["https://nsidc.org/data/spl3smp/versions/9 (granules via NASA Earthdata CMR)"],
    license=(
        "NASA NSIDC DAAC SMAP data: open, cite DOI 10.5067/4XXAC7SZSKTS (Earthdata login to "
        "download)"
    ),
    fmt="one HDF5 file per day with groups Soil_Moisture_Retrieval_Data_AM and _PM",
    crs=(
        "EASE-Grid 2.0 global cylindrical (EPSG:6933), declared in the file as lat/lon arrays and "
        "metadata, not as a GeoTIFF CRS"
    ),
    tags=[
        "HDF5",
        "groups",
        "EASE-Grid-2.0",
        "tiny-5-steps",
        "YYYYMMDD-names",
        "NaN-fill--9999",
        "2-D-lat-lon-arrays",
    ],
    on_disk="5 files SMAP_L3_SM_P_20200601_R19240_001.h5 ... 20200605, ~35 MB each",
    newcomer={"input": "raw", "flags": []},
)
def fetch_smap(raw: Path) -> None:
    import earthaccess

    earthaccess.login(strategy="netrc")
    res = earthaccess.search_data(
        short_name="SPL3SMP", version="009", temporal=("2020-06-01", "2020-06-05")
    )
    earthdata_download(res, raw, only=lambda link: link.endswith(".h5"))
    if len(list(raw.glob("*.h5"))) != 5:
        raise RuntimeError(f"expected 5 SMAP files, have {len(list(raw.glob('*.h5')))}")


# --- 20. HLS S30 red band, julian dates with time, UTM (Earthdata) -------------------------------


@dataset(
    "hls-s30-julian",
    title=(
        "Harmonized Landsat Sentinel-2 S30 v2.0 red band (B04), MGRS tile T17SQV, 10 dates of 2020"
    ),
    sources=["https://lpdaac.usgs.gov/products/hlss30v002/ (granules via NASA Earthdata CMR)"],
    license=(
        "NASA LP DAAC HLS data: open, cite DOI 10.5067/HLS/HLSS30.002 (Earthdata login to "
        "download)"
    ),
    fmt="folder of COG GeoTIFF, one per acquisition, original LP DAAC names with YYYYDDDThhmmss",
    crs="EPSG:32617 UTM 17N",
    tags=[
        "julian-date-with-time-names",
        "UTM",
        "int16-scale-0.0001",
        "nodata--9999",
        "full-tile-3660px",
    ],
    on_disk=(
        "10 files HLS.S30.T17SQV.2020DDDThhmmss.v2.0.B04.tif, 3660 x 3660 int16, scale 0.0001, "
        "nodata -9999"
    ),
    newcomer={"input": "raw", "flags": []},
)
def fetch_hls(raw: Path) -> None:
    import earthaccess

    earthaccess.login(strategy="netrc")
    res = earthaccess.search_data(
        short_name="HLSS30",
        version="2.0",
        bounding_box=(-78.8, 35.7, -78.5, 35.9),
        temporal=("2020-03-01", "2020-10-31"),
        cloud_cover=(0, 20),
    )
    chosen = {}
    for g in res:
        links = [
            link for link in g.data_links() if ".T17SQV." in link and link.endswith(".B04.tif")
        ]
        if links:
            chosen[links[0]] = g
    links = sorted(chosen, key=lambda link: link.rsplit("/", 1)[-1].split(".")[3])
    if len(links) < 10:
        raise RuntimeError(f"only {len(links)} HLS granules with cloud cover under 20 percent")
    step = len(links) / 10
    picked = [links[int(i * step)] for i in range(10)]
    todo = [link for link in picked if not (raw / link.rsplit("/", 1)[-1]).exists()]
    if todo:
        earthaccess.download(todo, str(raw), threads=4)


# --- main ---------------------------------------------------------------------------------------


def dir_stats(raw: Path) -> tuple[int, int]:
    files = [p for p in raw.rglob("*") if p.is_file() and not p.name.endswith(".part")]
    return len(files), sum(p.stat().st_size for p in files)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--only", action="append", help="dataset id (repeatable)")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--manifest", action="store_true", help="rewrite manifest.json and exit")
    args = parser.parse_args(argv)
    if args.list:
        for id in REGISTRY:
            print(id)
        return 0
    if args.manifest:
        write_manifest()
        return 0
    ids = args.only or list(REGISTRY)
    unknown = [i for i in ids if i not in REGISTRY]
    if unknown:
        print(f"unknown dataset id(s): {unknown}; --list shows them", file=sys.stderr)
        return 2
    status = 0
    for id in ids:
        raw = raw_dir(id)
        started = time.time()
        print(f"[{id}] fetching", flush=True)
        try:
            REGISTRY[id]["fn"](raw)
        except Exception as err:
            status = 1
            print(f"[{id}] FAILED: {type(err).__name__}: {err}", file=sys.stderr, flush=True)
            continue
        mark_done(raw)
        n, size = dir_stats(raw)
        print(
            f"[{id}] done: {n} files, {size / 1e6:.1f} MB in {time.time() - started:.0f} s",
            flush=True,
        )
    return status


# Timesteps in each dataset (measured with the independent readers in _check_truth.py).
N_TIMES = {
    "chirps-africa-monthly": 24,
    "worldclim-tmax-monthly": 24,
    "nsidc-seaice-n-daily": 320,
    "nsidc-seaice-s-monthly": 5,
    "prism-daily-bil-zip": 46,
    "modis-13a1-pc-cog-julian": 23,
    "modis-mod13a3-hdf4": 12,
    "landsat-c2-productid": 10,
    "landsat-ndvi-derived-nan": 10,
    "gee-multiband-dates": 23,
    "gee-tiled-per-date": 8,
    "oisst-psl-daily": 366,
    "oisst-ncei-daily-files": 60,
    "cmip6-hadgem3-360day-nc": 780,
    "cmip6-cesm2-noleap-zarr": 780,
    "era5-fiji-antimeridian-nc": 80,
    "daymet-lcc-nc": 365,
    "hrrr-t2m-grib2": 12,
    "smap-l3-hdf5": 5,
    "hls-s30-julian": 10,
}

# How each dataset relates to what its source delivers.
PROVENANCE = {
    "chirps-africa-monthly": "as delivered, gunzipped",
    "worldclim-tmax-monthly": "as delivered, unzipped",
    "nsidc-seaice-n-daily": (
        "as delivered, folder layout of the server kept; 2020-01-01 to 2020-11-15 only"
    ),
    "nsidc-seaice-s-monthly": "as delivered, folder layout of the server kept; five months only",
    "prism-daily-bil-zip": (
        "as delivered (zip per day, format=bil) plus the unzipped files; 46 days only"
    ),
    "modis-13a1-pc-cog-julian": "as delivered by Planetary Computer (NDVI asset only)",
    "modis-mod13a3-hdf4": "as delivered (tile h09v05 only)",
    "landsat-c2-productid": (
        "as delivered but cut to 1500 x 1500 pixels of each scene (rows and columns 3000 to "
        "4500), name kept"
    ),
    "landsat-ndvi-derived-nan": (
        "derived: NDVI computed here from SR red and NIR with cloud, shadow and fill set to NaN "
        "(a scientist's own product)"
    ),
    "gee-multiband-dates": (
        "emulated: real MODIS NDVI values repacked as ee.Image.toBands() exports them (no Earth "
        "Engine project was available)"
    ),
    "gee-tiled-per-date": (
        "emulated: real MODIS NDVI values tiled with Earth Engine's prefix-ROW-COL.tif names (no "
        "Earth Engine project was available)"
    ),
    "oisst-psl-daily": (
        "as delivered but cut to lat 20-60 N, lon 280-360 E (netCDF rewritten by xarray)"
    ),
    "oisst-ncei-daily-files": "as delivered",
    "cmip6-hadgem3-360day-nc": (
        "written here from the public CMIP6 Zarr as one CMIP6-style NetCDF (the file ESGF would "
        "serve split by decade), 1950-2014"
    ),
    "cmip6-cesm2-noleap-zarr": (
        "written here from the public CMIP6 Zarr with xarray to_zarr, 1950-2014"
    ),
    "era5-fiji-antimeridian-nc": (
        "emulated: ARCO-ERA5 subset written with CDS-beta variable and coordinate names (no CDS "
        "account); float32, not the legacy packed int16"
    ),
    "daymet-lcc-nc": (
        "written here from the Planetary Computer Zarr as the one-year NetCDF ORNL delivers, cut "
        "to a 200 km box"
    ),
    "hrrr-t2m-grib2": "the TMP 2 m GRIB2 message only, fetched by idx byte range as herbie does",
    "smap-l3-hdf5": "as delivered",
    "hls-s30-julian": "as delivered (B04 only)",
}


def write_manifest() -> None:
    rows = []
    for id, entry in REGISTRY.items():
        raw = CORPUS / id / "raw"
        done = CORPUS / id / ".done"
        n_files, size = dir_stats(raw) if raw.exists() else (0, 0)
        meta = {k: v for k, v in entry.items() if k != "fn"}
        retrieved = done.read_text().strip() if done.exists() else None
        meta["retrieved_utc"] = retrieved
        meta["retrieved_date"] = (
            (dt.datetime.strptime(retrieved, "%Y-%m-%dT%H:%M:%SZ") - dt.timedelta(hours=4))
            .date()
            .isoformat()
            if retrieved
            else None
        )
        meta["n_times"] = N_TIMES[id]
        meta["n_files"] = n_files
        meta["bytes"] = size
        meta["provenance"] = PROVENANCE[id]
        rows.append(meta)
    MANIFEST.write_text(json.dumps(rows, indent=2) + "\n")
    print(f"wrote {MANIFEST} ({len(rows)} datasets)")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
