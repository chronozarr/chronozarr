# Bring your own raster time series

This example converts georeferenced rasters on one grid into a chronozarr store and reads one pixel's values over time in Python. It then builds a static folder with the store, a viewer and an embed page. You can upload that folder to any static host. The folder needs no request to chronozarr.org or to its demo stores. The bundle script copies the JavaScript dependencies, including the GIF encoder, into it. You need a checkout of this repository, uv with Python 3.11 or newer, and Node.js with Chromium for the optional browser check in step 8.

## 1. Install

Run this command from the repository root.

```sh
uv sync --extra geo
```

uv picks an installed Python unless you add `--python`, as in `uv sync --python 3.13 --extra geo`. On Python 3.14 the locked codec dependency may need a source build. On 2026-10-02 this recipe ran from a clean checkout with Python 3.13, a fresh environment and newly downloaded observations.

The converter and reader are also on PyPI as `chronozarr[geo]`. The bundle script needs this checkout, because it copies the viewer from `js/`. It needs no npm install and no build. To copy the viewer from the npm package instead, see [self-host the packaged viewer](../../docs/viewer-distribution.md).

## 2. Prepare observations

For your own rasters, write `observations.csv` next to your files:

```csv
uri,datetime
january.tif,2024-01-01
february.tif,2024-02-01
march.tif,2024-03-01
```

Each row names one raster and its ISO-8601 date. A URI is an HTTP URL or a path relative to the manifest. For a first run, use rasters on the same north-up grid with the same CRS, bands, dtype, scales and offsets. The converter keeps the band descriptions and the scale and offset metadata of the source rasters, so set them there. Band names such as `red`, `green`, `blue` and `nir` let the viewer offer the matching products. A single measured variable also works.

For a sample, download three Sentinel-2 acquisitions near Lake Mead from May to July 2020. This needs network access but no account and no API key.

```sh
uv sync --extra geo --extra ingest
uv run python examples/bring_your_data/fetch_sample.py /tmp/lake-mead-input
```

The script writes three COGs, `observations.csv` and `source.json` into `/tmp/lake-mead-input`. For each month it picks the acquisition with the lowest scene cloud cover from one tile that covers the whole area. It resamples four bands to one 10 m UTM grid and applies the validity rule of the [Sentinel-2 example](../sentinel2_pc/README.md). It stops if an observation has less than 50% valid pixels. The manifest dates are acquisition dates, and `source.json` lists the scene IDs that your run used. In the steps below, use `/tmp/lake-mead-input/observations.csv` in place of `observations.csv`.

## 3. Convert and validate

1. Print the plan. A dry run writes nothing.

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

4. Run doctor on the local store.

   ```sh
   uv run chronozarr doctor /tmp/my-series
   ```

To convert an existing Zarr or NetCDF time series, use its path as the source and add `--variable NAME` where needed. NetCDF input needs `uv sync --extra geo --extra netcdf`. Run `uv run chronozarr convert --help` for the reprojection and resampling options. Resampling changes values, so use it only when you want that. For rendered PNG frames, follow the [PNG georeferencing guide](../../docs/png-frames.md). A PNG holds display colors, and the converter stores them unchanged.

## 4. Read a pixel's history

```sh
uv run python examples/bring_your_data/read_series.py /tmp/my-series --row 20 --col 30
```

The script opens the store with the lazy xarray backend and selects one pixel before it reads values. It prints physical values, which are the stored values after scale and offset, with NaN for invalid values. Add `physical=False` to `xr.open_dataset` to get the stored values. Over HTTP, a pixel history transfers one whole chunk per date.

## 5. Check the store against COG sources (optional)

The checks read one copy of the store with chronozarr and another with plain xarray and Zarr code, so convert the manifest a second time.

1. Convert the second copy.

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

`verify.py` compares every level-0 value and mask with the COGs and checks the physical scaling. It leaves the COG overviews out. The Sentinel-2 sample COGs are resampled to 10 m, so they differ from the original scenes. `compare_local.py` times level-0 reads with the COG, chronozarr and plain Zarr readers on local disk. It uses fresh file handles on warm caches and rotates the reader order. The `--repeats` option sets the number of repeats, which is 5 by default.

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

The embed page has a slider, a product menu, play and pause buttons and a pixel readout. They control a self-hosted iframe through the `chronozarr:*` messages in [embedding](../../docs/embedding.md). `bundle.py` copies the validated store unchanged, so allow disk space for a second copy. It stops if the output folder exists or lies inside the input store. The folder holds `store/`, `index.html`, `examples/embed.html`, `bundle.json` (the reader version, the object count and the size) and the viewer code. The preview server binds to localhost, so use it only for previews.

## 7. Publish to your own host

1. Upload the contents of `/tmp/my-published-series` to a static host and keep the directory structure. The folder works at a domain root or under a subdirectory.
2. Serve JavaScript with the JavaScript MIME type and enable byte ranges, which a sharded store needs. Configure CORS if an application on another origin reads the store or imports the modules. See [hosting](../../docs/hosting.md).
3. Check the hosted store.

   ```sh
   uv run chronozarr doctor https://your-host.example/my-published-series/store
   ```

4. Read one pixel from the hosted store.

   ```sh
   uv run python examples/bring_your_data/read_series.py \
     https://your-host.example/my-published-series/store --row 20 --col 30
   ```

The recipe covers public data and data that the host of the page can read. Authentication and expiring tokens need extra work, so do not publish private rasters in this folder.

## 8. Run the browser check (optional)

1. Install the browser tooling once.

   ```sh
   npm ci --prefix js
   (cd js && npx playwright install chromium)
   ```

2. Leave the preview server running. Run the check with the base URL that the server printed, without `index.html`.

   ```sh
   node examples/bring_your_data/check.mjs http://127.0.0.1:8000/my-published-series/
   ```

The store needs at least two observations. The check renders the full viewer and moves the host slider until the embedded viewer shows the second observation. It also selects a product, clicks a pixel, and presses play and pause. It blocks requests to other origins and fails on browser errors.

## Further checks and problem reports

The `extended/`, `large/` and `http_stress/` folders hold further check scripts for twelve dates, a larger source, appending a date and failed requests. When you report a problem, include your source type, the dimensions, bands and dates, your browser and host, and the conversion command and any error. Add the `bundle.json` and `chronozarr doctor` output. Remove credentials and signed URLs.
