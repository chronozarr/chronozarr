"""Local cached-acquisition monthly compositing: isolated RSS, timings and exact stored hashes.

Run: uv run python bench/sar/creation_benchmark.py > bench/sar/creation_results.json
No network, cache mutation or store writes. macOS ru_maxrss is bytes.
"""

import hashlib
import importlib.util
import json
import platform
import resource
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "s1_creation", ROOT / "examples/sentinel1_pc/ingest.py"
)
ingest = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ingest
spec.loader.exec_module(ingest)


def paths_for_month():
    groups = defaultdict(list)
    for sidecar in sorted((ROOT / "data/cache/sentinel1/ucayali_santa_maria").glob("*.json")):
        record = json.loads(sidecar.read_text())
        if record["valid_fraction"] > 0:
            groups[record["start"][:7]].append((record["start"], sidecar.with_suffix(".npz")))
    return [
        (month, [str(p) for _, p in sorted(entries)])
        for month, entries in sorted(groups.items(), key=lambda pair: (-len(pair[1]), pair[0]))[:3]
    ]


def run_worker(mode, paths):
    def frames():
        for path in paths:
            with np.load(path) as archive:
                yield archive["vv"], archive["vh"]

    start = time.perf_counter()
    if mode == "baseline":
        stacks = list(frames())
        # Frozen original algorithm and retention, independent of the production function.
        shape = stacks[0][0].shape
        total_vv = np.zeros(shape, dtype=np.float64)
        total_vh = np.zeros(shape, dtype=np.float64)
        count = np.zeros(shape, dtype=np.int32)
        for vv, vh in stacks:
            ok = np.isfinite(vv) & np.isfinite(vh)
            total_vv[ok] += vv[ok]
            total_vh[ok] += vh[ok]
            count[ok] += 1
        valid = count > 0
        divisor = np.maximum(count, 1)
        vv, vh = total_vv / divisor, total_vh / divisor
    else:
        vv, vh, valid = ingest.month_mean(frames())
    elapsed = time.perf_counter() - start
    stored = (ingest.stored_db(vv, valid), ingest.stored_db(vh, valid), valid)
    print(
        json.dumps(
            {
                "seconds": elapsed,
                "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                * (1 if sys.platform == "darwin" else 1024),
                "shape": list(valid.shape),
                "acquisitions": len(paths),
                "stored_sha256": [hashlib.sha256(a.tobytes()).hexdigest() for a in stored],
            }
        )
    )


if __name__ == "__main__":
    if len(sys.argv) > 1:
        run_worker(sys.argv[1], json.loads(sys.argv[2]))
    else:
        results = []
        for month, paths in paths_for_month():
            for repeat in range(3):
                for mode in ("baseline", "streaming"):
                    result = json.loads(
                        subprocess.check_output(
                            [sys.executable, __file__, mode, json.dumps(paths)], cwd=ROOT
                        )
                    )
                    results.append({"month": month, "repeat": repeat, "mode": mode, **result})
        inputs = [Path(p) for _, paths in paths_for_month() for p in paths]
        provenance = {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": subprocess.check_output(
                ["sysctl", "-n", "machdep.cpu.brand_string"], text=True
            ).strip()
            if sys.platform == "darwin"
            else platform.processor(),
            "python": sys.version,
            "numpy": np.__version__,
            "git_head": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
            ).strip(),
            "source_sha256": hashlib.sha256(
                (ROOT / "examples/sentinel1_pc/ingest.py").read_bytes()
            ).hexdigest(),
            "inputs_sha256": {
                str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in inputs
            },
        }
        print(
            json.dumps(
                {
                    "provenance": provenance,
                    "scope": (
                        "cached 20 m S1 powers only; excludes remote acquisition, "
                        "NPZ write, percentiles and encoding"
                    ),
                    "results": results,
                },
                indent=2,
            )
        )
