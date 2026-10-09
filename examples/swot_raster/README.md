# Local SWOT raster fidelity demo

This example builds a small chronozarr store from two SWOT L2 HR Raster 100 m NetCDF files over the Roanoke region, dated 2025-11-14 and 2026-07-12. It opens the store in the notebook player and in a leafmap MapLibre map. The store holds water surface elevation (WSE) as float32 metres with a mask. The default store applies no quality screening, and the two acquisitions come from different passes.

## Run it

You need the two NetCDF files under `~/geodata/nisar_swot_water_detection/roanoke*/`. The build reads them without changing them, and `--source-root` points it to another location. The example downloads and uploads nothing. Its output goes under the ignored `data/` directory. Run the commands from the repository root.

1. Install the extras. The browser check also needs Node.js and `npm ci --prefix js`.

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

The build intersects the aligned EPSG:32618 grids. It chooses the 512 × 512 native-grid window with the most shared valid WSE pixels, searching at a stride of 64 pixels. It stages GeoTIFF crops with no warp and no interpolation. `chronozarr.convert` then writes a two-date, two-level, unsharded float32 store of about 1 MB with explicit masks.

## Quality-filtered variants

`--quality good` keeps pixels with `wse_qual == 0`. `--quality usable` keeps `wse_qual <= 1`, which is good or suspect. Each variant keeps the raw values, the native grid and the window of the default store, with no clipping, interpolation or morphological cleanup.

```sh
# good: store good-20261002, notebook filtered.ipynb
uv run python examples/swot_raster/build_demo.py --quality good
node examples/swot_raster/browser_check.mjs --good

# usable: store usable-20261002, notebook quality_le1.ipynb
uv run python examples/swot_raster/build_demo.py --quality usable
node examples/swot_raster/browser_check.mjs --usable
```

Valid pixels fall from 93,717 to 26,049 (2025-11-14) and from 65,672 to 30,589 (2026-07-12) for `good`, and to 76,521 and 59,971 for `usable`. Water observations have gaps even at good quality. The build never excludes a negative value.

## What the checks cover

The default store is `data/stores/swot_roanoke/local-20261002`. WSE is in metres above the geoid of the source product, with its delivered corrections. Level 0 keeps all raw values, including negative elevations and the source fill value, and the mask marks the finite pixels that are not fill. Both xarray interfaces keep the units in `band_units` and set the variable units to `m`.

The build checks level-0 float bit patterns and masks, xarray values, and the valid values, masks, units and grid of a COG export. It writes source paths, SHA-256 hashes, windows and counts to `data/reports/swot-roanoke-20261002.json`, with `-good` and `-usable` versions for the variants. All three stores pass these checks and the browser check.

The browser check compares chunk and mask hashes with Python and checks negative readouts and units on both dates. It also compares the MapLibre `getValueAt` with a source pixel. It saves `data/reports/swot-roanoke-viewer.png`. The MapLibre demo accepts `p=band` for this store, and the leafmap helper uses `product="band"`.

The player reads the local store through a range and CORS server, so the browser may ask for local-network permission. The notebooks turn off the floating sidebar of leafmap, as the [leafmap example](../leafmap/README.md) explains.
