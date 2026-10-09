"""Prepare actual leafmap renderer for the SWOT intensity browser check."""

import json
from pathlib import Path

import leafmap.maplibregl as leafmap

from chronozarr import add_chronozarr

ROOT = Path(__file__).resolve().parents[2]

band = 0
m = leafmap.Map(
    style={"version": 8, "sources": {}, "layers": []},  # ty: ignore[invalid-argument-type]  # leafmap annotates str but passes a style dict through
    controls={},
    height="600px",
    add_sidebar=False,
    add_floating_sidebar=False,
)
m.use_message_queue(False)
add_chronozarr(
    m,
    "http://127.0.0.1:8765/data/stores/swot_intensity/local-20261002",
    name="SWOT intensity (dB)",
    band=band,
    product="band",
    range=[30, 80],
    reader_url="http://127.0.0.1:8765/js/maplibre/layer.js",
)
out = ROOT / "data/reports/swot-intensity"
out.mkdir(parents=True, exist_ok=True)
# anywidget replaces the class-level `_esm` Path with a str trait holding the JS source.
(out / "widget.js").write_text(str(m._esm))
(out / "model.json").write_text(
    json.dumps({"map_options": m.map_options, "calls": m.calls, "height": m.height})
)
