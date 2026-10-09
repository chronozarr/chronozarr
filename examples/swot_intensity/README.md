# SWOT intensity

This example builds a chronozarr store from existing geocoded SWOT amplitude rasters of Wax Lake and the Atchafalaya. The two dates, 2024-12-11 and 2025-05-06, share a 60 m grid. The store holds `amplitude`, `intensity` (amplitude squared) and `intensity_dB`, derived after geocoding. The notebook shows it with fixed limits of 30 to 80 dB.

## Get the data

Only a machine that has the output of `swot-slc-geocode`, a private project, can build this store. The inputs are four GeoTIFFs: two 60 m amplitude crops and two 5 m crops. No public archive serves them, and this repository has no recipe to make them.

The `source` tag of each 5 m GeoTIFF names its SWOT L1B HR SLC granule. The PO.DAAC serves both granules in the collection `SWOT_L1B_HR_SLC_D` (version D). Each file is 2.0 GB. A download needs a free [NASA Earthdata login](https://urs.earthdata.nasa.gov).

- `SWOT_L1B_HR_SLC_025_244_102R_20241211T161411_20241211T161422_PGD0_01.nc`
- `SWOT_L1B_HR_SLC_032_244_102R_20250506T172942_20250506T172953_PGD0_01.nc`

If you have rasters with the same file names and tags, pass their directory as `--source-root`. The default is `~/projects/swot-slc-geocode/output/site_scouting/wax_lake_atchafalaya/geocoded`.

## Run it

Run the commands from the repository root.

1. Install the extras.

   ```sh
   uv sync --extra geo --extra notebook --extra leafmap
   ```

2. Build the store `data/stores/swot_intensity/local-20261002`. The build keeps an existing store, so delete an old one first.

   ```sh
   uv run python examples/swot_intensity/build_demo.py --source-root path/to/geocoded
   ```

3. Open `demo.ipynb` in a local notebook.

## Check it in a browser

The checks need Node.js and `npm ci --prefix js`.

```sh
uv run python examples/swot_intensity/prepare_check.py
node examples/swot_intensity/browser_check.mjs
node examples/swot_intensity/player_check.mjs
```

The intensity is uncalibrated and is not sigma0, and the example supports no flood inference.
