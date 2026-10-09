# SWOT intensity

This example builds a chronozarr store from existing geocoded SWOT amplitude rasters of Wax Lake and the Atchafalaya. The two dates, 2024-12-11 and 2025-05-06, share a 60 m grid. The store holds `amplitude`, `intensity` (amplitude squared) and `intensity_dB`, derived after geocoding. The notebook shows it with fixed limits of 30 to 80 dB.

## Run it

Run the commands from the repository root. The build reads four GeoTIFFs from `~/projects/swot-slc-geocode/output/site_scouting/wax_lake_atchafalaya/geocoded`. They are not in this repository. The example needs no account.

1. Install the extras.

   ```sh
   uv sync --extra geo --extra notebook --extra leafmap
   ```

2. Build the store `data/stores/swot_intensity/local-20261002`. The build keeps an existing store, so delete an old one first.

   ```sh
   uv run python examples/swot_intensity/build_demo.py
   ```

3. Open `demo.ipynb` in a local notebook.

## Check it in a browser

The checks need Node.js and `npm ci --prefix js`.

```sh
uv run python examples/swot_intensity/prepare_check.py
node examples/swot_intensity/browser_check.mjs
node examples/swot_intensity/player_check.mjs
```

The intensity is uncalibrated and is not sigma0, and the example supports no flood inference. Only a machine with the source rasters can build the store.
