"""Derive lab-ucayali-x2-5km from lab-ucayali-x2: same scenes and mirror files, 5 km bbox."""

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("bench", REPO / "examples/sentinel2_pc/bench.py")
bench = importlib.util.module_from_spec(spec)
sys.modules["bench"] = bench
spec.loader.exec_module(bench)

workload = bench.load_workload("lab-ucayali-x2")
west, south, east, north = workload["bbox"]
lon, lat = (west + east) / 2, (south + north) / 2
half = bench.SMALL_AOI_HALF_DEGREES
workload["bbox"] = [lon - half, lat - half, lon + half, lat + half]
workload["name"] = "lab-ucayali-x2-5km"
workload["derived_from"] = "lab-ucayali-x2"
path = bench.save_workload(workload)
print(path, workload["bbox"], workload["sha256"])
