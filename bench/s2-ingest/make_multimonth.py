"""Derive lab-ucayali-4mo: the 10 mirrored Ucayali scenes served under 4 aliases, one per month.

Four months of 10 scenes each on the full Ucayali grid, for multi-month memory tests without
new downloads. The months hold the same pixels, so each month's composite is the same.
"""

import importlib.util
import sys
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("bench", REPO / "examples/sentinel2_pc/bench.py")
assert spec is not None and spec.loader is not None
bench = importlib.util.module_from_spec(spec)
sys.modules["bench"] = bench
spec.loader.exec_module(bench)

source = bench.load_workload("ucayali-1m")
scenes = source["scenes_by_month"]["2024-07"]
months = ["2024-07", "2024-08", "2024-09", "2024-10"]
by_month = {}
for copy, month in enumerate(months):
    by_month[month] = []
    for scene in scenes:
        entry = dict(scene)
        entry["item_id"] = f"{scene['item_id']}-a{copy}"
        entry["asset_hrefs"] = {
            name: f"lab://a{copy}/{urlsplit(href).path.lstrip('/')}"
            for name, href in scene["asset_hrefs"].items()
        }
        by_month[month].append(entry)
workload = {**source, "name": "lab-ucayali-4mo", "lab": True, "scenes_by_month": by_month}
workload["derived_from"] = "ucayali-1m"
print(bench.save_workload(workload))
