# Water mask stacks

This example derives a water store from the monthly Sentinel-2 mosaics of one area of interest (AOI). The store has two int16 bands, `ndwi` and `water`, a mask and a coverage plane. Physical `water` values are 0 or 1 at level 0. Each overview recursively averages valid pixels of the preceding level, with integer rounding down. Level 1 approximates the water fraction of its valid source pixels; higher levels weight valid child blocks equally, even when those blocks contain different numbers of original observations. Use level 0 for water-area statistics. Each month uses its Otsu threshold on NDWI, or the `--floor` value when that is higher.

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

The build also writes a CSV of monthly thresholds and quicklook PNGs in `data/reports`. Current Sentinel-2 ingest already applies each scene's processing-baseline offset. Use `--boa-offset-from` only for a separately verified legacy mosaic series that still contains the +1000 DN offset; applying it to corrected mosaics subtracts the offset twice and changes the water classification. The acquisition month alone does not establish the processing baseline, because older scenes can be reprocessed.

## Check it in a browser

Start a range server in one shell. Run the check in a second shell. The check needs Node.js and `npm ci --prefix js`.

```sh
uv run --with rangehttpserver python -m RangeHTTPServer 8000
node examples/water_masks/viewer_check.mjs --aoi ucayali_santa_maria --month 2019-03
```

Use a store built from the current checkout. The browser check treats console errors as failures; older stores can omit all-fill chunks, whose 404 responses are valid missing-chunk behavior. Current encodes write those chunks explicitly.
