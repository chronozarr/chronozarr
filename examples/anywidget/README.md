# Notebook player

`chronozarr.player` shows a chronozarr store in a notebook widget with a time slider and a play button. You can set the timestep, the product and the speed from Python, and you can read the pixel that you clicked. The `chronozarr` 0.4.0 release on PyPI includes the player. You need a trusted local Jupyter notebook, VS Code notebook or compatible anywidget host. The host must allow an iframe and serve the notebook over HTTP or HTTPS.

## Run it

1. Install the extra with `uv add 'chronozarr[notebook]'`. In this checkout, run `uv sync --extra notebook` instead.
2. Open `demo.ipynb`. It opens a public store of 36 rendered PNG frames of the Ucayali River, one for each month from 2019-01 to 2021-12.
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

The widget shows only the play button and the time slider. Pass `controls=True` to add the date, product and speed menus. The attributes `times`, `products`, `bands` and `ready` arrive after the viewer opens the store. `ready` does not mean that every image chunk has painted. During playback `t` is the requested timestep, and the values in `click` belong to the painted timestep. The browser sends its state to the kernel at most ten times per second.

The viewer runs in an iframe, and the widget accepts messages only from its exact origin and window. By default the iframe loads the viewer from `https://chronozarr.org/demo/`. Set `viewer=` to use a self-hosted viewer, and make the store send CORS headers that allow its origin. A local store path works as the first argument. The player serves it with the same range and CORS server as `chronozarr.view`. The browser may ask for local-network permission. A remote kernel needs `viewer_dir=`, `base_url=` or port forwarding. If the widget cannot open the store, its `hint` attribute says what the server has seen and what to try. See [Remote notebooks](../../docs/python.md#remote-notebooks).

## Check it in a browser

```sh
node examples/anywidget/browser_check.mjs
node examples/anywidget/browser_check.mjs --live
```

Both commands need Node.js and `npm ci --prefix js`, and they run without a Jupyter kernel. The first opens the local store `data/stores/ucayali_santa_maria/png-1`. Build it with `examples/png_frames/render_frames.py` and `examples/png_frames/convert.sh`, which read the monthly mosaics from the [Sentinel-2 example](../sentinel2_pc/README.md). The second command uses the hosted viewer and store.

On 2026-10-02 a JupyterLab test used the real anywidget extensions. A slider change reached the kernel (`movie.t == 12`), and `movie.t = 9` in Python moved the live iframe.
