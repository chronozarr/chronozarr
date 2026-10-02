"""Generate a browser check from the installed leafmap widget, without a Jupyter server.

uv run --with leafmap python examples/leafmap/prepare_check.py
node examples/leafmap/browser_check.mjs
"""

import json
from pathlib import Path

import leafmap.maplibregl as leafmap
from chronozarr_map import add_chronozarr

m = leafmap.Map(style={"version": 8, "sources": {}, "layers": []}, controls={}, height="600px")
m.use_message_queue(False)
add_chronozarr(
    m,
    url="http://127.0.0.1:8765/data/stores/ucayali_santa_maria/png-1",
    reader_url="http://127.0.0.1:8765/js/maplibre/layer.js",
)
output = Path(__file__).resolve().parents[2] / "data/reports/leafmap"
output.mkdir(parents=True, exist_ok=True)
(output / "widget.js").write_text(m._esm)
(output / "model.json").write_text(
    json.dumps({"map_options": m.map_options, "calls": m.calls, "height": m.height})
)
print(f"Generated leafmap browser check in {output}")
