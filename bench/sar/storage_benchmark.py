"""Bounded local SAR encode/storage experiment; never modifies the source stores.

Run: uv run python bench/sar/storage_benchmark.py
Outputs raw subprocess measurements and aggregated medians in storage_results.json.
Lossy cases are explicitly experimental and retain the 0.01 dB scale to isolate rounding.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import resource
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import xarray as xr
import zarr

ROOT = Path(__file__).resolve().parents[2]
STORES = {
    "nisar": ROOT / "data/stores/colorado_yuma/nisar-gcov-v2",
    "sentinel1": ROOT / "data/stores/ucayali_santa_maria/s1-rtc-monthly-v1",
}
CASES = [
    ("baseline", "zstd", 5, 1, 4, 512),
    ("zstd1", "zstd", 1, 1, 4, 512),
    ("zstd3", "zstd", 3, 1, 4, 512),
    ("zstd9", "zstd", 9, 1, 4, 512),
    ("shuffle1", "blosc-zstd-shuffle", 1, 1, 4, 512),
    ("shuffle5", "blosc-zstd-shuffle", 5, 1, 4, 512),
    ("quant005", "zstd", 5, 5, 4, 512),
    ("quant010", "zstd", 5, 10, 4, 512),
    ("worker1", "zstd", 5, 1, 1, 512),
    ("chunk256", "zstd", 5, 1, 4, 256),
]


def sample(sensor):
    path = STORES[sensor]
    root = zarr.open_group(path, mode="r")
    a = root["0/data"]
    indices = [0, a.shape[0] // 2, a.shape[0] - 1]
    # Three real windows distributed across the AOI and acquisition history.
    # Date and position vary together: this is a compression sample, not a time-change study.
    windows = [
        (round((a.shape[2] - 1024) * f), round((a.shape[3] - 1024) * f)) for f in (0.05, 0.5, 0.95)
    ]
    y, x = windows[0]
    values = np.stack(
        [
            a[t, :, yy : yy + 1024, xx : xx + 1024]
            for t, (yy, xx) in zip(indices, windows, strict=True)
        ]
    )
    mask = (
        np.stack(
            [
                root["0/mask"][t, yy : yy + 1024, xx : xx + 1024]
                for t, (yy, xx) in zip(indices, windows, strict=True)
            ]
        )
        if "mask" in root["0"]
        else None
    )
    coverage = (
        np.stack(
            [
                root["0/coverage"][t, yy : yy + 1024, xx : xx + 1024]
                for t, (yy, xx) in zip(indices, windows, strict=True)
            ]
        )
        if "coverage" in root["0"]
        else None
    )
    attrs = dict(root.attrs)
    ts = root["0/time"][:][indices].astype("datetime64[ms]")
    transform = list(a.attrs["transform"])
    transform[2] += x * transform[0]
    transform[5] += y * transform[4]
    da = xr.DataArray(
        values,
        dims=("time", "band", "y", "x"),
        coords={"time": ts, "band": root["0/band"][:]},
        attrs={"crs": a.attrs["crs"], "transform": transform},
    )
    provenance = {
        "root_metadata_sha256": hashlib.sha256((path / "zarr.json").read_bytes()).hexdigest(),
        "sample_sha256": hashlib.sha256(values.tobytes()).hexdigest(),
        "coverage_sha256": hashlib.sha256(coverage.tobytes()).hexdigest()
        if coverage is not None
        else None,
        "mask_sha256": hashlib.sha256(mask.tobytes()).hexdigest() if mask is not None else None,
        "source": str(path.relative_to(ROOT)),
        "source_shape": a.shape,
        "windows_yx": [[yy, xx, 1024, 1024] for yy, xx in windows],
        "geography_note": (
            "Each timestep is a different window, with synthetic shared coordinates; "
            "compression/encode benchmark only."
        ),
        "time_indices": indices,
        "times": ts.astype(str).tolist(),
        "bands": attrs["chronozarr"]["bands"],
        "valid_fraction": float(np.mean(mask > 0))
        if mask is not None
        else float(np.mean(values != 0)),
    }
    return da, mask, coverage, provenance


def worker(sensor, case):
    from chronozarr import encode

    da, mask, coverage, provenance = sample(sensor)
    name, codec, level, quantum, workers, chunk = next(c for c in CASES if c[0] == case)
    original = da.values.copy()
    valid = (
        np.broadcast_to(mask[:, None] > 0, original.shape) if mask is not None else original != 0
    )
    if quantum > 1:
        q = (
            (np.rint(original.astype("float64") / quantum) * quantum)
            .clip(0, 65535)
            .astype("uint16")
        )
        q[~valid] = original[~valid]
        da.values = q
    errors = (da.values.astype("float64") - original)[valid] * 0.01
    with tempfile.TemporaryDirectory(prefix="chronozarr-sar-storage-") as temp:
        out = Path(temp) / "store"
        started = time.perf_counter()
        report = encode(
            da,
            out,
            bands=provenance["bands"],
            mask=mask,
            coverage=coverage,
            codec=codec,
            level=level,
            workers=workers,
            chunk_size=chunk,
            n_lods=3,
        )
        elapsed = time.perf_counter() - started
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # macOS reports bytes, Linux reports KiB. Includes imports and source-window loading.
        peak_mb = rss / (1024**2 if platform.system() == "Darwin" else 1024)
        got = zarr.open_group(out, mode="r")
        exact = np.array_equal(got["0/data"][:], da.values)
        sizes = {}
        for p in out.rglob("*"):
            if p.is_file():
                part = "/".join(p.relative_to(out).parts[:2])
                sizes[part] = sizes.get(part, 0) + p.stat().st_size
        pyramid_hashes = {}
        for lod in range(3):
            for array in ("data", "mask", "coverage"):
                if array in got[str(lod)]:
                    content = got[f"{lod}/{array}"][:]
                    pyramid_hashes[f"{lod}/{array}"] = hashlib.sha256(
                        content.tobytes()
                    ).hexdigest()
        decode = []
        for _ in range(3):
            started = time.perf_counter()
            readback = got["0/data"][:]
            decode.append(time.perf_counter() - started)
            assert np.array_equal(readback, da.values)
        result = dict(
            pyramid_hashes=pyramid_hashes,
            sensor=sensor,
            case=name,
            codec=codec,
            compression_level=level,
            quantum_db=quantum * 0.01,
            workers=workers,
            chunk=chunk,
            encode_s=elapsed,
            peak_process_mb=peak_mb,
            total_bytes=report.total_bytes,
            n_files=report.n_files,
            bytes_by_array=sizes,
            l0_readback_s=decode,
            exact_encoded_roundtrip=exact,
            exact_source=np.array_equal(got["0/data"][:], original),
            max_abs_db=float(np.max(np.abs(errors))),
            rmse_db=float(np.sqrt(np.mean(errors**2))),
            mean_error_db=float(np.mean(errors)),
            source=provenance,
            levels=[vars(item) for item in report.levels],
        )
        print(json.dumps(result))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", nargs=2)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.worker:
        worker(*args.worker)
        return
    result_path = ROOT / "bench/sar/storage_results.json"
    declared_sources = {}
    for sensor, path in STORES.items():
        files = [p for p in path.rglob("*") if p.is_file()]
        declared_sources[sensor] = {
            "total_bytes": sum(p.stat().st_size for p in files),
            "n_files": len(files),
            "root_metadata_sha256": hashlib.sha256((path / "zarr.json").read_bytes()).hexdigest(),
        }
    result = dict(
        declared_sources=declared_sources,
        created_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        hardware=dict(
            platform=platform.platform(),
            machine=platform.machine(),
            cpu_count=os.cpu_count(),
            processor=platform.processor(),
        ),
        runtime={
            p: importlib.metadata.version(p)
            for p in ("numpy", "zarr", "xarray", "chronozarr", "numcodecs")
        },
        git_head=subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        repeats=args.repeats,
        scope=(
            "Local re-encoding only, 3 dates x 1024x1024 pixels at 3 distributed locations "
            "x 2 bands per sensor, 3 pyramid levels; ingestion/network excluded. "
            "Source reads and imports excluded from encode timing; process peak RSS includes "
            "them. OS caches warm, no fsync. Quantization errors are relative to existing "
            "0.01 dB stored values, not raw sensor powers. No visual or scientific acceptance "
            "of lossy cases."
        ),
        raw=[],
        summary=[],
    )
    for repeat in range(args.repeats):
        # Rotate case order to reduce monotonic thermal/cache order bias.
        cases = CASES[repeat:] + CASES[:repeat]
        for sensor in STORES:
            for case in cases:
                proc = subprocess.run(
                    [sys.executable, str(Path(__file__).resolve()), "--worker", sensor, case[0]],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                row = json.loads(proc.stdout)
                row["repeat"] = repeat
                result["raw"].append(row)
                result_path.write_text(json.dumps(result, indent=2) + "\n")
                print(
                    sensor,
                    case[0],
                    repeat,
                    round(row["encode_s"], 3),
                    row["total_bytes"],
                    flush=True,
                )
    for sensor in STORES:
        for case in CASES:
            rows = [r for r in result["raw"] if r["sensor"] == sensor and r["case"] == case[0]]
            result["summary"].append(
                dict(
                    sensor=sensor,
                    case=case[0],
                    encode_s=statistics.median(r["encode_s"] for r in rows),
                    peak_process_mb=statistics.median(r["peak_process_mb"] for r in rows),
                    total_bytes=rows[0]["total_bytes"],
                    n_files=rows[0]["n_files"],
                    l0_readback_s=statistics.median(t for r in rows for t in r["l0_readback_s"]),
                    exact_source=all(r["exact_source"] for r in rows),
                    max_abs_db=rows[0]["max_abs_db"],
                    rmse_db=rows[0]["rmse_db"],
                )
            )
    result["fidelity_checks"] = []
    for sensor in STORES:
        base = next(r for r in result["raw"] if r["sensor"] == sensor and r["case"] == "baseline")
        for row in result["raw"]:
            if row["sensor"] == sensor and row["quantum_db"] == 0.01:
                result["fidelity_checks"].append(
                    dict(
                        sensor=sensor,
                        case=row["case"],
                        repeat=row["repeat"],
                        all_data_mask_coverage_levels_equal_baseline=row["pyramid_hashes"]
                        == base["pyramid_hashes"],
                    )
                )
        baseline = next(
            r for r in result["summary"] if r["sensor"] == sensor and r["case"] == "baseline"
        )
        for row in result["summary"]:
            if row["sensor"] == sensor:
                row["storage_savings_pct"] = 100 * (
                    1 - row["total_bytes"] / baseline["total_bytes"]
                )
    result_path.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
