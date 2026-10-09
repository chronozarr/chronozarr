# Bring your own raster time series

This example converts a set of georeferenced rasters on one grid into a chronozarr store. It reads the values of one pixel over time in Python. It then builds a static folder that holds the store, a viewer and an embed page. You can upload that folder to any static host.

The folder needs no request to chronozarr.org or to its demo stores. The bundle script copies the JavaScript dependencies, including the GIF encoder, into the folder.

## What you need

- A checkout of this repository, because the bundle script copies the viewer from `js/`.
- uv and Python 3.11 or newer.
- Your observations as GeoTIFFs or COGs, or an existing Zarr or NetCDF time series. Step 2 has a sample if you have none.
- Node.js and Chromium, only for the optional browser check in step 8.

## 1. Install

Run this command from the repository root.

```sh
uv sync --extra geo
```

uv picks an installed Python unless you add `--python`, as in `uv sync --python 3.13 --extra geo`. On Python 3.14 the locked codec dependency may need a source build. On 2026-10-02 this recipe ran from a clean checkout with Python 3.13, a fresh environment and newly downloaded observations.

The converter and reader are also on PyPI as `chronozarr[geo]`. The bundle script still needs this checkout, because it copies the viewer and the vendored JavaScript from `js/`. It needs no npm install and no JavaScript build. To copy the viewer from the npm package instead, see [self-host the packaged viewer](../../docs/viewer-distribution.md).

## 2. Prepare observations

Write a manifest for your own rasters, or download a sample.

### Your own rasters

Write `observations.csv` next to your files:

```csv
uri,datetime
january.tif,2024-01-01
february.tif,2024-02-01
march.tif,2024-03-01
```

Each row names one raster and its date, written in ISO-8601 as in `2024-01-01`. A URI is an HTTP URL or a path relative to the manifest.

For a first run, use rasters on the same north-up grid with the same CRS, bands, dtype, scales and offsets. The converter keeps the band descriptions and the scale and offset metadata of the source rasters, so set them there. Band names such as `red`, `green`, `blue` and `nir` let the viewer offer the matching products, and a single measured variable also works.

### Sample data

If you have no series yet, download three Sentinel-2 acquisitions near Lake Mead from May to July 2020. The download from Microsoft Planetary Computer needs network access but no account and no API key.

```sh
uv sync --extra geo --extra ingest
uv run python examples/bring_your_data/fetch_sample.py /tmp/lake-mead-input
```

The script writes three COGs, `observations.csv` and `source.json` into `/tmp/lake-mead-input`. For each month it picks the acquisition with the lowest scene cloud cover from one tile that covers the whole area. It resamples four bands to one 10 m UTM grid and applies the validity rule of the [Sentinel-2 example](../sentinel2_pc/README.md). Each month is one acquisition, with no compositing and no gap filling. The script stops if an observation has less than 50% valid pixels.

The dates in the manifest are acquisition dates. The file `source.json` lists the scene IDs and acquisition dates that your run used, and it holds no signed URLs. The catalog can reprocess its scenes, so a later run can pick different ones.

In the steps below, use `/tmp/lake-mead-input/observations.csv` in place of `observations.csv`.

## 3. Convert and validate

1. Print the plan. A dry run prints size and time estimates and writes nothing.

   ```sh
   uv run chronozarr convert observations.csv /tmp/my-series --dry-run
   ```

2. Convert the observations into a new output directory.

   ```sh
   uv run chronozarr convert observations.csv /tmp/my-series
   ```

3. Validate the store against the spec.

   ```sh
   uv run chronozarr validate /tmp/my-series
   ```

4. Run doctor on the local store. It checks that every level decodes and matches a plain Zarr read.

   ```sh
   uv run chronozarr doctor /tmp/my-series
   ```

To convert an existing Zarr or NetCDF time series, use its path as the source. Add `--variable NAME` where needed. NetCDF input needs `uv sync --extra geo --extra netcdf`. Run `uv run chronozarr convert --help` for the reprojection and resampling options. Resampling changes values, so use it only when you want that.

For rendered PNG frames, follow the [PNG georeferencing guide](../../docs/png-frames.md). A PNG holds display colors, and the converter stores them unchanged.

## 4. Read a pixel's history

```sh
uv run python examples/bring_your_data/read_series.py /tmp/my-series --row 20 --col 30
```

The script opens the store with the lazy xarray backend and selects one pixel before it reads values. By default it prints physical values, which are the stored values after scale and offset, with NaN for invalid values. To get the stored values, add `physical=False` to `xr.open_dataset`.

The read still needs the spatial chunk that holds the pixel at every date. Over HTTP, a pixel history transfers one whole chunk per date.

## 5. Check the store against your source files (optional)

This step works when the sources are COGs listed in a manifest. The checks read one copy of the store with chronozarr and another with plain xarray and Zarr code, so it needs a second conversion.

1. Convert the manifest a second time.

   ```sh
   uv run chronozarr convert observations.csv /tmp/my-plain
   ```

2. Compare every value with the source COGs.

   ```sh
   uv run python examples/bring_your_data/verify.py observations.csv \
     /tmp/my-series /tmp/my-plain /tmp/my-verification.json
   ```

3. Time full reads of all dates.

   ```sh
   uv run python examples/bring_your_data/compare_local.py observations.csv \
     /tmp/my-series /tmp/my-plain /tmp/my-comparison.json
   ```

`verify.py` checks every level-0 value and mask against the COGs and checks the physical scaling. It also compares a plain xarray and Zarr read of level 0. The JSON report holds the dates, the shape, one pixel history and the size of each representation.

`compare_local.py` reads the level-0 data and masks of all dates with the COG reader, the chronozarr reader and the plain Zarr reader. It uses fresh file handles on warm filesystem caches and rotates the reader order between repeats. The report holds the median, minimum and maximum times. The `--repeats` option sets the number of repeats, which is 5 by default.

## 6. Build the folder and preview it

1. Copy the store and the viewer into a new folder.

   ```sh
   uv run python examples/bring_your_data/bundle.py /tmp/my-series /tmp/my-published-series
   ```

2. Start the preview server.

   ```sh
   uv run python examples/bring_your_data/serve.py /tmp/my-published-series --port 8000
   ```

3. Open the Viewer and Embed URLs that the server prints.

The full viewer opens your store, and the embed page has a slider, a product menu, play and pause buttons and a pixel readout. They control a self-hosted iframe through the `chronozarr:*` messages described in [embedding](../../docs/embedding.md). Neither page uses the hosted demo.

`bundle.py` validates the store and copies it unchanged. It stops if the output folder exists or lies inside the input store. It leaves the source data and the original store untouched. Because it copies the data, allow disk space for a second copy.

The preview server binds to localhost and answers byte-range requests, so use it only for previews.

## 7. Publish to your own host

1. Upload the contents of `/tmp/my-published-series` to a static host and keep the directory structure. The folder works at a domain root or under a subdirectory.
2. Serve JavaScript files with the JavaScript MIME type.
3. Enable byte ranges, which a sharded store needs.
4. Configure CORS if an application on another origin reads the store or imports the modules.
5. Check the hosted store.

   ```sh
   uv run chronozarr doctor https://your-host.example/my-published-series/store
   ```

6. Read one pixel from the hosted store.

   ```sh
   uv run python examples/bring_your_data/read_series.py \
     https://your-host.example/my-published-series/store --row 20 --col 30
   ```

See [hosting](../../docs/hosting.md) for provider recipes and cache settings. Open `index.html` or `examples/embed.html` at the upload location to see the result.

## 8. Run the browser check (optional)

The check drives the preview server with a headless browser and needs the repository's browser tooling.

1. Install the tooling once.

   ```sh
   npm ci --prefix js
   (cd js && npx playwright install chromium)
   ```

2. Leave the preview server running. Run the check with the base URL that the server printed, without `index.html`.

   ```sh
   node examples/bring_your_data/check.mjs http://127.0.0.1:8000/my-published-series/
   ```

The store needs at least two observations. The check renders the full viewer. It moves the host slider until the embedded viewer shows the second observation, selects a product, clicks a pixel, and presses play and pause. It blocks requests to other origins and fails on browser errors.

## What you get

| Path | Content |
|------|---------|
| `/tmp/my-series` | The chronozarr store |
| `/tmp/my-published-series/store/` | A copy of the validated store with no data changes |
| `/tmp/my-published-series/index.html` | A page that opens the viewer on the local store |
| `/tmp/my-published-series/examples/embed.html` | A host page that controls an embedded viewer |
| `/tmp/my-published-series/bundle.json` | The reader version, the number of objects in the store and their total size |
| Folders next to `store/` | The viewer, reader and map-layer code and the vendored libraries |

## Limits

- The recipe covers public data and data that the host of the page can read. Authentication and expiring tokens need extra work, so do not publish private rasters in this folder.
- `verify.py` compares level 0 with the source COGs and leaves the COG overviews out.
- The Sentinel-2 sample COGs are resampled to a 10 m grid, so they differ from the original scenes.
- The timings from `compare_local.py` come from Python reads on local disk.
- The `extended/`, `large/` and `http_stress/` folders hold further check scripts for twelve dates, a larger source, appending a date and failed requests. They have no guide of their own.

## Report a problem

Include your source type, the dimensions, bands and dates, your browser and hosting provider, the conversion command and any error. Say whether the viewer, the embed controls and the Python values work. Add `bundle.json` and the output of `chronozarr doctor`. Remove credentials and signed URLs.
