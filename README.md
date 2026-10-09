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

Install the package with GeoTIFF support. It needs Python 3.11 or later.

```bash
pip install "chronozarr[geo]"
```

### Without data

1. Save this script as `quickstart.py`. It writes a store of three synthetic timesteps and reads one back. The pixels are 10 m in UTM zone 31N.

   ```python
   import numpy as np
   import xarray as xr
   import chronozarr

   values = np.arange(3 * 64 * 64, dtype=np.uint16).reshape(3, 1, 64, 64)
   da = xr.DataArray(
       values,
       dims=("time", "band", "y", "x"),
       coords={
           "time": np.array(["2024-01-01", "2024-02-01", "2024-03-01"], dtype="datetime64[ns]"),
           "band": ["example"],
           "y": 5000000 - (np.arange(64) + 0.5) * 10,
           "x": 500000 + (np.arange(64) + 0.5) * 10,
       },
   )
   chronozarr.encode(da, "synthetic_store", crs="EPSG:32631", nodata=None)

   store = chronozarr.open_store("synthetic_store")
   print((store.read(t=1) == values[1]).all())
   ```

2. Run the script. It prints `True`: level 0 returns the values that you wrote.

   ```bash
   python quickstart.py
   ```

3. Check the store.

   ```bash
   chronozarr validate synthetic_store
   chronozarr info synthetic_store
   ```

### With your data

1. Write a store from your rasters. `convert` is the command for files on disk or in S3. Point it at a directory, a quoted glob or an `s3://` prefix of GeoTIFFs with the date in each file name (`20240131`, `2024-01-31` or `2024-01`):

   ```bash
   chronozarr convert "scenes/*.tif" my_store --dry-run   # list files and dates, check every file, write nothing
   chronozarr convert "scenes/*.tif" my_store
   ```

   The dry run prints each file with its date, then the grid, bands, validity and size of the store. A directory or `s3://` prefix is not searched recursively; use a glob such as `scenes/**/*.tif` for subdirectories. Listing an S3 prefix needs `pip install "chronozarr[s3]"` and uses your AWS credentials.

   A date is read from a name only when the name holds exactly one. A name with no date, with several (`20240215_2024-03`), or two files with the same date are reported, never guessed. `--date-pattern` says where the date is, for example `--date-pattern "ndvi_%Y%m%d"`, and `--write-manifest found.csv` saves the files and dates as a manifest that `convert` reads back.

   Before it reads any pixels in bulk, `convert` checks every file for its date, grid and CRS, bands, dtype, scale, offset, units and nodata. It reports all problems at once, grouped by file, each with a suggested fix, and writes nothing:

   ```text
   2 problem(s) in 2 of 24 source file(s); nothing was written:

   scenes/ndvi_2024-03.tif
     - has scales [0.5, 1.0]; scenes/ndvi_2024-01.tif has [1.0, 1.0]. A store holds one value per band for every timestep, so the sources must agree
       fix: if the metadata is wrong, correct it (gdal_edit.py -scale -offset -units); ...

   scenes/readme_copy.tif
     - no date in the file name
       fix: name the date YYYYMMDD, YYYY-MM-DD or YYYY-MM, or pass --date-pattern ...
   ```

   Nothing is resampled or rescaled unless you ask. Files on another grid fail with a suggestion to pass `--resampling`.

   The store takes each band's description (its name), scale, offset and units from the files, so a Sentinel-2 file with a scale of 0.0001 gives reflectance. These must be identical in every file. It also takes the nodata value that the files declare; files that declare none give a store with no nodata, so a stored 0 is data. `chronozarr convert --help` lists the manifest, Zarr and NetCDF sources and the validity rules.

   `chronozarr encode "scenes/*.tif" my_store` is the in-memory route for GeoTIFFs on one grid. It reads uint8, uint16, int16 and float32 files by the same rules, holds the whole stack in memory and never resamples. It points you to `convert` for directories, S3 prefixes and manifests.

2. Check the store against the spec.

   ```bash
   chronozarr validate my_store
   ```

3. Read the values in Python.

   ```python
   import chronozarr

   store = chronozarr.open_store("my_store")
   store.read(t=42)              # (band, y, x), exact stored values
   da = store.to_xarray(lod=0)   # xarray DataArray, loaded into memory
   ```

4. Look at the store on your machine before you upload it.

   ```bash
   chronozarr preview my_store
   ```

   The command serves the store on `127.0.0.1`, opens the viewer in your browser and stops on Ctrl-C. See [docs/python.md](docs/python.md#preview-from-the-command-line).

5. Upload `my_store` to a static host. [docs/hosting.md](docs/hosting.md) has recipes for S3 with CloudFront, Cloudflare R2, Google Cloud Storage and Source Cooperative.

6. Check the host.

   ```bash
   chronozarr doctor https://your-host/my_store
   ```

7. Open the store in the hosted viewer:

   ```
   https://chronozarr.org/demo/?store=https://your-host/my_store
   ```

The input must be on an EPSG grid with north up. To write a store from an xarray `DataArray`, call `chronozarr.encode` as in the script above.

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
| `doctor TARGET` | Checks a hosted URL or a local store. See [docs/hosting.md](docs/hosting.md) |
| `publish STORE --destination s3://BUCKET/PREFIX` | Uploads a store to S3 or R2, checks the hosted store and prints a viewer link. `--update` publishes appended timesteps to the same prefix and link. Needs the `publish` extra. See [docs/hosting.md](docs/hosting.md#chronozarr-publish) |
| `export-cog STORE OUT_DIR` | Writes true-value COGs for GDAL and QGIS. Needs the `geo` extra |
| `stac STORE --out DIR` | Writes a static STAC Collection and Item. Needs the `geo` extra |

Other extras: `notebook` adds `chronozarr.view(store)`, which shows a local store in Jupyter. `netcdf` and `dask` add those inputs. `s3` adds listing of an `s3://` prefix for `convert`.

## JavaScript

```bash
npm install chronozarr
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

The spec is v0.3.0, a draft. The packages are 0.3.1 on PyPI and npm. Readers open only v0.3 stores. To use a v0.2 store, convert it:

```bash
chronozarr convert OLD_STORE NEW_STORE
```

## License

See [LICENSE](LICENSE).
