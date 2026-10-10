# Command line

Run `chronozarr <command> --help` for every option.

## Create

| Command | What it does |
|---------|--------------|
| `convert SOURCE OUT` | The command for existing raster files. Writes a store one timestep at a time from a directory, quoted glob or `s3://` prefix of dated GeoTIFFs, a manifest of COGs or PNG frames, a Zarr store or a NetCDF file. `--dry-run` checks every file and reports all problems per file. Also converts a v0.2 store to v0.3 |
| `encode INPUT OUT` | Writes a store in memory from a Zarr store, a NetCDF file or a quoted glob of GeoTIFFs. Points to `convert` for anything else |
| `append STORE INPUT` | Adds timesteps at the end of a store. See [Append timesteps](append.md) |
| `bands STORE` | Lists the bands, the role each plays and the viewer products they allow. `--band-role B04=red` sets a band's `common_name`. `encode` and `convert` take the same flag. See [Band roles](python.md#band-roles-and-the-first-view) |

## Check

| Command | What it does |
|---------|--------------|
| `validate STORE` | Checks a store against the spec. Exits with status 1 on failure |
| `info STORE` | Prints the times, bands and levels of a store |
| `doctor TARGET` | Checks a hosted URL or a local store. See [hosting.md](hosting.md#1-checklist) |

## Explore and share

| Command | What it does |
|---------|--------------|
| `preview STORE` | Serves a local store on `127.0.0.1` and opens it in the viewer. Ctrl-C stops it. See [Preview a store](preview.md) |
| `share STORE` | Starts a disposable Cloudflare quick tunnel for a local store, doctor-checks the public route, and prints a viewer link. Needs `cloudflared`; Ctrl-C stops both processes. See [Share a local store instantly](share.md) |
| `publish STORE --destination s3://BUCKET/PREFIX` | Uploads a store to S3 or R2; `gs://` and `az://` destinations publish to Google Cloud Storage and Azure Blob Storage. It checks the hosted store and prints a viewer link. `--update` publishes eligible appended timesteps to the same prefix and link. Install the matching `publish` extra. See [Publish with the command](hosting.md#chronozarr-publish) |
| `link STORE_URL` | Prints a viewer URL with an initial product, band, display limits and timestep, checked against the hosted store |

## Export

| Command | What it does |
|---------|--------------|
| `export-cog STORE OUT_DIR` | Writes true-value COGs for GDAL and QGIS. Needs the `geo` extra |
| `stac STORE --out DIR` | Writes a static STAC Collection and Item. Needs the `geo` extra |

## Extras

| Extra | Adds |
|-------|------|
| `geo` | GeoTIFF input and output |
| `notebook` | `chronozarr.view(store)` for Jupyter. See [notebooks](notebooks.md) |
| `netcdf`, `dask` | NetCDF input and dask arrays |
| `s3` | Listing of an `s3://` prefix for `convert` |
| `publish`, `publish-gcs`, `publish-azure` | The SDK that `publish` uses for S3 or R2, Google Cloud Storage or Azure. See [Publish with the command](hosting.md#chronozarr-publish) |
