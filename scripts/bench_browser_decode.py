#!/usr/bin/env python3
"""Generate test chunks for browser decode benchmarking.

Reads real Zarr chunks and re-encodes in multiple codec formats.
bench.html fetches these and measures decode latency in the browser.

Usage:
    uv run python scripts/bench_browser_decode.py
"""

import gzip
import json
from pathlib import Path

import numpy as np
import zarr
from numcodecs import Zstd

STORE_DIR = Path("data/stores/sahara_tamanrasset/cs512")
OUT_DIR = Path("src/spacetime/static/bench")


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Corner, center, far corner — diverse spatial content
    chunk_ids = ["r000_c000", "r003_c003", "r005_c005"]
    zstd = Zstd(level=5)

    manifest = {"chunks": []}

    for chunk_id in chunk_ids:
        zarr_path = STORE_DIR / chunk_id / "stack.zarr"
        if not zarr_path.exists():
            print(f"Skip {chunk_id} — not found")
            continue

        z = zarr.open(str(zarr_path), mode="r")

        for month_idx in range(z.shape[0]):
            data = np.array(z[month_idx])  # (4, 512, 512) uint16
            raw = data.tobytes()
            prefix = f"{chunk_id}_m{month_idx:03d}"

            # Original Blosc(zstd,bitshuffle) bytes from Zarr chunk file
            blosc_raw = (zarr_path / f"{month_idx}.0.0.0").read_bytes()

            # Plain zstd (no Blosc wrapper, no shuffle)
            zstd_raw = bytes(zstd.encode(raw))

            # Gzip (native DecompressionStream in browser)
            gz_raw = gzip.compress(raw, compresslevel=5)

            (OUT_DIR / f"{prefix}_raw.bin").write_bytes(raw)
            (OUT_DIR / f"{prefix}_zstd.bin").write_bytes(zstd_raw)
            (OUT_DIR / f"{prefix}_gzip.bin").write_bytes(gz_raw)

            info = {
                "prefix": prefix,
                "shape": list(data.shape),
                "raw": len(raw),
                "blosc": len(blosc_raw),
                "zstd": len(zstd_raw),
                "gzip": len(gz_raw),
            }

            # Star-delta: month 1+ stores delta vs month 0 anchor
            if month_idx > 0:
                anchor = np.array(z[0])
                delta = data.astype(np.int32) - anchor.astype(np.int32)
                delta_i16 = delta.clip(-32768, 32767).astype(np.int16)
                delta_zstd = bytes(zstd.encode(delta_i16.tobytes()))
                (OUT_DIR / f"{prefix}_zstd_delta.bin").write_bytes(delta_zstd)
                info["zstd_delta"] = len(delta_zstd)

                # Delta characterization
                abs_delta = np.abs(delta_i16).astype(np.float32)
                info["delta_pct_zero"] = round(float((delta_i16 == 0).mean()) * 100, 1)
                info["delta_mean_abs"] = round(float(abs_delta.mean()), 1)

            manifest["chunks"].append(info)

    (OUT_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2))

    # Print summary table
    print(f"\n{'Chunk':<25} {'Raw':>8} {'Blosc':>8} {'Zstd':>8} {'Gzip':>8} {'Δ+Zstd':>8}")
    print("-" * 75)
    for c in manifest["chunks"]:
        delta_s = f"{c['zstd_delta']:>8,}" if "zstd_delta" in c else f"{'—':>8}"
        print(
            f"{c['prefix']:<25} {c['raw']:>8,} {c['blosc']:>8,} "
            f"{c['zstd']:>8,} {c['gzip']:>8,} {delta_s}"
        )

    print("\nCompression ratios (raw / compressed):")
    for codec in ["blosc", "zstd", "gzip"]:
        ratios = [c["raw"] / c[codec] for c in manifest["chunks"]]
        print(f"  {codec:>12}: {np.mean(ratios):.2f}x")
    deltas = [c for c in manifest["chunks"] if "zstd_delta" in c]
    if deltas:
        ratios = [c["raw"] / c["zstd_delta"] for c in deltas]
        print(f"  {'delta+zstd':>12}: {np.mean(ratios):.2f}x")
        pct_zeros = [c["delta_pct_zero"] for c in deltas]
        print(f"\n  Delta stats: {np.mean(pct_zeros):.0f}% zero pixels (avg)")


if __name__ == "__main__":
    main()
