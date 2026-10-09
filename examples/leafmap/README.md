# chronozarr in leafmap

`add_chronozarr` is a helper in the `chronozarr` package that draws a chronozarr store on a leafmap map and adds a date slider. The layer reads pixel values directly from static storage, so the map needs no raster tile service.

## What you need

- The `chronozarr[leafmap]` extra and a local Jupyter notebook.
- A browser that allows module imports from blob URLs.
- Network access to the hosted reader at `https://chronozarr.org/maplibre/layer.js`. Set `reader_url=` to use your own copy.
- A store URL that sends CORS headers. The default store is public.

## Steps

1. Install the extra with `pip install "chronozarr[leafmap]"`. In this checkout, run `uv sync --extra leafmap` instead.
2. Open `demo.ipynb` in a local notebook, or run this code in a cell.

   ```python
   import leafmap.maplibregl as leafmap
   from chronozarr import add_chronozarr

   m = leafmap.Map(style="positron", height="600px",
                   add_sidebar=False, add_floating_sidebar=False)
   add_chronozarr(m)
   m
   ```

3. Call `add_chronozarr` before you display the map.

The two sidebar arguments turn off the floating sidebar of leafmap, for the reason given under Limits.

### Arguments

| Argument | Meaning | Default |
|----------|---------|---------|
| `url` | The store URL, or a local store path | `https://data.chronozarr.org/ucayali_santa_maria_v03` |
| `t` | The first timestep | `0` |
| `product` | The product to draw | `"true_color"` |
| `opacity` | The layer opacity, from 0 to 1 | `1.0` |
| `fit_bounds` | Whether to fit the map to the store | `True` |
| `name` | The layer name, which must be unique for each added layer | `"chronozarr"` |
| `reader_url` | The URL of the reader module | `https://chronozarr.org/maplibre/layer.js` |

To try the rendered PNG frames, create a new map and pass `"https://data.chronozarr.org/ucayali_santa_maria/png-v03"` as `url`. The RGB values in that store are display colors.

### Local stores

A local store path works as `url`, and the helper serves it with a local CORS and byte-range server. To start that server yourself, import `serve_store` with `from chronozarr.view import serve_store`. A remote kernel needs a store URL that the browser can reach.

### Check the helper in a browser

```sh
uv run --with leafmap python examples/leafmap/prepare_check.py
node examples/leafmap/browser_check.mjs
```

Run both commands from the repository root. The second command needs Node.js and the browser tooling from `npm ci --prefix js`. It also needs the local store `data/stores/ucayali_santa_maria/png-1`. Build that store with `examples/png_frames/render_frames.py` and `examples/png_frames/convert.sh`, which read the Ucayali monthly mosaics from the [Sentinel-2 example](../sentinel2_pc/README.md).

The first command builds the check files from the installed leafmap widget. The second command opens the real upstream renderer in Chromium, adds the custom GPU layer, changes dates, saves a screenshot to `data/reports/leafmap/browser.png` and runs cleanup. It exercises the browser bridge without a Jupyter server. To test the notebook trust policy and the kernel comms, run `demo.ipynb` in your own notebook.

## What you get

You get a leafmap map with its basemaps and controls, the store drawn as a GPU layer, and a date slider. The helper adds the existing `ChronozarrLayer` and fits the map to the store.

A Python layer definition cannot carry a WebGL callback. The helper therefore wraps the model interface of the installed py-maplibregl anywidget renderer, and it rebuilds the custom layer in the browser.

## Limits

- The map must be a `leafmap.maplibregl.Map` or a `geemap.maplibregl.Map`. The helper raises a `TypeError` for other maps, such as the default ipyleaflet map.
- The helper raises a `ValueError` for a map that is already displayed.
- leafmap 0.63.1 builds its floating sidebar with `ipyvuetify.ExpansionPanelHeader`, which ipyvuetify 3 removed. The map and the chronozarr slider work without the sidebar. To use the sidebar, install `ipyvuetify<3` and `ipyvue<3`. Versions 1.11.3 and 1.12.0 were checked with leafmap 0.63.1 on 2026-10-02.
