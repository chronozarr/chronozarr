"""Prepare actual leafmap renderer for the NISAR browser check."""

import json
import sys
from pathlib import Path

import leafmap.maplibregl as leafmap

from chronozarr import add_chronozarr

ROOT = Path(__file__).resolve().parents[2]

band = 1 if "--hv" in sys.argv else 0
m = leafmap.Map(
    style={"version": 8, "sources": {}, "layers": []},
    controls={},
    height="600px",
    add_sidebar=False,
    add_floating_sidebar=False,
)
m.use_message_queue(False)
add_chronozarr(
    m,
    "http://127.0.0.1:8765/data/stores/nisar/local-20261002",
    name="NISAR HV (dB)" if band else "NISAR HH (dB)",
    band=band,
    product="band",
    range=[-25, 0],
    reader_url="http://127.0.0.1:8765/js/maplibre/layer.js",
)
out = ROOT / "data/reports/nisar"
out.mkdir(parents=True, exist_ok=True)
(out / "widget.js").write_text(m._esm)
(out / "model.json").write_text(
    json.dumps({"map_options": m.map_options, "calls": m.calls, "height": m.height})
)
