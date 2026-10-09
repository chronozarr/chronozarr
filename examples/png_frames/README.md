# PNG frames

This example converts 36 monthly true-color PNG frames with world files into a chronozarr store. The frames show the Ucayali River in Peru. `render_frames.py` writes them from Sentinel-2 mosaics, in the form an image exporter produces. The store keeps the 8-bit values and turns the alpha band into the mask. The [conversion rules](../../docs/png-frames.md) have the details.

## Run it

Run the commands from the repository root. The scripts read the monthly mosaics 2019-01 to 2021-12 in `data/mosaics/ucayali_santa_maria`. The [Sentinel-2 example](../sentinel2_pc/README.md) writes them and needs no account.

1. Install the extra.

   ```sh
   uv sync --extra geo
   ```

2. Render the frames into `data/png_frames/ucayali`.

   ```sh
   uv run python examples/png_frames/render_frames.py
   ```

3. Convert the frames into `data/stores/ucayali_santa_maria/png-1` and validate the store.

   ```sh
   examples/png_frames/convert.sh
   ```

4. Check that every frame is bit-exact in the store.

   ```sh
   uv run python examples/png_frames/check_store.py
   ```

## Check it in a browser

Start a range server in one shell. Run the check in a second shell. The check needs Node.js and `npm ci --prefix js`.

```sh
uv run --with rangehttpserver python -m RangeHTTPServer 8000
node examples/png_frames/viewer_check.mjs
```

The frames hold display values, so the viewer enables only True color and Single band. `render_frames.py` and `convert.sh` stop if their output exists.
