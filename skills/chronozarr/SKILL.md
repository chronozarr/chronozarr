---
name: chronozarr
description: Create, open, inspect, append to, host and view chronozarr stores, a Zarr v3 layout for satellite and raster image time series with true stored values. Use when the user mentions chronozarr, a Zarr raster time series or satellite image time series store, converting dated GeoTIFFs, COGs, NetCDF or Zarr into a store, opening a chronozarr URL in xarray (engine="chronozarr"), hosting a store on S3, R2 or GCS, running chronozarr doctor, or the chronozarr viewer at chronozarr.org/demo.
---

# chronozarr

chronozarr is a Zarr v3 layout convention for raster time series, with a Python package (CLI `chronozarr`, library `chronozarr`) and a browser viewer.

- A store holds the true stored values (uint8, uint16, int16 or float32), never quantized. Physical values are `stored * scale + offset`, computed at read time.
- The root group has level groups `"0"`, `"1"`, ... (a pyramid, level 0 is native). Each has a `data` array with dims `(time, band, y, x)` and coordinates `time`, `band`, `y`, `x`.
- The grid is the native projected CRS (for example UTM), north up. Stored data is not Web Mercator.
- A store is static files. Hosting needs no server: a bucket or CDN with CORS is enough.

Depth: https://chronozarr.org/llms-full.txt (all docs), or one page as Markdown at `https://chronozarr.org/<route>.md`: `/getting-started.md`, `/specification.md`, `/reference/python.md`, `/guides/hosting.md`, `/guides/append.md`, `/guides/private.md`, `/guides/embedding.md`, `/guides/viewer-distribution.md`, `/guides/bring-your-data.md`, `/guides/format-comparison.md`, `/guides/png-frames.md`.

## Install

The PyPI package is `chronozarr`. Python 3.11+. Prefer uv.

```bash
uv add 'chronozarr[geo]'            # in a project: library and CLI; `uv run chronozarr ...`
uv tool install 'chronozarr[geo]'   # CLI only
uvx --from 'chronozarr[geo]' chronozarr --help   # one-off
```

Extras: `geo` (rasterio: GeoTIFF/COG input, `export-cog`, `stac`), `netcdf`, `s3` (list an `s3://` prefix in `convert`), `dask` (`chunks=` in xarray), `notebook` (`chronozarr.view`), `publish` / `publish-gcs` / `publish-azure` (`chronozarr publish`), `leafmap`. With pip: `pip install 'chronozarr[geo]'`. Run `chronozarr <command> --help` for exact options.

## Which command

| The user has | Do this |
|---|---|
| A directory, quoted glob or `s3://` prefix of dated GeoTIFFs | `chronozarr convert SRC OUT --dry-run`, fix what it reports, then run without `--dry-run`. Date comes from the file name (`YYYYMMDD`, `YYYY-MM-DD`, `YYYY-MM`, exactly one per name); otherwise `--date-pattern 'ndvi_%Y%m%d'`. A directory is not recursive; use `'dir/**/*.tif'` |
| COG or PNG frames at URLs, or files not on one grid | A manifest (`.csv` with `uri,datetime[,bands]`, or `.json`) passed to `convert`; `--resampling` is required for sources off the target grid. `--write-manifest` saves what a directory scan found |
| A NetCDF file or a Zarr array | `chronozarr convert SRC OUT --variable NAME` (`--dims` for unusual dim names). `chronozarr encode` is the in-memory shortcut |
| An xarray DataArray in memory | `chronozarr.encode(da, "OUT", crs="EPSG:32631")`; `da` has dims `(time, band, y, x)` |
| A store path or URL to analyze | `xr.open_dataset(path_or_url, engine="chronozarr")`, or `chronozarr.open_store(path_or_url)` |
| A store to inspect or check | `chronozarr info STORE`, `chronozarr validate STORE` (exit 1 if it does not conform) |
| New timesteps for an existing store | `chronozarr append STORE INPUT` (in place; see traps) |
| A store to publish | Read `/guides/hosting.md`. `chronozarr publish STORE --destination s3://BUCKET/PREFIX [--public-url URL]` (`gs://`, `az://` too), then `chronozarr doctor <store-url>` |
| A store already hosted that needs checking | `chronozarr doctor https://host/prefix` (CORS, byte ranges, caching, decode) |
| To look at a store | Hosted: `https://chronozarr.org/demo/?store=<store-url>`. Local: `chronozarr preview STORE`. Temporary public link for a colleague: `chronozarr share STORE` (needs `cloudflared`; public while it runs) |
| A Jupyter notebook | `chronozarr.view("STORE")` or `chronozarr.player("STORE")` (needs the `notebook` extra) |
| To choose the first view of a hosted store | `chronozarr link STORE_URL --product ndvi` (also `--band`, `--range LOW,HIGH`, `--time`) |
| GeoTIFFs for QGIS or GDAL | `chronozarr export-cog STORE OUT_DIR [--level N] [--times 2024-03] [--physical]` |
| A STAC catalog | `chronozarr stac STORE --out DIR [--href URL]` |
| Bands the viewer's products do not find (True color, NDVI, ...) | `chronozarr bands STORE`, then `chronozarr bands STORE --band-role b4=red,b8=nir` (also `--band-role` on `convert` and `encode`) |
| A private store | Read `/guides/private.md`; a viewer link does not gate access |
| The viewer in a web page | `/guides/embedding.md`; self-hosted viewer: `/guides/viewer-distribution.md` |

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

Write with `chronozarr.encode(da, out, crs=...)`, or `from chronozarr.convert import convert` for files (same options as the CLI, for example `band_roles={"b8": "nir"}`). API reference: `/reference/python.md`.

## Traps

Each is confirmed in the source or docs.

- **Plain `xr.open_zarr(store)` on the root does not fail.** It returns an empty Dataset with the metadata as attrs, because the data lives in the level groups. Use the engine (`xr.open_dataset(path, engine="chronozarr")`), or open one level: `xr.open_zarr(path, group="0", zarr_format=3, chunks=None)`. The engine is required for URLs; auto-detection only works on local directories.
- **Two value modes.** The engine returns float32 physical values with NaN for invalid pixels unless you pass `physical=False`. `store.read` returns stored values. The data arrays carry no CF `scale_factor` or `add_offset`, so a plain Zarr reader sees stored values and applies no scaling. Scale, offset and units are per band in the root attrs `chronozarr.bands`.
- **Validity is a mask, or a nodata sentinel, or nothing.** With a mask, the mask decides and nodata is not compared. Without one, pixels equal to nodata are invalid. `encode` defaults nodata to 0 for uint8 and uint16 (None for int16 and float32), so a stored 0 is invalid in a store made from a bare uint16 DataArray; pass `nodata=None` if 0 is data. `convert` takes nodata from the sources, and no declared nodata means no nodata. NaN is never stored.
- **Not Web Mercator.** The CRS is the native EPSG code (see `chronozarr info`); `x` and `y` are projected pixel centres. Do not reproject to EPSG:3857 before encoding. The layout uses the `multiscales`, `proj` and `spatial` Zarr conventions.
- **Time is stored as int64 Unix milliseconds** (`time` array, CF units `milliseconds since 1970-01-01T00:00:00`). `open_store(...).times` is `datetime64[ms]`; the xarray engine gives `datetime64[ns]`.
- **`shard_time` without `shard` is an error** (`--shard-time` needs `--shard`). Stores are unsharded by default, one object per chunk; sharding is opt-in and a CDN cache miss then costs a whole shard.
- **`append` takes a store, a Zarr/NetCDF file, or a quoted glob.** A single GeoTIFF path is read as a Zarr store and fails with "No group found"; write a wildcard, for example `'scenes/s2_2024-03.t*'`. The new times must come after the last one, on the same grid, bands, dtype, CRS and nodata. It edits the store in place and is not atomic: work on a copy and run `chronozarr validate` before publishing.
- **`convert` stops on any mismatch** (dates, grid, CRS, bands, dtype, scale, offset, units, nodata) and reports all of them per file. Nothing is resampled or rescaled unless you pass `--resampling`. Run `--dry-run` first.
- **Hosting requirements** (`/guides/hosting.md`): an absent key returns 404 (not an HTML fallback page); chunk bytes are served unchanged (no `Content-Encoding`); `Access-Control-Allow-Origin: *`; `Access-Control-Expose-Headers` lists `Content-Range`; sharded stores need `206` range responses; `Cache-Control: public, max-age=31536000, immutable` on immutable objects. `chronozarr doctor` checks all of these except 404 behavior and `Content-Encoding`.
- **Never edit a published prefix in place.** Re-encode to a new prefix and switch the URL. The only in-place growth is `append` followed by `chronozarr publish --update`.
- **Upload order: chunks, then metadata below the root, then the root `zarr.json` last.** A cached 404 on the root makes a finished store look absent. `publish` does this for you; for a manual upload, follow `/guides/hosting.md` section 2.
- **Metadata-only changes do not propagate by themselves.** `chronozarr bands --band-role` rewrites only the local root `zarr.json` and its consolidated metadata. For a hosted copy, upload the root `zarr.json` again and purge the CDN copy. Assign roles before first publish when possible.
- **The viewer link is not access control.** The viewer reads the store directly from `?store=<url>` in the browser; anyone who can fetch the URL can read the store. A presigned URL signs one object and does not work for a whole store. See `/guides/private.md`.
- **`chronozarr share` is public while it runs.** Do not use it for private data.
- **The first view (product, band, range, timestep) lives in the viewer URL, not the store.** Use `chronozarr link`, which validates each value against the store; the viewer silently ignores values the store cannot honor.

## Quick end to end

```bash
chronozarr convert scenes/ my_store --dry-run     # check every file, print size estimate
chronozarr convert scenes/ my_store
chronozarr validate my_store && chronozarr info my_store
chronozarr preview my_store                       # opens the viewer on a local server
chronozarr publish my_store --destination s3://BUCKET/aoi/store-v1 --dry-run
```

When a command or option is uncertain, run `chronozarr <command> --help` instead of guessing. The format rules are in `/specification.md`.
