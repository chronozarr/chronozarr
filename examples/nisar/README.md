# NISAR backscatter

This example builds a chronozarr store from two NISAR HH and HV backscatter windows at 20 m, dated 2026-06-22 and 2026-08-21. The store holds each band as linear power and as dB. The notebook opens the store in the notebook player, in leafmap maps and in xarray, with fixed limits of -25 to 0 dB.

## Get the data

Run the commands from the repository root. The build reads two `.npz` extracts, one per date. No archive serves them. You cut them from two NISAR L2 GCOV granules of the ASF DAAC. Downloading a granule needs a free [NASA Earthdata login](https://urs.earthdata.nasa.gov).

1. Search the collection `NISAR_L2_GCOV_PROVISIONAL_V1` (version 1) in CMR.

   ```sh
   curl -sG https://cmr.earthdata.nasa.gov/search/granules.csv \
     -d short_name=NISAR_L2_GCOV_PROVISIONAL_V1 \
     -d point=-74.84,8.11 \
     -d 'temporal[]=2026-06-22T00:00:00Z,2026-06-22T23:59:59Z' \
     -d 'temporal[]=2026-08-21T00:00:00Z,2026-08-21T23:59:59Z'
   ```

   The query returns two granules of about 1.9 GB each.

   - `NISAR_L2_PR_GCOV_023_119_A_006_2005_DHDH_M_20260622T103120_20260622T103155_P05023_N_F_J_001`
   - `NISAR_L2_PR_GCOV_028_119_A_006_2005_DHDH_M_20260821T103117_20260821T103151_P05023_N_F_J_001`

2. Download the `.h5` file of each granule with your Earthdata login.
3. Cut the window from each file.

   ```sh
   uv run --extra netcdf python examples/nisar/extract_window.py path/to/granule.h5
   ```

The script writes `data/examples/nisar/nisar_<date>_frequencyA.npz`. [Evidence](../../docs/evidence.md#nisar-and-swot-example-data) records how the granules were matched.

## Run it

1. Install the extras.

   ```sh
   uv sync --extra notebook --extra leafmap
   ```

2. Build the store `data/stores/nisar/local-20261002`. Delete an old store first. `--source-root` defaults to `~/geodata/nisar_swot_water_detection/pilot_20260904`.

   ```sh
   uv run python examples/nisar/build_demo.py --source-root data/examples/nisar
   ```

3. Open `demo.ipynb` in a local notebook.

## Check it in a browser

The checks need Node.js and `npm ci --prefix js`. Add `--hv` to the first two commands to check the HV band.

```sh
uv run python examples/nisar/prepare_check.py
node examples/nisar/browser_check.mjs
node examples/nisar/player_check.mjs
```

The extracts have no calibration metadata or acquisition times, so the example supports no change analysis.
