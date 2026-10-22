"""NISAR full-frame local ingest-cache tradeoff; temp files only, no remote reads.
Run: uv run python bench/sar/creation_nisar_cache.py > bench/sar/creation_nisar_results.json
"""

import hashlib
import json
import platform
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]


def worker(source, mode):
    with np.load(source) as cached:
        arrays = {name: cached[name] for name in ("HH", "HV", "mask")}
    hashes = {k: hashlib.sha256(v.tobytes()).hexdigest() for k, v in arrays.items()}
    with tempfile.TemporaryDirectory(prefix="chronozarr-nisar-cache-") as directory:
        path = Path(directory) / "frame.npz"
        start = time.perf_counter()
        (np.savez if mode == "uncompressed" else np.savez_compressed)(path, **arrays)
        write_seconds = time.perf_counter() - start
        start = time.perf_counter()
        # Mirrors the encoder's independent frame and mask iterators.
        with np.load(path) as archive:
            frame = np.stack([archive["HH"], archive["HV"]])
        with np.load(path) as archive:
            mask = archive["mask"]
        read_seconds = time.perf_counter() - start
        actual = {"HH": frame[0], "HV": frame[1], "mask": mask}
        assert hashes == {k: hashlib.sha256(v.tobytes()).hexdigest() for k, v in actual.items()}
        print(
            json.dumps(
                {
                    "write_seconds": write_seconds,
                    "encode_read_seconds": read_seconds,
                    "bytes": path.stat().st_size,
                    "shape": list(mask.shape),
                    "arrays_sha256": hashes,
                    "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                    * (1 if sys.platform == "darwin" else 1024),
                }
            )
        )


if __name__ == "__main__":
    if len(sys.argv) > 1:
        worker(Path(sys.argv[1]), sys.argv[2])
    else:
        sources = sorted((ROOT / "data/cache/nisar/colorado_yuma").glob("*.npz"))
        chosen = [sources[0], sources[len(sources) // 2], sources[-1]]
        results = []
        for source in chosen:
            sidecar = json.loads(source.with_suffix(".json").read_text())
            for repeat in range(3):
                for mode in ("compressed", "uncompressed"):
                    result = json.loads(
                        subprocess.check_output(
                            [sys.executable, __file__, str(source), mode], cwd=ROOT
                        )
                    )
                    results.append(
                        {
                            "source": str(source.relative_to(ROOT)),
                            "valid_fraction": sidecar["valid_fraction"],
                            "start": sidecar["start"],
                            "repeat": repeat,
                            "mode": mode,
                            **result,
                        }
                    )
        print(
            json.dumps(
                {
                    "scope": (
                        "Full spatial first/middle/last cached NISAR windows; warm local "
                        "filesystem, cache write plus encoder frame/mask reads; excludes "
                        "remote HDF5 extraction and Zarr encode; no production change"
                    ),
                    "provenance": {
                        "platform": platform.platform(),
                        "machine": platform.machine(),
                        "python": sys.version,
                        "numpy": np.__version__,
                        "git_head": subprocess.check_output(
                            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
                        ).strip(),
                        "production_source_sha256": hashlib.sha256(
                            (ROOT / "examples/nisar/ingest.py").read_bytes()
                        ).hexdigest(),
                        "inputs_sha256": {
                            str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                            for p in chosen
                        },
                    },
                    "results": results,
                },
                indent=2,
            )
        )
