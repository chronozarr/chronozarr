"""anywidget traits for the v1 chronozarr embed contract (optional dependency)."""

from pathlib import Path
from urllib.parse import urlsplit

import anywidget
import traitlets as T


class Player(anywidget.AnyWidget):
    _esm = Path(__file__).with_name("player.js")
    store_url = T.Unicode().tag(sync=True)
    viewer_url = T.Unicode("https://chronozarr.org/demo/").tag(sync=True)
    height = T.Int(560, min=320).tag(sync=True)
    theme = T.Enum(["light", "dark"], default_value="light").tag(sync=True)
    t = T.Int(0, min=0).tag(sync=True)
    product = T.Unicode("").tag(sync=True)
    controls = T.Bool(False).tag(sync=True)
    band = T.Unicode("").tag(sync=True)
    range = T.List(T.Float(), default_value=None, allow_none=True).tag(sync=True)
    speed = T.Float(4, min=0.01).tag(sync=True)
    playing = T.Bool(False).tag(sync=True)
    ready = T.Bool(False).tag(sync=True)
    times = T.List(T.Unicode()).tag(sync=True)
    products = T.List(T.Dict()).tag(sync=True)
    bands = T.List(T.Dict()).tag(sync=True)
    state = T.Dict().tag(sync=True)
    click = T.Dict().tag(sync=True)
    error = T.Dict().tag(sync=True)

    @T.validate("store_url", "viewer_url")
    def _validate_url(self, proposal):
        url = urlsplit(proposal["value"])
        if url.scheme not in {"http", "https"} or not url.netloc:
            raise T.TraitError("store and viewer URLs must be absolute HTTP(S) URLs")
        return proposal["value"]

    @T.validate("range")
    def _validate_range(self, proposal):
        import math

        value = proposal["value"]
        if value is not None and (
            len(value) != 2 or not all(math.isfinite(v) for v in value) or value[0] >= value[1]
        ):
            raise T.TraitError("range must be None or two finite increasing physical limits")
        return value
