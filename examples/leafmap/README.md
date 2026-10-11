# chronozarr in leafmap

`add_chronozarr` draws a chronozarr store on a leafmap map, fits the map to the store and adds a date slider. The layer reads pixel values directly from static storage, so the map needs no raster tile service. The map must be a `leafmap.maplibregl.Map`, and you must call the helper before you display it.

## Run it

1. Add the extra to your project with `uv add 'chronozarr[leafmap]'`. The pip fallback is `pip install 'chronozarr[leafmap]'`. In this checkout, run `uv sync --locked --all-extras`.
2. Open `demo.ipynb` in a local notebook, or run this code in a cell.

   ```python
   import leafmap.maplibregl as leafmap
   from chronozarr import add_chronozarr

   m = leafmap.Map(style="positron", height="600px",
                   add_sidebar=False, add_floating_sidebar=False)
   add_chronozarr(m)
   m
   ```

The browser must allow module imports from blob URLs and reach the reader at `https://chronozarr.org/maplibre/layer.js`. Set `reader_url=` to use your own copy. The store URL must send CORS headers.

The arguments are `url`, `t`, `product`, `opacity`, `fit_bounds` and `name`. Each added layer needs a unique `name`. A local store path works as `url`, and the helper serves it with a local CORS and byte-range server. To start that server yourself, use `from chronozarr.view import serve_store`. A remote kernel needs a store URL that the browser can reach. Pass the absolute `http(s)` address of the local server as `base_url`, for example one that a proxy or an SSH forward gives. See [Remote notebooks](../../docs/python.md#remote-notebooks). To try the rendered PNG frames, pass `"https://data.chronozarr.org/ucayali_santa_maria/png-v03"` as `url` on a new map.

The two sidebar arguments turn off the floating sidebar of leafmap. leafmap 0.63.1 builds that sidebar with `ipyvuetify.ExpansionPanelHeader`, which ipyvuetify 3 removed. To use the sidebar, install `ipyvuetify<3` and `ipyvue<3`. Versions 1.11.3 and 1.12.0 were checked with leafmap 0.63.1 on 2026-10-02.

## Check it in a browser

```sh
uv run --with leafmap python examples/leafmap/prepare_check.py
node examples/leafmap/browser_check.mjs
```

The second command needs Node.js, the tooling from `npm ci --prefix js` and the local store `data/stores/ucayali_santa_maria/png-1`. Build that store with `examples/png_frames/render_frames.py` and `examples/png_frames/convert.sh`, which read the monthly mosaics from the [Sentinel-2 example](../sentinel2_pc/README.md). The check opens the real upstream renderer in Chromium, adds the layer, changes dates, saves `data/reports/leafmap/browser.png` and runs cleanup. It runs without a Jupyter server, so run `demo.ipynb` in your own notebook to test the notebook trust policy and the kernel comms.
