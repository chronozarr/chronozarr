---
name: chronozarr
description: Create, open, inspect, append to, share, publish and view chronozarr stores, a Zarr v3 layout for satellite and raster image time series with true stored values. Use when the user mentions chronozarr, a Zarr raster time series or satellite image time series store, converting dated GeoTIFFs, COGs, NetCDF or Zarr into a store, opening a chronozarr URL in xarray (engine="chronozarr"), hosting a store on S3, R2, GCS or Azure, running chronozarr doctor, or the chronozarr viewer at chronozarr.org/demo.
---

# chronozarr

chronozarr is a Zarr v3 layout convention for raster time series, with a Python package (CLI `chronozarr`, library `chronozarr`) and a browser viewer.

- A store holds the true stored values (uint8, uint16, int16 or float32), never quantized. Physical values are `stored * scale + offset`, computed at read time.
- The root group has level groups `"0"`, `"1"`, ... (a pyramid, level 0 is native). Each has a `data` array with dims `(time, band, y, x)` and coordinates `time`, `band`, `y`, `x`.
- The grid is the native projected CRS (for example UTM), north up. Stored data is not Web Mercator.
- A store is static files. Hosting needs no server: a bucket or CDN with CORS is enough.

Depth: https://chronozarr.org/llms.txt lists every page, https://chronozarr.org/llms-full.txt joins them, and each page is Markdown at `https://chronozarr.org/<route>.md`. The routes this file cites: `/quickstart.md`, `/guides/convert.md`, `/guides/preview.md`, `/guides/share.md`, `/guides/publish.md`, `/guides/analyze.md`, `/guides/append.md`, `/guides/sentinel-2.md`, `/guides/png-frames.md`, `/guides/notebooks.md`, `/guides/private.md`, `/guides/embedding.md`, `/guides/viewer-distribution.md`, `/guides/hosting-requirements.md`, `/guides/hosting-providers.md`, `/reference/cli.md`, `/reference/python.md`, `/specification.md`, `/troubleshooting.md`.

## Install

The PyPI package is `chronozarr`. Python 3.11+. Prefer uv.

```bash
uv tool install --python 3.13 'chronozarr[geo]'   # CLI only; --python 3.13 skips an older Python on PATH
uv add 'chronozarr[geo]'                          # in a project: library and CLI; `uv run chronozarr ...`
uvx --from 'chronozarr[geo]' chronozarr --help    # one-off
```

Extras: `geo` (rasterio: GeoTIFF/COG input, `export-cog`, `stac`; pyproj: `link --lonlat` for a projected store), `netcdf` (with cftime), `cftime` (a Zarr store on a `360_day`, `noleap`, `all_leap` or `julian` calendar), `s3` (list an `s3://` prefix in `convert`), `dask` (`chunks=` in xarray), `notebook` (`chronozarr.view`, `chronozarr.player`), `publish` (`chronozarr publish` to S3, R2 and other S3-compatible hosts), `publish-gcs` (`gs://`), `publish-azure` (`az://`), `leafmap` (`chronozarr.add_chronozarr`, a layer on a leafmap map), `geemap` (the same on a geemap map), `ingest` (the Sentinel-2 example in a repository checkout). With pip: `pip install 'chronozarr[geo]'`. Run `chronozarr <command> --help` for exact options.

## Which command

| The user has | Do this |
|---|---|
| A directory, quoted glob or `s3://` prefix of dated GeoTIFFs | `chronozarr convert SRC OUT --dry-run`, fix what it reports, then run without `--dry-run`. Date comes from the file name (`YYYYMMDD`, `YYYY-MM-DD`, `YYYY_MM_DD`, `YYYY-MM`, `YYYY_MM`, exactly one per name; a bare `YYYYMM` is not read); otherwise `--date-pattern 'ndvi_%Y%m%d'`, or `'MOD13A1.A%Y%j'` for a year and day of year (MODIS, HLS). A directory is not recursive; use `'dir/**/*.tif'`. Files on another grid fail with a hint: add `--resampling nearest` (or another method) |
| One GeoTIFF whose bands are dates (Earth Engine `toBands()`: descriptions such as `2020_01_01_NDVI`) | `chronozarr convert FILE OUT --bands-are-dates --dry-run`, then without `--dry-run`. `convert` refuses such a file without the option and names it. Each band description holds one date (the forms above; `--date-pattern` reads others from the descriptions) and the rest of the text names the band; several variables per date become the bands of a timestep. A pixel-interleaved file with many bands is slow: the dry run says so and gives the `gdal_translate -co INTERLEAVE=BAND` rewrite. See `/guides/convert.md#from-earth-engine-exports` |
| Several files per date that tile one image (Earth Engine splits a large export into `name-0000000000-0000000000.tif` files) | `chronozarr convert DIR OUT --mosaic-tiles --dry-run`, then without `--dry-run`. Without the option `convert` refuses each file, and when the files of a date are tiles of one grid (one CRS and pixel size, whole-pixel offsets, no two on the same pixels) the fix names `--mosaic-tiles`; never leave tiles out, which stores part of the area. Tiles that share pixels are refused (join them yourself with a rule for those pixels); pixels no tile of a date covers are invalid. Earth Engine's MODIS NDVI fill -3000 is undeclared: add `--nodata -3000`. See `/guides/convert.md#exports-split-into-tiles` |
| Dates not in the file names, files at URLs, or PNG frames | A manifest (`.csv` with `uri,datetime[,bands]`, or `.json`) passed to `convert`. `--write-manifest` saves what a directory scan found. PNG frames need a world file or `.png.aux.xml`, or `--crs` and `--bounds`; palette PNGs are refused (`/guides/png-frames.md`) |
| A NetCDF file or a Zarr array | `chronozarr convert SRC OUT --variable NAME` (`--dims` for unusual dim names). `--resampling`, `--transform`, `--shape` and `--bounds` do not apply here. A time axis on a `360_day`, `noleap`, `all_leap` or `julian` calendar (CMIP6) needs the `cftime` extra and `--calendar-to-gregorian date`, which keeps each step's date and time of day as a Gregorian timestamp and records the calendar in the store's provenance; `date-or-month-end` also moves a date Gregorian lacks (30 February) to the last day of its month. Use `convert` for these existing variables and their CF metadata. The separate in-memory `encode` interface requires literal `(time, band, y, x)` dimensions; see `/reference/cli.md` |
| An xarray DataArray in memory | `chronozarr.encode(da, "OUT", crs="EPSG:32631")`; `da` has dims `(time, band, y, x)` |
| Sentinel-2 imagery for an area and dates | A repository checkout: `uv sync --extra ingest`, then `uv run python examples/sentinel2_pc/ingest.py --bbox W S E N --name NAME --start 2024-06-01 --end 2024-08-31 --out-dir DIR` (or `--aoi-file area.geojson`). Not an installed command. See `/guides/sentinel-2.md` |
| A store path or URL to analyze | `xr.open_dataset(path_or_url, engine="chronozarr")`, or `chronozarr.open_store(path_or_url)`. See `/guides/analyze.md` |
| A store to inspect or check | `chronozarr info STORE`, `chronozarr validate STORE` (exit 1 if it does not conform) |
| New timesteps for an existing store | `chronozarr append STORE INPUT` (in place; see traps), then `chronozarr publish STORE --destination SAME_PREFIX --update` for a published copy |
| A local store to show to anyone, a colleague included (the first route to offer) | `chronozarr share STORE` (needs `cloudflared`). Prints a temporary public link that opens on any computer. Public while it runs, and only while the sharing computer is on and online. See `/guides/share.md` |
| To look at a local store on this computer only, privately or without `cloudflared` | `chronozarr preview STORE` (local server, opens the viewer; the data stays on the computer). Its link and the viewer's Copy link open only on this computer; use `share` for anyone else. Offline: `--viewer-dir` with a viewer copied by `chronozarr-viewer`. Safari can block the https viewer from reaching `127.0.0.1`; use Chrome or Firefox |
| A link that lasts | `chronozarr publish STORE --destination s3://BUCKET/PREFIX --dry-run`, then without `--dry-run` (`gs://`, `az://ACCOUNT/CONTAINER/PREFIX` too). It validates, uploads chunks before metadata and the root `zarr.json` last, runs the `doctor` checks on the public URL and prints a viewer link only if they pass. R2 and other S3-compatible hosts: add `--endpoint-url` and `--public-url`. `--apply-cors` adds the viewer's CORS rule. Use a fresh prefix per version. See `/guides/publish.md` |
| A store already hosted that needs checking | `chronozarr doctor https://host/prefix` (CORS, byte ranges, caching, decode) |
| A Jupyter notebook | `chronozarr.view("STORE")` or `chronozarr.player("STORE")` (`notebook` extra). JupyterHub, VS Code or SSH: `/guides/notebooks.md` |
| To choose the first view of a hosted store | `chronozarr link STORE_URL --product ndvi`, or `--product band --band B8 --range 0,4000 --colormap viridis` (`gray`, `viridis`, `magma`, `turbo`). `--time N` is a timestep index. The place: `--lonlat LON,LAT` (degrees, longitude first; a projected store needs the `geo` extra) or `--xy X,Y` (the store's own coordinates), with `--zoom Z` in screen pixels per stored pixel (1 draws each stored pixel as one screen pixel; a centre without `--zoom` gets 1). A point outside the store is refused with the store's extent. Needs an http(s) URL; it does not check CORS (run `doctor`) |
| GeoTIFFs for QGIS or GDAL | `chronozarr export-cog STORE OUT_DIR [--level N] [--times 2024-03] [--physical]` |
| A STAC catalog | `chronozarr stac STORE --out DIR [--href URL]` |
| Bands the viewer's products do not find (True color, NDVI, ...) | `chronozarr bands STORE`, then `chronozarr bands STORE --band-role b4=red,b8=nir` (also `--band-role` on `convert` and `encode`) |
| A private store | Read `/guides/private.md`; a viewer link does not gate access |
| The viewer in a web page | `/guides/embedding.md`; self-hosted viewer: `/guides/viewer-distribution.md` |
| An error message | `/troubleshooting.md` |

## Read in Python

```python
import xarray as xr
import chronozarr

ds = xr.open_dataset("my_store", engine="chronozarr")                   # float32 physical values, NaN where invalid
raw = xr.open_dataset("https://host/my_store", engine="chronozarr", physical=False)  # stored values, stored dtype
ds0 = xr.open_dataset("my_store", engine="chronozarr", lod=2)           # a coarser pyramid level
ds.data.sel(band="B08").isel(time=0)                                    # lazy; reads only the chunks it needs

store = chronozarr.open_store("my_store")      # path or http(s) URL
store.read(t=0, lod=0)                         # (band, y, x), stored values
store.physical(t=0, lod=0)                     # float32, scale and offset applied
store.read_mask(t=0, lod=0)                    # None when the store has no mask
```

Write with `chronozarr.encode(da, out, crs=...)`, or `from chronozarr.convert import convert` for files. Its options follow the CLI with Python names: `dry_run`, `write_manifest_to`, `mask_var`, `band_roles` (a mapping, for example `{"b8": "nir"}`); encoder options such as `n_lods` and `shard` pass through as keywords. API reference: `/reference/python.md`.

## Traps

Each is confirmed in the source or docs.

- **Plain `xr.open_zarr(store)` on the root does not fail.** It returns an empty Dataset with the metadata as attrs, because the data lives in the level groups. Use the engine (`xr.open_dataset(path, engine="chronozarr")`), or open one level: `xr.open_zarr(path, group="0", zarr_format=3, chunks=None)`. The engine is required for URLs; auto-detection only works on local directories.
- **Two value modes.** The engine returns float32 physical values with NaN for invalid pixels unless you pass `physical=False`. `store.read` returns stored values. The data arrays carry no CF `scale_factor` or `add_offset`, so a plain Zarr reader sees stored values and applies no scaling. Scale, offset and units are per band in the root attrs `chronozarr.bands`.
- **Validity is a mask, or a nodata sentinel, or nothing.** With a mask, the mask decides and nodata is not compared. Without one, pixels equal to nodata are invalid. `encode` (Python, and CLI `chronozarr encode` of Zarr or NetCDF) defaults nodata to 0 for uint8 and uint16 (None for int16 and float32, and None when a mask is given), so a stored 0 is invalid in a store made from a bare uint16 DataArray. If 0 is data: `nodata=None` in Python, or `chronozarr convert SRC OUT --variable NAME --nodata none` on the command line. `convert` takes nodata from the sources, and no declared nodata means no nodata, but it refuses an undeclared value that looks like a fill (-9999 in rainfall) and holds a large share of the sampled pixels: pass `--nodata -9999` to mark it invalid or `--nodata none` to keep it as data. NaN is never stored.
- **Not Web Mercator.** The CRS is the native one of the data: an EPSG code, or WKT2 text for a CRS that has none, such as a sinusoidal grid (see `chronozarr info`); `x` and `y` are projected pixel centres. Do not reproject to EPSG:3857 before encoding. The layout uses the `multiscales`, `proj` and `spatial` Zarr conventions.
- **Time is stored as int64 Unix milliseconds** (`time` array, CF units `milliseconds since 1970-01-01T00:00:00`). `open_store(...).times` is `datetime64[ms]`; the xarray engine gives `datetime64[ns]`.
- **`shard_time` without `shard` is an error** (`--shard-time` needs `--shard`). Stores are unsharded by default, one object per chunk; sharding is opt-in and a CDN cache miss then costs a whole shard.
- **`append` takes a chronozarr store, a Zarr/NetCDF file, one GeoTIFF, or a quoted glob of GeoTIFFs** (date in each file name). Package 0.4.1 reads GeoTIFFs only through a glob, so there quote a pattern that matches the one file, such as `'scenes/2024-07*.tif'`. The new times must come after the last one, on the same grid, bands, dtype, CRS and nodata. It edits the store in place and is not atomic: work on a copy and run `chronozarr validate` before publishing.
- **`convert` stops on any mismatch** (dates, grid, CRS, bands, dtype, scale, offset, units, nodata) and reports all of them per file. Nothing is resampled or rescaled unless you pass `--resampling`, `--scale` or `--offset` (`--scale` and `--offset` replace the scale and offset of every band). A scale of 100, 1000, 10000 with no offset that gives values past 1e6 is refused, as for MODIS NDVI, which declares 10000 and means stored / 10000: pass `--scale 0.0001`, or `--scale 10000` to keep it. Run `--dry-run` first.
- **Hosting requirements** (`/guides/hosting-requirements.md`): an absent key returns 404 (not an HTML fallback page); chunk bytes are served unchanged (no `Content-Encoding`); `Access-Control-Allow-Origin: *`; `Access-Control-Expose-Headers` lists `Content-Range` (`doctor` fails without it); sharded stores need `206` range responses; `Cache-Control: public, max-age=31536000, immutable` on immutable objects (`doctor` only advises). `chronozarr doctor` checks all of these except 404 behavior and `Content-Encoding`.
- **Never edit a published prefix in place.** Re-encode to a new prefix and switch the URL. The only in-place growth is `append` followed by `chronozarr publish --update`.
- **Upload order: chunks, then metadata below the root, then the root `zarr.json` last.** A cached 404 on the root makes a finished store look absent. `publish` does this for you; for a manual upload, follow `/guides/hosting-providers.md#upload-without-chronozarr-publish`.
- **Metadata-only changes do not propagate by themselves.** `chronozarr bands --band-role` rewrites only the local root `zarr.json` and its consolidated metadata. For a hosted copy, upload the root `zarr.json` again and purge the CDN copy. Assign roles before first publish when possible.
- **The viewer link is not access control.** The viewer reads the store directly from `?store=<url>` in the browser; anyone who can fetch the URL can read the store. A presigned URL signs one object and does not work for a whole store. See `/guides/private.md`.
- **`chronozarr share` is public while it runs.** Do not use it for private data.
- **The first view (product, band, range, colormap, timestep, centre, zoom) lives in the viewer URL, not the store.** Use `chronozarr link`, which validates each value against the store; the viewer silently ignores values the store cannot honor. A store link without `t=` opens at the first timestep.

## Quick end to end

```bash
chronozarr convert scenes/ my_store --dry-run     # check every file, print size estimate
chronozarr convert scenes/ my_store
chronozarr validate my_store && chronozarr info my_store
chronozarr share my_store                         # temporary public link, needs cloudflared; Ctrl-C stops it
chronozarr preview my_store                       # or: this computer only, no tunnel
chronozarr publish my_store --destination s3://BUCKET/aoi/store-v1 --dry-run
```

When a command or option is uncertain, run `chronozarr <command> --help` instead of guessing. The format rules are in `/specification.md`; every command is in `/reference/cli.md`.
