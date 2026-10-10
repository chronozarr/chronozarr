# chronozarr

[![CI](https://github.com/chronozarr/chronozarr/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/chronozarr/chronozarr/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/chronozarr)](https://pypi.org/project/chronozarr/)
[![npm](https://img.shields.io/npm/v/chronozarr)](https://www.npmjs.com/package/chronozarr)

chronozarr turns a raster time series into static files that you can put in a storage bucket. Anyone with the link can then open the series in a web browser and play or scrub through it like a video. Clicking a pixel shows its values over time. The same files open in Python with the exact values you wrote.

It is for data that is hard to share today: years of monthly satellite composites, model output, or any stack of georeferenced rasters on one grid. The usual choices are to send the files, which can run to many gigabytes, or to run a tile server. A tile server has to be kept running, and it usually sends the browser images. chronozarr needs no server, and the browser receives the values themselves.

[Live demo](https://chronozarr.org/demo/): 117 monthly Sentinel-2 composites (2015 to 2026) of the Ucayali River in Peru, read directly from a bucket.

## How it works

A chronozarr store is a Zarr v3 group arranged so that a browser can read it over plain HTTP.

The map is divided into square cells, 512 by 512 pixels by default. Each chunk holds one cell at one timestep, with all of its bands. To show a view, the browser downloads only the chunks for the visible cells at the current timestep.

The store also holds a pyramid. Level 0 holds your values unchanged. Each coarser level is the mean of 2 by 2 blocks of the level below it, at half the resolution. When you zoom out, the viewer switches to a coarser level, so a view of the whole area still needs only a few chunks.

```text
my_store/
  zarr.json                root metadata
  0/                       level 0, the original resolution
    data/                  (time, band, y, x)
    time/ band/ y/ x/      coordinates
    mask/ coverage/        optional
  1/ 2/ ...                coarser levels
```

The chunks hold the stored numbers in their original type: uint8, uint16, int16 or float32. A scale and offset per band give physical units. An optional mask marks invalid pixels. An optional coverage plane counts the valid observations behind each pixel. The viewer draws these numbers on the GPU and computes products such as true color or NDVI there. It also downloads the timesteps around the current one in the background, so stepping through time usually needs no new download.

The layout follows the Zarr conventions for pyramids (`multiscales`), coordinate systems (`proj`) and georeferencing (`spatial`), all at v0.1. This is why other tools can read a store without chronozarr installed: xarray, GDAL 3.13, and CarbonPlan's zarr-layer for MapLibre.

To publish a store, upload it to a static host that sends CORS headers. A sharded store also needs byte-range requests. The demo store is on Cloudflare R2, and there is no server code to run.

## Quickstart

Install the released Python package with GeoTIFF support. It needs Python 3.11 or later.

```bash
python -m pip install --upgrade "chronozarr[geo]==0.4.0"
```

`0.4.0` is the Python package release. It reads and writes the unchanged chronozarr v0.3 store format.

### 1. Put dated GeoTIFFs in a directory

Use one GeoTIFF per timestep, with exactly one date in each filename. For example:

```text
scenes/
  ndvi_2024-01.tif
  ndvi_2024-02.tif
  ndvi_2024-03.tif
```

The files must share a north-up grid: CRS, width and height, pixel size and transform, band count and names, dtype, scale, offset, units and nodata metadata. Names can contain a date as `20240131`, `2024-01-31` or `2024-01`. If a filename uses another pattern, pass `--date-pattern` to `convert`.

### 2. Convert and check the store

Point `convert` at the directory or a quoted glob. A dry run is optional, but useful before writing:

```bash
chronozarr convert "scenes/*.tif" my_store --dry-run
chronozarr convert "scenes/*.tif" my_store
chronozarr validate my_store
chronozarr info my_store
```

The dry run checks every input and writes nothing. `convert` preserves the source values and metadata; it does not resample or rescale unless you request it. A directory or `s3://` prefix is not recursive, so use a glob such as `scenes/**/*.tif` for subdirectories. S3 listing needs the `s3` extra.

### 3. Open and share the first view

Preview the store locally:

```bash
chronozarr preview my_store
```

The command serves the store on `127.0.0.1` and opens the viewer. Press Ctrl-C to stop the preview before starting another command in the same terminal. For a temporary share from your laptop, run:

```bash
chronozarr share my_store
```

This needs `cloudflared`. Keep the terminal and laptop running while the recipient uses the link; the requested store data is transferred from your laptop to the recipient's browser. In the viewer, choose the intended date and view, then use `Copy link` to share that state. The recipient only needs a modern browser. See the [sharing walkthrough](docs/python.md#share-a-store-through-a-tunnel) for checks and limits.

For durable hosting, upload the store to a static host, run `chronozarr doctor https://your-host/my_store`, and use `chronozarr link https://your-host/my_store`. See the [hosting guide](docs/hosting.md). The [Python guide](docs/python.md) covers the API and notebook viewer.

Advanced sources and workflows are documented in `chronozarr convert --help`, the [bring-your-data example](examples/bring_your_data/README.md), and the [PNG georeferencing guide](docs/png-frames.md).

## Read a store without chronozarr

xarray reads each level as a plain Zarr group:

```python
import xarray as xr

ds = xr.open_zarr("my_store", group="0", zarr_format=3, chunks=None)
```

GDAL 3.13 reads the CRS, the georeferencing and the values. It attaches the coarser levels as overviews when you open the data array as `ZARR:"my_store":/0/data`. A single time-slice subdataset shows no overviews. To get Cloud Optimized GeoTIFFs for older GDAL or QGIS, run `chronozarr export-cog my_store out_dir`.

## Commands

Run `chronozarr <command> --help` for every option.

| Command | What it does |
|---------|--------------|
| `convert SOURCE OUT` | The command for existing raster files. Writes a store one timestep at a time from a directory, quoted glob or `s3://` prefix of dated GeoTIFFs, a manifest of COGs or PNG frames, a Zarr store or a NetCDF file. `--dry-run` checks every file and reports all problems per file. Also converts a v0.2 store to v0.3 |
| `encode INPUT OUT` | Writes a store in memory from a Zarr store, a NetCDF file or a quoted glob of GeoTIFFs. Points to `convert` for anything else |
| `append STORE INPUT` | Adds timesteps at the end of a store. See [docs/append.md](docs/append.md) |
| `validate STORE` | Checks a store against the spec. Exits with status 1 on failure |
| `info STORE` | Prints the times, bands and levels of a store |
| `bands STORE` | Lists the bands, the role each plays and the viewer products they allow. `--band-role B04=red` sets a band's `common_name`. `encode` and `convert` take the same flag. See [docs/python.md](docs/python.md#band-roles-and-the-first-view) |
| `link STORE_URL` | Prints a viewer URL with an initial product, band, display limits and timestep, checked against the hosted store |

| `preview STORE` | Serves a local store on `127.0.0.1` and opens it in the viewer. Ctrl-C stops it |
| `share STORE` | Starts a disposable Cloudflare quick tunnel for a local store, doctor-checks the public route, and prints a viewer link. Needs `cloudflared`; Ctrl-C stops both processes |
| `doctor TARGET` | Checks a hosted URL or a local store. See [docs/hosting.md](docs/hosting.md) |
| `publish STORE --destination s3://BUCKET/PREFIX` | Uploads a store to S3 or R2; `gs://` and `az://` destinations publish to Google Cloud Storage and Azure Blob Storage. It checks the hosted store and prints a viewer link. `--update` publishes eligible appended timesteps to the same prefix and link. Install the matching `publish` extra. See [docs/hosting.md](docs/hosting.md#chronozarr-publish) |
| `export-cog STORE OUT_DIR` | Writes true-value COGs for GDAL and QGIS. Needs the `geo` extra |
| `stac STORE --out DIR` | Writes a static STAC Collection and Item. Needs the `geo` extra |

Other extras: `notebook` adds `chronozarr.view(store)`, which shows a local store in Jupyter. `netcdf` and `dask` add those inputs. `s3` adds listing of an `s3://` prefix for `convert`.

## JavaScript

```bash
npm install chronozarr@0.4.0
```

The package has four parts:

- `chronozarr` is a DOM-free reader. It returns one cell of one timestep as a typed array.
- `chronozarr/maplibre` is a MapLibre custom layer that draws a store.
- `chronozarr/geolibre` is a GeoLibre plugin that adds a store as a MapLibre layer.
- `chronozarr-viewer` copies the full viewer into a folder that you can host next to your store. See [docs/viewer-distribution.md](docs/viewer-distribution.md).

There is no build step and no runtime dependency. Examples are in [js/README.md](js/README.md).

## Documentation

| Topic | Document |
|-------|----------|
| Format rules | [spec/CHRONOZARR.md](spec/CHRONOZARR.md) |
| Python functions | [docs/python.md](docs/python.md) |
| Your own data, end to end | [examples/bring_your_data](examples/bring_your_data/README.md) |
| Georeferenced PNG frames | [docs/png-frames.md](docs/png-frames.md) |
| Hosting and the doctor checklist | [docs/hosting.md](docs/hosting.md) |
| Adding timesteps | [docs/append.md](docs/append.md) |
| Embedding the viewer in a page | [docs/embedding.md](docs/embedding.md) |
| Drawing a store on a MapLibre map | [js/maplibre/README.md](js/maplibre/README.md) |
| More examples: Sentinel-2 ingest, water masks, notebooks, SWOT, NISAR | [examples](examples/) |
| Comparison with PMTiles, Mapbox raster-array and zarr-layer | [docs/format-comparison.md](docs/format-comparison.md) |
| Measurements and tested reader versions | [docs/evidence.md](docs/evidence.md) |

## Status

The released Python and npm packages are 0.4.0. The spec remains v0.3.0, a draft: package upgrades do not change existing v0.3 stores, and readers open v0.3 stores. To use a v0.2 store, convert it:

```bash
chronozarr convert OLD_STORE NEW_STORE
```

## License

See [LICENSE](LICENSE).
