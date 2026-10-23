# Command line

**Local release candidate — 0.5.0rc1, unpublished.** Follow the [candidate installation](development.md) before using this reference. For the released package, use the [0.4.1 command line reference](https://github.com/chronozarr/chronozarr/blob/v0.4.1/docs/cli.md).

Run `chronozarr <command> --help` for every option. In a uv project, run `uv run chronozarr <command> --help`. A standalone tool or pip installation runs `chronozarr` directly.

## Create

| Command | What it does |
|---------|--------------|
| `convert SOURCE OUT` | The command for existing raster files. Writes a store one timestep at a time from a directory, quoted glob or `s3://` prefix of dated rasters (GeoTIFF, BIL, GRIB2, HDF5, zips of one raster), a manifest of COGs or PNG frames, a Zarr store or a NetCDF file. `--dry-run` checks every file and reports all problems per file. Also converts a v0.2 store to v0.3 |
| `encode INPUT OUT` | Writes a store in memory from a Zarr store, a NetCDF file or a quoted glob of GeoTIFFs. Points to `convert` for anything else |
| `append STORE INPUT` | Adds timesteps at the end of a store. See [Append timesteps](append.md) |
| `bands STORE` | Lists the bands, the role each plays and the viewer products they allow. `--band-role B04=red` sets a band's `common_name`. `encode` and `convert` take the same flag. See [Band roles](python.md#band-roles-and-the-first-view) |

## Check

| Command | What it does |
|---------|--------------|
| `validate STORE` | Checks a store against the spec. Exits with status 1 on failure. It prints a `note:` for each SHOULD the store leaves out, which does not fail it |
| `info STORE` | Prints the times, bands and levels of a store, and a `note:` for each deviation the reader read through |
| `doctor TARGET` | Checks a hosted URL or a local store. See [hosting-requirements.md](hosting-requirements.md#checklist) |

## Explore and share

| Command | What it does |
|---------|--------------|
| `share STORE` | Starts a disposable Cloudflare quick tunnel for a local store, doctor-checks the public route, and prints a viewer link that opens on any computer. Needs `cloudflared`; Ctrl-C stops both processes. See [Share a temporary link](share.md) |
| `preview STORE` | Serves a local store on `127.0.0.1` and opens it in the viewer. Its link opens only on this computer. Ctrl-C stops it. See [Preview a store](preview.md) |
| `publish STORE --destination s3://BUCKET/PREFIX` | Uploads a store to S3 or R2; `gs://` and `az://` destinations publish to Google Cloud Storage and Azure Blob Storage. It checks the hosted store and prints a viewer link. `--update` publishes eligible appended timesteps to the same prefix and link. Install the matching `publish` extra. See [Publish a permanent link](publish.md) |
| `link STORE_URL` | Prints a viewer URL with an initial product, band, display limits, colormap, timestep, centre and zoom, checked against the hosted store. The centre is a longitude and latitude (`--lonlat`) or a point in the store's coordinates (`--xy`). See [Choose a date and a place](viewer.md#choose-a-date-and-a-place) |

## Export

| Command | What it does |
|---------|--------------|
| `export-cog STORE OUT_DIR` | Writes true-value COGs for GDAL and QGIS. Needs the `geo` extra |
| `stac STORE --out DIR` | Writes a static STAC Collection and Item. Needs the `geo` extra |

## Extras

| Extra | Adds |
|-------|------|
| `geo` | GeoTIFF input and output, and `link --lonlat` for projected coordinates or stored WKT2/PROJJSON definitions |
| `notebook` | `chronozarr.view(store)` for Jupyter. See [notebooks](notebooks.md) |
| `netcdf`, `dask` | NetCDF input (with cftime) and dask arrays |
| `cftime` | Zarr time axes on a `360_day`, `noleap`, `all_leap` or `julian` calendar, without a NetCDF reader |
| `s3` | Listing of an `s3://` prefix for `convert` |
| `publish`, `publish-gcs`, `publish-azure` | The SDK that `publish` uses for S3 or R2, Google Cloud Storage or Azure. See [Publish a permanent link](publish.md) |

`link --lonlat` needs no projection library for a store that declares only `EPSG:4326`. A stored WKT2 or PROJJSON definition takes precedence over its authority code; interpreting it or proving it equivalent to that code needs pyproj from the `geo` extra. This includes a geographic store with redundant WKT2. `--xy` and links without a centre remain usable without pyproj.
