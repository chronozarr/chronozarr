#!/usr/bin/env python3
"""Benchmark PIL and pyvips image encoding.

Creates a random uint8 (512, 512, 3) array and times 100 iterations
of JPEG and PNG encoding. Reports mean/p50/p95 in milliseconds.
"""

from __future__ import annotations

import argparse
import io
import statistics
import time
from collections.abc import Callable

import numpy as np
from PIL import Image


def benchmark_encode(
    arr: np.ndarray,
    encode_fn: Callable[[np.ndarray], bytes],
    n_iter: int = 100,
) -> dict[str, float]:
    """Run benchmark and return timing statistics in milliseconds."""
    times_ms = []

    # Warmup
    for _ in range(5):
        _ = encode_fn(arr)

    # Timed iterations
    for _ in range(n_iter):
        t0 = time.perf_counter()
        _ = encode_fn(arr)
        t1 = time.perf_counter()
        times_ms.append((t1 - t0) * 1000)

    times_ms.sort()
    return {
        "mean": statistics.mean(times_ms),
        "p50": times_ms[n_iter // 2],
        "p95": times_ms[int(n_iter * 0.95)],
        "min": times_ms[0],
        "max": times_ms[-1],
    }


def pil_jpeg_encode(arr: np.ndarray, quality: int = 85) -> bytes:
    """Encode numpy array as JPEG using PIL."""
    img = Image.fromarray(arr)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def pil_png_encode(arr: np.ndarray) -> bytes:
    """Encode numpy array as PNG using PIL."""
    img = Image.fromarray(arr)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False)
    return buf.getvalue()


def pyvips_jpeg_encode(arr: np.ndarray, quality: int = 85) -> bytes:
    """Encode numpy array as JPEG using pyvips."""
    import pyvips

    # pyvips expects (width, height, bands) interleaved
    # arr is (H, W, C) uint8
    h, w, c = arr.shape
    # Create from memory buffer
    image = pyvips.Image.new_from_memory(arr.tobytes(), w, h, c, "uchar")
    # JPEG save to buffer
    return image.jpegsave_buffer(Q=quality)


def pyvips_png_encode(arr: np.ndarray) -> bytes:
    """Encode numpy array as PNG using pyvips."""
    import pyvips

    h, w, c = arr.shape
    image = pyvips.Image.new_from_memory(arr.tobytes(), w, h, c, "uchar")
    return image.pngsave_buffer()


def format_stats(name: str, stats: dict[str, float]) -> str:
    """Format benchmark results."""
    return (
        f"{name}: "
        f"mean={stats['mean']:.2f}ms "
        f"p50={stats['p50']:.2f}ms "
        f"p95={stats['p95']:.2f}ms "
        f"(min={stats['min']:.2f}ms max={stats['max']:.2f}ms)"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark image encoding performance")
    parser.add_argument(
        "--pyvips",
        action="store_true",
        help="Also benchmark pyvips encoding (requires pyvips installed)",
    )
    parser.add_argument(
        "--iter",
        type=int,
        default=100,
        dest="n_iter",
        help="Number of iterations (default: 100)",
    )
    parser.add_argument(
        "--size",
        type=int,
        default=512,
        help="Tile size (default: 512)",
    )
    args = parser.parse_args()

    # Create random uint8 array simulating a tile
    arr = np.random.randint(0, 256, size=(args.size, args.size, 3), dtype=np.uint8)
    print(f"Benchmarking {args.n_iter} iterations on {args.size}x{args.size} uint8 RGB array")
    print()

    # PIL benchmarks
    print("--- PIL (Pillow) ---")
    jpeg_stats = benchmark_encode(arr, pil_jpeg_encode, args.n_iter)
    print(format_stats("JPEG (quality=85)", jpeg_stats))

    png_stats = benchmark_encode(arr, pil_png_encode, args.n_iter)
    print(format_stats("PNG", png_stats))
    print()

    # pyvips benchmarks (if requested)
    if args.pyvips:
        try:
            import pyvips  # noqa: F401
        except ImportError:
            print("--- pyvips ---")
            print("pyvips not installed, skipping. Install with: pip install pyvips")
            return

        print("--- pyvips ---")
        jpeg_stats_vips = benchmark_encode(
            arr, lambda a: pyvips_jpeg_encode(a, quality=85), args.n_iter
        )
        print(format_stats("JPEG (quality=85)", jpeg_stats_vips))

        png_stats_vips = benchmark_encode(arr, pyvips_png_encode, args.n_iter)
        print(format_stats("PNG", png_stats_vips))


if __name__ == "__main__":
    main()
