# Local SWOT raster fidelity demo

This example builds a small chronozarr store from two SWOT L2 HR Raster 100 m NetCDF files over the Roanoke region, dated 2025-11-14 and 2026-07-12. `demo.ipynb` opens the store in the notebook player and in a leafmap MapLibre map. The build and the browser check confirm that the store holds every source value unchanged.

The store holds water surface elevation (WSE) as float32 values in metres, with a mask for invalid pixels. Two more builds keep only the pixels that pass a quality threshold.

## What you need

- The two source NetCDF files on disk, under `~/geodata/nisar_swot_water_detection/roanoke*/`. The build reads them without changing them, and `--source-root` points it to another location. The example downloads and uploads nothing.
- The `geo` extra, which provides rasterio.
- The `notebook` extra and leafmap, to run the notebooks.
- Node.js and the browser tooling from `npm ci --prefix js`, to run the browser check.

All output goes under the `data/` directory, which git ignores.

## Steps

Run the commands from the repository root.

1. Install the extras.

   ```sh
   uv sync --extra geo --extra notebook --extra leafmap
   ```

2. Build the store.

   ```sh
   uv run python examples/swot_raster/build_demo.py
   ```

3. Run the browser check.

   ```sh
   node examples/swot_raster/browser_check.mjs
   ```

4. Open `demo.ipynb` in a notebook.

The build intersects the aligned EPSG:32618 grids of the two files. It chooses a 512 × 512 window on the native 100 m grid. The window has the most shared valid WSE pixels, found by a search at a stride of 64 pixels. It stages GeoTIFF crops, with no warp and no interpolation. `chronozarr.convert` then writes a two-date, two-level, unsharded float32 store with explicit masks, about 1 MB in size.

### Quality-filtered variants

The build option `--quality` also masks pixels by the delivered quality flag, `wse_qual`. Each variant has its own store, check option and notebook.

| Variant | Mask keeps | Valid pixels on 2025-11-14 | Valid pixels on 2026-07-12 | Store | Notebook |
|---------|------------|---------------------------:|---------------------------:|-------|----------|
| default | Finite pixels that are not fill | 93,717 | 65,672 | `local-20261002` | `demo.ipynb` |
| `--quality good` | Those with `wse_qual == 0` | 26,049 | 30,589 | `good-20261002` | `filtered.ipynb` |
| `--quality usable` | Those with `wse_qual <= 1` (good or suspect) | 76,521 | 59,971 | `usable-20261002` | `quality_le1.ipynb` |

Build and check a variant with these commands:

```sh
uv run python examples/swot_raster/build_demo.py --quality good
node examples/swot_raster/browser_check.mjs --good

uv run python examples/swot_raster/build_demo.py --quality usable
node examples/swot_raster/browser_check.mjs --usable
```

Each variant keeps the raw values, the native grid and the window of the default store. The build does no clipping, interpolation or morphological cleanup. All three stores pass the same bit-exact data and mask, xarray, COG and browser checks.

## What you get

The default store is `data/stores/swot_roanoke/local-20261002`. WSE is in metres above the geoid of the source product, with its delivered corrections. Level 0 keeps all raw values, including negative elevations and the source fill sentinel. The mask marks the finite pixels that are not fill.

Both xarray interfaces, `store.to_xarray()` and `xr.open_dataset(..., engine="chronozarr")`, keep the per-band units in `band_units` and set the units of the data variable to `m`.

The build also checks its own output:

- the complete level-0 float bit patterns and masks
- the xarray values and mask coordinates
- the valid values, masks, metre units and grid of a COG export

A COG export keeps invalid pixels only as invalid, while the chronozarr store also keeps their original fill values. The build writes the source paths, SHA-256 hashes, windows, counts, timestamps and results to `data/reports/swot-roanoke-20261002.json`. The variants write `-good` and `-usable` versions of that file.

The browser check compares the complete chunk and mask hashes with Python. It checks the negative pixel readouts and the units on both dates. It also compares the geographic `getValueAt` of the MapLibre layer with a source pixel. It saves a screenshot to `data/reports/swot-roanoke-viewer.png`.

The MapLibre demo accepts `p=band` for this single-band store. The leafmap helper uses `product="band"`.

## Limits

- The default store applies no quality screening, and its mask removes only fill and non-finite pixels.
- The two acquisitions come from different passes.
- Water observations have gaps even at good quality. The build never excludes a value because it is negative.
- No valid pixel in the chosen window has an elevation of exactly 0.
- The notebook player reads the local store through the range and CORS server, so the browser may ask for local-network permission.
- The notebooks turn off the floating sidebar of leafmap, as the [leafmap example](../leafmap/README.md) explains.
