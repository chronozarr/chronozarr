# chronozarr in leafmap

Open `demo.ipynb` in a local notebook with `leafmap` installed. The helper uses
`leafmap.maplibregl.Map`, not leafmap's default ipyleaflet backend:

```python
import leafmap.maplibregl as leafmap
from chronozarr_map import add_chronozarr

m = leafmap.Map(style="positron", height="600px")
add_chronozarr(m)
m
```

Call the helper before displaying the map. It adds the existing `ChronozarrLayer`
and a date slider, fits the map to the store, and preserves leafmap's basemaps and
controls. It reads pixels directly from static storage; no raster tile service is
involved. `url=`, `t=`, `product=`, `opacity=` and `fit_bounds=` are configurable.
Each additional layer needs a unique `name=`.

This is an example adapter, not an upstream leafmap method. Ordinary Python layer
definitions cannot serialize WebGL callbacks. The helper wraps the installed
py-maplibregl anywidget renderer's model interface and reconstructs custom layers
in the browser. It requires a browser that permits module imports from blob URLs
and the hosted reader at `https://tileripper.com/maplibre/layer.js` (override
`reader_url=` to use your own reader). Store URLs need CORS. Local notebook kernels
can use `chronozarr.serve_store`; remote kernels need a URL the browser can reach.

Verification with an installed leafmap widget and local PNG fixture:

```sh
uv run --with leafmap python examples/leafmap/prepare_check.py
node examples/leafmap/browser_check.mjs
```

The check opens the real upstream renderer in Chromium, adds the custom GPU
layer, changes dates, saves a screenshot and runs cleanup. It exercises the
browser bridge without running a Jupyter server; notebook trust/security policy
and kernel comms still need a user notebook smoke test.
