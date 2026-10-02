"""Add a chronozarr GPU layer to leafmap's MapLibre notebook backend.

Call before displaying the map. No tile server or raster conversion is involved.
The bridge wraps the installed py-maplibregl renderer, preserving its basemaps,
controls and Python message queue. Leafmap is optional; importing chronozarr does not import it.
"""

import json
import math
import os
from pathlib import Path
from urllib.parse import urlsplit

from chronozarr.view import serve_store

DEFAULT_STORE = "https://data.tileripper.com/ucayali_santa_maria/chronozarr-4"
DEFAULT_READER = "https://tileripper.com/maplibre/layer.js"


def add_chronozarr(
    map_widget,
    url=DEFAULT_STORE,
    *,
    name="chronozarr",
    t=0,
    product="true_color",
    opacity=1.0,
    band=0,
    range=None,
    fit_bounds=True,
    reader_url=DEFAULT_READER,
    port=0,
):
    """Add a layer and browser time slider; return the supplied leafmap map.

    Use ``import leafmap.maplibregl as leafmap`` and ``m = leafmap.Map()``.
    Local store paths are served by a local CORS/range server. Hosted URLs must
    allow CORS. Remote kernels need port forwarding for local stores.
    ``band`` selects the single-band product; ``range`` sets fixed physical limits.
    Reader URL can point to a local no-cache server for development.
    """
    if getattr(map_widget, "_rendered", False):
        raise ValueError("add_chronozarr must be called before displaying the map")
    if not callable(getattr(map_widget, "add_call", None)):
        raise TypeError("use leafmap.maplibregl.Map, the MapLibre notebook backend")
    if not isinstance(t, int) or isinstance(t, bool) or t < 0:
        raise ValueError("t must be a nonnegative integer timestep")
    if not 0 <= opacity <= 1:
        raise ValueError("opacity must be between 0 and 1")
    if not isinstance(band, int) or isinstance(band, bool) or band < 0:
        raise ValueError("band must be a nonnegative integer")
    if range is not None and (
        len(range) != 2 or not all(math.isfinite(v) for v in range) or range[0] >= range[1]
    ):
        raise ValueError("range must contain two finite increasing limits")
    local = isinstance(url, os.PathLike) or (isinstance(url, str) and not urlsplit(url).scheme)
    if local:
        url = serve_store(os.fspath(url), port=port).url
    for value in (url, reader_url):
        if urlsplit(value).scheme not in {"http", "https"}:
            raise ValueError("store and reader URLs must use HTTP or HTTPS")
    if not getattr(map_widget, "_chronozarr_bridge_installed", False):
        source = map_widget._esm
        if isinstance(source, Path):
            source = source.read_text()
        if not isinstance(source, str) or "export" not in source:
            raise TypeError("expected an anywidget MapLibre renderer with ESM source")
        bridge = Path(__file__).with_name("leafmap.js").read_text()
        map_widget._esm = (
            bridge
            + "\nexport default {render: context => renderBridge(context, "
            + json.dumps(source)
            + ", "
            + json.dumps(reader_url)
            + ")};\n"
        )
        map_widget._chronozarr_bridge_installed = True
    map_widget.add_call(
        "addLayer",
        {
            "type": "chronozarr-notebook",
            "options": {
                "id": name,
                "url": url,
                "t": t,
                "product": product,
                "opacity": opacity,
                "band": band,
                "range": range,
            },
            "fitBounds": fit_bounds,
        },
    )
    return map_widget
