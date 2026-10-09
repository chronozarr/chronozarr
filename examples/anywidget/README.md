# Notebook player

`chronozarr.player` shows a chronozarr store in a notebook widget. The widget has a time slider and a play button. You can set the timestep, the product and the playback speed from Python. You can also read the pixel that you clicked.

The example opens a store of 36 rendered PNG frames of the Ucayali River in Peru, one for each month from 2019-01 to 2021-12. The `chronozarr` 0.3.1 release on PyPI includes the player.

## What you need

- The `notebook` extra of `chronozarr`.
- A trusted local Jupyter notebook, VS Code notebook or compatible anywidget host that allows an iframe.
- Network access to the hosted viewer and store, which are public and need no account.

## Steps

1. Install the extra with `uv add 'chronozarr[notebook]'`. In this checkout, run `uv sync --extra notebook` instead.
2. Open `demo.ipynb` in the notebook host.
3. Create the player and display it.

   ```python
   import chronozarr

   movie = chronozarr.player("https://data.chronozarr.org/ucayali_santa_maria/png-v03")
   movie
   ```

4. Wait for the viewer to open, then control it from Python.

   ```python
   movie.t = 9
   movie.product = "band"
   movie.speed = 10
   movie.playing = True
   movie.playing = False
   movie.click   # most recent clicked pixel, physical band values and validity
   movie.state   # acknowledged viewer state, including camera and date
   movie.error   # last viewer error; {} until one occurs
   movie.close()
   ```

### Check the player in a browser

```sh
node examples/anywidget/browser_check.mjs
node examples/anywidget/browser_check.mjs --live
```

Run both commands from the repository root. They need Node.js and the browser tooling from `npm ci --prefix js`.

The first command opens the local store `data/stores/ucayali_santa_maria/png-1`. Build that store with `examples/png_frames/render_frames.py` and `examples/png_frames/convert.sh`, which read the Ucayali monthly mosaics from the [Sentinel-2 example](../sentinel2_pc/README.md). The second command uses the hosted viewer and store and needs no local data.

Both commands test the frontend model of the widget and the iframe protocol. They cover the metadata, kernel-style trait changes, the controls, playback, click values, errors, sender rejection and cleanup. They run without a Jupyter kernel.

On 2026-10-02 a JupyterLab test used the real anywidget extensions. The notebook showed the live PNG player. A slider change reached the kernel (`movie.t == 12`). The metadata arrived (`len(movie.times) == 36`). Setting `movie.t = 9` in Python moved the live iframe to that timestep.

## What you get

### The widget

The widget shows only the play button and the time slider. Pass `controls=True` to add the date, product and speed menus. With `product="band"` the widget also shows the band menu and the display limits.

Python and browser controls stay in step, so moving the slider changes `movie.t` and setting `movie.t` moves the slider. The browser sends its state to the kernel at most ten times per second, while the browser controls and the rendering update immediately.

| Attribute | Content |
|-----------|---------|
| `times`, `products`, `bands` | The store metadata, which arrives after the viewer opens the store |
| `ready` | True once the metadata has arrived. Image chunks may still be painting. |
| `t` | The requested timestep |
| `click` | The most recent clicked pixel, with its physical band values and validity. The values belong to the painted timestep. |
| `state` | The viewer state that the viewer last acknowledged, including camera and date |
| `error` | The last viewer error, or `{}` |

### Hosted and local stores

The viewer runs in an iframe and uses the v1 embed contract. The widget accepts a message only when both the origin and the sending window match the viewer. After each change from Python or from the browser, it asks the viewer for its state with `chronozarr:get`. An invalid command produces an `error` and restores the last accepted state.

A hosted store needs no raster server. By default the iframe loads the viewer from `https://chronozarr.org/demo/`, and `viewer=` selects a self-hosted viewer. The store must send CORS headers that allow the origin of that viewer.

The `store` argument can also be a local path. The player serves it with the same range and CORS server as `chronozarr.view`. The browser must be able to reach that server. A remote kernel needs port forwarding, and the browser may ask for local-network permission.

Closing the widget removes its iframe, listeners and timers. The shared local store server keeps the lifecycle that `view()` uses.

## Limits

The notebook must be served over HTTP or HTTPS. Otherwise the widget shows a status message and does not open the store.
