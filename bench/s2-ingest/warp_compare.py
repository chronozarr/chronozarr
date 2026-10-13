"""Run one pipeline on the lab warp workload from local mirror files, for warp_compare.

    uv run python bench/s2-ingest/warp_compare.py run pr90 OUT_DIR     # the pipeline at d7c2ce1
    uv run python bench/s2-ingest/warp_compare.py run new OUT_DIR
    uv run python bench/s2-ingest/warp_compare.py run strips OUT_DIR  # 512-row strips
    uv run python bench/s2-ingest/warp_compare.py diff OLD.npz NEW.tif

Each `run` is one process, because the two pipelines share module names.
"""

import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "examples" / "sentinel2_pc"))
import bench
import performance


def run(which: str, out: Path) -> None:
    workload = bench.load_workload("lab-ucayali-x2-warp")
    local = {
        month: [
            dict(
                s,
                asset_hrefs={
                    k: str(bench.MIRROR_DIR / v.split("/", 3)[3])
                    for k, v in s["asset_hrefs"].items()
                },
            )
            for s in scenes
        ]
        for month, scenes in workload["scenes_by_month"].items()
    }
    if which == "pr90":
        catalog, mosaic = bench.load_baseline_modules("d7c2ce1", Path(tempfile.mkdtemp()))
    else:
        import catalog
        import mosaic
    scenes = {
        m: [bench.scene_from_dict(catalog.SceneRef, s) for s in ss] for m, ss in local.items()
    }
    settings = performance.plan_settings(performance.detect_resources(), adaptive=True)
    options = {"strip_rows": 512} if which == "strips" else {}
    mosaic.build_monthly_mosaics(
        scenes,
        bench.as_bbox(workload["bbox"]),
        workload["epsg"],
        out,
        settings=settings,
        sign=str,
        **options,
    )


def diff(old_path: Path, new_path: Path) -> None:
    import mosaic

    old, new = mosaic.load_mosaic(old_path), mosaic.load_mosaic(new_path)
    a, b = old["bands"].astype(np.int32), new["bands"].astype(np.int32)
    both = (a > 0) & (b > 0)
    d = np.abs(a - b)[both]
    one_only = int(((a > 0) != (b > 0)).sum())
    counts = (old["valid_count"] != new["valid_count"]).mean()
    print(f"values differing: {(a != b).mean():.3%}; valid in one only: {one_only}")
    print(
        f"|d| where both valid: mean {d.mean():.4f}, p99.9 {np.percentile(d, 99.9)}, max {d.max()}"
    )
    print(f"pixels whose valid-scene count differs: {counts:.3%}")


if __name__ == "__main__":
    if sys.argv[1] == "run":
        run(sys.argv[2], Path(sys.argv[3]))
    else:
        diff(Path(sys.argv[2]), Path(sys.argv[3]))
