"""Check, read only, whether the audit's scene counts turn each legacy month's coverage
fraction back into per-pixel counts; writes ucayali-coverage-backfill.json.

    uv run python bench/s2-ingest/coverage_backfill_check.py \\
        data/mosaics/ucayali_santa_maria bench/s2-ingest/audit-ucayali_santa_maria.json \\
        bench/s2-ingest/ucayali-coverage-backfill.json

For each month: n from the audit (a repeated STAC search), whether float32(k / n) reproduces
every stored value bit for bit, the smallest n that does (only that n is accepted by
`ingest.counts_from_fraction`, since any multiple of the true n also fits), whether the month
holds only 0 and 1, and the largest count.
"""

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "examples" / "sentinel2_pc"))
import ingest

mosaic_dir, audit_path, out_path = (Path(a) for a in sys.argv[1:4])
audit = ingest.read_scenes_searched(audit_path)
months = []
for path in sorted(mosaic_dir.glob("*.npz")):
    with np.load(path, allow_pickle=False) as data:
        coverage = data["coverage"]
    n = audit[path.stem]
    values = np.unique(coverage)
    smallest = next(m for m in range(1, 256) if ingest._reproduces(values, m))
    months.append(
        {
            "month": path.stem,
            "n_audit": n,
            "bit_exact_reconstruction": ingest._reproduces(values, n),
            "ambiguous": bool(np.isin(values, (0.0, 1.0)).all()),
            "max_residual": float(np.max(np.abs(values * n - np.rint(values * n)))),
            "n_min_consistent": smallest,
            "max_k": int(np.rint(values.max() * n)),
        }
    )
totals = {
    "months": len(months),
    "bit_exact": sum(m["bit_exact_reconstruction"] for m in months),
    "audit_n_is_smallest": sum(m["n_audit"] == m["n_min_consistent"] for m in months),
    "only_0_and_1": sum(m["ambiguous"] for m in months),
}
out_path.write_text(json.dumps({"totals": totals, "months": months}, indent=1) + "\n")
print(totals)
