# Local SWOT raster fidelity demo

This example builds a small chronozarr store from two SWOT L2 HR Raster 100 m NetCDF files over the Roanoke region, dated 2025-11-14 and 2026-07-12. The store holds water surface elevation (WSE) as float32 metres with a mask. The dates come from different passes. The notebook opens the store in the notebook player and in a leafmap map.

## Get the data

The PO.DAAC serves both files in the collection `SWOT_L2_HR_Raster_100m_D` (version D), about 60 MB each. A download needs a free [NASA Earthdata login](https://urs.earthdata.nasa.gov). Each file is at `https://archive.swot.podaac.earthdata.nasa.gov/podaac-swot-ops-cumulus-protected/SWOT_L2_HR_Raster_D/<file name>`.

1. Download these two files.

   - `SWOT_L2_HR_Raster_100m_UTM18S_N_x_x_x_041_369_109F_20251114T234531_20251114T234553_PID0_01.nc`
   - `SWOT_L2_HR_Raster_100m_UTM18S_N_x_x_x_053_076_046F_20260712T211127_20260712T211148_PID0_01.nc`

2. Put both in one directory, such as `data/swot_roanoke/roanoke/`.

The build reads `roanoke*/*.nc` under `--source-root`, which defaults to `~/geodata/nisar_swot_water_detection`.

## Run it

Run the commands from the repository root.

1. Install the extras. The browser check also needs Node.js and `npm ci --prefix js`.

   ```sh
   uv sync --extra geo --extra notebook --extra leafmap
   ```

2. Build the store `data/stores/swot_roanoke/local-20261002`.

   ```sh
   uv run python examples/swot_raster/build_demo.py --source-root data/swot_roanoke
   ```

3. Run the browser check.

   ```sh
   node examples/swot_raster/browser_check.mjs
   ```

4. Open `demo.ipynb` in a notebook.

The build picks the 512 × 512 native-grid window with the most shared valid WSE pixels. It writes a two-level, unsharded store of about 1 MB.

## Quality-filtered variants

`--quality good` keeps `wse_qual == 0`. `--quality usable` keeps `wse_qual <= 1`. Add one of them to the build command. Add `--good` or `--usable` to the browser check. `filtered.ipynb` and `quality_le1.ipynb` open the variants.

## What the checks cover

Level 0 keeps raw values, including negative elevations and the fill value. The mask marks the finite pixels that are not fill. [Evidence](../../docs/evidence.md#swot-raster-example) has the checks, the pixel counts and the CMR query.
