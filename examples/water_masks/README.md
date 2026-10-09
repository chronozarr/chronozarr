# Water mask stacks

This example derives a water store from the monthly Sentinel-2 mosaics of one area of interest (AOI). The store has two int16 bands, `ndwi` and `water`, a mask and a coverage plane. `water` is 0 or 1 at level 0. A coarse pyramid level holds the water fraction of each block. Each month uses its Otsu threshold on NDWI, or the `--floor` value when that is higher.

## Run it

Run the commands from the repository root. The scripts read `data/mosaics/<aoi>`, which the [Sentinel-2 example](../sentinel2_pc/README.md) writes. It needs no account.

1. Build the store `data/stores/ucayali_santa_maria/water-1`. The floor of -0.15 suits this AOI.

   ```sh
   uv run python examples/water_masks/build_water_stack.py --aoi ucayali_santa_maria --floor -0.15
   ```

2. Check the store against the mosaics for one month.

   ```sh
   uv run python examples/water_masks/check_water_stack.py --aoi ucayali_santa_maria --month 2019-03
   ```

The build also writes a CSV of monthly thresholds and quicklook PNGs in `data/reports`. For Lake Mead, add `--boa-offset-from 2022-02`. It removes the +1000 offset of Sentinel-2 processing baseline 04.00.

## Check it in a browser

Start a range server in one shell. Run the check in a second shell. The check needs Node.js and `npm ci --prefix js`.

```sh
uv run --with rangehttpserver python -m RangeHTTPServer 8000
node examples/water_masks/viewer_check.mjs --aoi ucayali_santa_maria --month 2019-03
```

The last check fails on this server. The server answers 404 for chunks that the encoder did not write, and the browser logs each as an error. The earlier checks pass.
