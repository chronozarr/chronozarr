# NISAR backscatter

This example builds a chronozarr store from two NISAR HH and HV backscatter extracts at 20 m, dated 2026-06-22 and 2026-08-21 by file name. The store holds each band as linear power and as dB. The notebook opens the store in the notebook player, in leafmap maps and in xarray. The player and the maps use the fixed limits -25 to 0 dB.

## Run it

Run the commands from the repository root. The build reads two cached `.npz` files in `~/geodata/nisar_swot_water_detection/pilot_20260904`. They are not in this repository. The example needs no account.

1. Install the extras.

   ```sh
   uv sync --extra notebook --extra leafmap
   ```

2. Build the store `data/stores/nisar/local-20261002`. The build keeps an existing store, so delete an old one first.

   ```sh
   uv run python examples/nisar/build_demo.py
   ```

3. Open `demo.ipynb` in a local notebook.

## Check it in a browser

The checks need Node.js and `npm ci --prefix js`. Add `--hv` to the first two commands to check the HV band.

```sh
uv run python examples/nisar/prepare_check.py
node examples/nisar/browser_check.mjs
node examples/nisar/player_check.mjs
```

The extracts have no calibration metadata or acquisition times, so the example supports no change analysis. Only a machine with the source files can build the store.
