# chronozarr

Start with [Bring your own data](examples/bring_your_data/README.md). It converts your rasters, reads values in Python, and publishes a self-hosted viewer and embed example.

chronozarr opens a decade of analysis-ready satellite time series in a browser tab from a static bucket. You scrub it like video. You click for real numbers.

chronozarr v0.3 is a raster time-series profile. It is built on Zarr v3 and the zarr-conventions multiscales, proj and spatial v0.1. Every data array holds true stored values. Physical units use a per-band scale and offset. Volatility is optional.

v0.3 readers do not open v0.2 stores. Migrate a v0.2 store with `chronozarr convert OLD_STORE NEW_STORE`.

A store is a Zarr group of pyramid levels. A level is one resolution of the raster. A chunk is one piece of a level at one timestep. The default layout writes one object per chunk.

Reader checks and measurements are in [docs/evidence.md](https://github.com/chronozarr/chronozarr/blob/main/docs/evidence.md).

## Who this is for

- **Researchers with stacks.** You have a Sentinel-2, Landsat or model time series in xarray, NetCDF or GeoTIFFs. `chronozarr encode` writes a store from an array. `chronozarr convert` writes a store from a COG manifest, a Zarr variable or a NetCDF file, one timestep at a time. The viewer scrubs it.
- **Data publishers.** A store is one immutable prefix in a bucket. The host needs byte ranges and CORS. `chronozarr doctor <url>` checks a live URL. Recipes are in [docs/hosting.md](https://github.com/chronozarr/chronozarr/blob/main/docs/hosting.md).
- **Map library authors.** `js/chronozarr/` is a DOM-free reader on zarrita. `js/maplibre/` is a MapLibre custom layer built on it.
- **Notebook users.** `chronozarr.open_store(path_or_url).to_xarray()` returns arrays. `chronozarr.view(store)` opens a local store in the viewer.

## Install

Install the Python package and the CLI:

```bash
pip install chronozarr
```

Extras: `geo` (GeoTIFF input), `notebook` (`view()`), `netcdf`, `dask`.

Install the JavaScript reader and the MapLibre layer:

```bash
npm install chronozarr
```

The JavaScript package has no build step and no runtime dependency. Usage examples are in [js/README.md](https://github.com/chronozarr/chronozarr/blob/main/js/README.md).

To work from a checkout of this repository:

```bash
uv sync
uv run chronozarr --help
```

Add `--extra geo` for GeoTIFF input. Add `--extra notebook` for `view()`.

## Quickstart

1. Encode your array, NetCDF file or GeoTIFF glob into a store.

   ```bash
   uv run chronozarr encode scenes.nc my_store
   ```

   For COG manifests, use `chronozarr convert SOURCE OUT`.

2. Validate the store.

   ```bash
   uv run chronozarr validate my_store
   ```

3. Read the store in Python.

   ```python
   import chronozarr

   store = chronozarr.open_store("my_store")
   store.read(t=42)         # (band, y, x), exact stored values
   store.to_xarray(lod=0)
   ```

4. Upload the store to a static host. Follow [docs/hosting.md](https://github.com/chronozarr/chronozarr/blob/main/docs/hosting.md).

5. Check the live URL.

   ```bash
   uv run chronozarr doctor https://your-host/my_store
   ```

6. Open the viewer.

   ```
   js/demo/index.html?store=https://your-bucket/my_store
   ```

A plain Zarr reader also reads the true stored values:

```python
import xarray as xr

ds = xr.open_zarr("my_store", group="0", zarr_format=3, chunks=None)
```

To write from memory, call `chronozarr.encode(da, "my_store", crs="EPSG:32631")`. The array `da` has dims `(time, band, y, x)` with x and y coordinates.

## Commands

`uv run chronozarr <command> --help` lists every option.

| Command | What it does |
|---------|--------------|
| `encode INPUT OUT` | Writes a store from a Zarr store, a NetCDF file or a quoted GeoTIFF glob |
| `convert SOURCE OUT` | Writes a store from a manifest of COGs or PNG frames, a Zarr store or a NetCDF file, one timestep at a time |
| `append STORE INPUT` | Adds timesteps at the end of a store. See [docs/append.md](https://github.com/chronozarr/chronozarr/blob/main/docs/append.md) |
| `validate STORE` | Checks a store against the spec. Exits with status 1 on failure |
| `info STORE` | Prints the times, bands and levels of a store |
| `doctor TARGET` | Checks an https URL or a local path. See [docs/hosting.md](https://github.com/chronozarr/chronozarr/blob/main/docs/hosting.md) |
| `export-cog STORE OUT_DIR` | Writes true-value COGs for GDAL and QGIS. Needs extra `geo` |
| `stac STORE --out DIR` | Writes a static STAC Collection and Item. Needs extra `geo` |

## What is in this repo

| Part | Path | Content |
|------|------|---------|
| Format spec | `spec/CHRONOZARR.md` | The normative layout, attributes and hosting rules |
| Python package | `src/chronozarr/` | `encode()`, `open_store()`, `validate()`, `view()`, the CLI and the xarray engine `chronozarr` |
| JS reader | `js/chronozarr/` | DOM-free reader on zarrita. It maps `(lod, row, col, t)` to a typed-array cell. It has a shard index cache, worker-pool decode and prefetch |
| MapLibre layer | `js/maplibre/` | Custom layer that draws a store |
| Viewer | `js/demo/` | WebGL2 viewer with time scrub, looping playback up to 60 steps per second, click for values and a time-series chart, permalinks, and WebM and GIF export. `?embed=1` gives a compact mode with a postMessage API for host pages |
| Ingest example | `examples/sentinel2_pc/` | Monthly Sentinel-2 median composites from Planetary Computer |
| Water-mask example | `examples/water_masks/` | NDWI and water-fraction stores. See [docs/user-zero.md](https://github.com/chronozarr/chronozarr/blob/main/docs/user-zero.md) |
| PNG frames example | `examples/png_frames/` | Georeferenced PNG frames converted to a store. See [docs/png-frames.md](https://github.com/chronozarr/chronozarr/blob/main/docs/png-frames.md) |
| Docs | `docs/` | Hosting, appending, embedding, format comparison, evidence and [style guide](docs/STYLE.md) |

## More

- Spec: [v0.3.0](spec/CHRONOZARR.md).
- Guides: [hosting](docs/hosting.md), [appending](docs/append.md), [embedding](docs/embedding.md).
- Comparison with PMTiles, Mapbox raster-array and CarbonPlan zarr-layer: [format comparison](https://github.com/chronozarr/chronozarr/blob/main/docs/format-comparison.md).
- Measurements, tested versions and development checks: [evidence](https://github.com/chronozarr/chronozarr/blob/main/docs/evidence.md).

## Status

The spec is v0.3.0. The package is 0.3.1. The default layout is unsharded. Sharding is optional with `--shard`.

The demo catalog uses the v0.3 Ucayali store `ucayali_santa_maria_v03` and the v0.3 PNG store `ucayali_santa_maria/png-v03`. Both are on `data.chronozarr.org`.
