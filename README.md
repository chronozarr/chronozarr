# chronozarr

Publish a raster time series as static files. Scrub it in a browser, and read the exact values in Python.

[Live demo](https://chronozarr.org/demo/): 117 months of Sentinel-2 imagery over the Ucayali River, Peru, served from a bucket.

## How it works

A chronozarr store is a Zarr v3 group. It holds a pyramid of levels, and each level holds the full time series at one resolution. Every array holds the true stored values, not rendered images.

The store uses the zarr-conventions for the pyramid (`multiscales`), the CRS (`proj`) and the georeferencing (`spatial`). Because of this, GDAL 3.13, xarray and CarbonPlan zarr-layer open a store without chronozarr.

Any static host serves a store if it answers byte-range requests and sends CORS headers. There is no server code to run.

The browser viewer decodes chunks in web workers and draws the raw bands on the GPU. Band math, such as NDVI, runs in the shader. A click on the map returns the stored values for that pixel.

## Quickstart

Install the package with GeoTIFF support:

```bash
pip install "chronozarr[geo]"
```

1. Write a store from a GeoTIFF time series. Use one file per timestep, with the date in each file name.

   ```bash
   chronozarr encode "scenes/*.tif" my_store
   ```

2. Check the store against the spec.

   ```bash
   chronozarr validate my_store
   ```

3. Read the values in Python.

   ```python
   import chronozarr

   store = chronozarr.open_store("my_store")
   store.read(t=42)              # (band, y, x), exact stored values
   ds = store.to_xarray(lod=0)   # lazy xarray Dataset
   ```

4. Upload `my_store` to a static host. [docs/hosting.md](docs/hosting.md) has recipes for S3 with CloudFront, Cloudflare R2, Google Cloud Storage and Source Cooperative.

5. Check the host.

   ```bash
   chronozarr doctor https://your-host/my_store
   ```

6. Open the store in the hosted viewer:

   ```
   https://chronozarr.org/demo/?store=https://your-host/my_store
   ```

The input must be on an EPSG grid with north up. To write a store from an xarray `DataArray` with dims `(time, band, y, x)`, call `chronozarr.encode(da, "my_store", crs="EPSG:32618")`.

## Read a store without chronozarr

xarray reads each level as a plain Zarr group:

```python
import xarray as xr

ds = xr.open_zarr("my_store", group="0", zarr_format=3, chunks=None)
```

GDAL 3.13 reads the CRS, the georeferencing and the values. It lists each level as a separate subdataset. To get Cloud Optimized GeoTIFFs for older GDAL or QGIS, run `chronozarr export-cog my_store out_dir`.

## Commands

Run `chronozarr <command> --help` for every option.

| Command | What it does |
|---------|--------------|
| `encode INPUT OUT` | Writes a store from a Zarr store, a NetCDF file or a quoted GeoTIFF glob |
| `convert SOURCE OUT` | Writes a store one timestep at a time, from a manifest of COGs or PNG frames, a Zarr store or a NetCDF file. Also converts a v0.2 store to v0.3 |
| `append STORE INPUT` | Adds timesteps at the end of a store. See [docs/append.md](docs/append.md) |
| `validate STORE` | Checks a store against the spec. Exits with status 1 on failure |
| `info STORE` | Prints the times, bands and levels of a store |
| `doctor TARGET` | Checks a hosted URL or a local store. See [docs/hosting.md](docs/hosting.md) |
| `export-cog STORE OUT_DIR` | Writes true-value COGs for GDAL and QGIS. Needs the `geo` extra |
| `stac STORE --out DIR` | Writes a static STAC Collection and Item. Needs the `geo` extra |

Other extras: `notebook` adds `chronozarr.view(store)`, which shows a local store in Jupyter. `netcdf` and `dask` add those inputs.

## JavaScript

```bash
npm install chronozarr
```

The package has three parts:

- `chronozarr` is a DOM-free reader. It returns one cell of one timestep as a typed array.
- `chronozarr/maplibre` is a MapLibre custom layer that draws a store.
- `chronozarr-viewer` copies the full viewer into a folder that you can host next to your store. See [docs/viewer-distribution.md](docs/viewer-distribution.md).

There is no build step and no runtime dependency. Examples are in [js/README.md](js/README.md).

## Documentation

| Topic | Document |
|-------|----------|
| Format rules | [spec/CHRONOZARR.md](spec/CHRONOZARR.md) |
| Your own data, end to end | [examples/bring_your_data](examples/bring_your_data/README.md) |
| Hosting and the doctor checklist | [docs/hosting.md](docs/hosting.md) |
| Adding timesteps | [docs/append.md](docs/append.md) |
| Embedding the viewer in a page | [docs/embedding.md](docs/embedding.md) |
| Comparison with PMTiles, Mapbox raster-array and zarr-layer | [docs/format-comparison.md](docs/format-comparison.md) |
| Measurements and tested reader versions | [docs/evidence.md](docs/evidence.md) |
| Writing style for these docs | [docs/STYLE.md](docs/STYLE.md) |

## Status

The spec is v0.3.0, a draft. The packages are 0.3.1 on PyPI and npm. Readers open only v0.3 stores. To use a v0.2 store, convert it:

```bash
chronozarr convert OLD_STORE NEW_STORE
```

## License

See [LICENSE](LICENSE).
