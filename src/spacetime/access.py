"""Access pattern simulation for benchmarking.

Simulates realistic map interaction patterns and measures bytes touched
and decode latency for each representation.

Access patterns:
    1. cold_viewport: Load a 3x3 chunk viewport at month t from scratch
    2. pan: Shift viewport by 1 chunk in a direction (reuse 6, fetch 3)
    3. time_scrub: Same viewport, advance by 1 month
    4. time_jump: Same viewport, jump N months forward/backward
    5. product_switch: Same viewport + month, switch rendered product
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from spacetime.chunk import ChunkGrid

logger = logging.getLogger(__name__)


@dataclass
class AccessResult:
    """Result of a single access operation."""

    pattern: str
    bytes_fetched: int
    decode_time_ms: float
    n_chunks_fetched: int
    month: str
    product: str
    chunk_ids: list[str] = field(default_factory=list)


def viewport_chunk_ids(
    grid: ChunkGrid, center_row: int, center_col: int, radius: int = 1
) -> list[tuple[int, int]]:
    """Return chunk IDs for a viewport centered at (center_row, center_col).

    Default radius=1 gives a 3x3 viewport.
    """
    ids = []
    for dr in range(-radius, radius + 1):
        for dc in range(-radius, radius + 1):
            r, c = center_row + dr, center_col + dc
            if 0 <= r < grid.n_rows and 0 <= c < grid.n_cols:
                ids.append((r, c))
    return ids


def sim_cold_viewport_baseline_a(
    store_dir: Path,
    grid: ChunkGrid,
    center_row: int,
    center_col: int,
    month: str,
    product: str,
) -> AccessResult:
    """Simulate loading a viewport from Baseline A (prerendered PNGs)."""
    from spacetime.encode.baseline_a import bytes_for_tile, decode_tile

    vp = viewport_chunk_ids(grid, center_row, center_col)
    total_bytes = 0
    t0 = time.perf_counter()
    for r, c in vp:
        cid = grid.chunk_id_str(r, c)
        total_bytes += bytes_for_tile(store_dir, product, month, cid)
        _ = decode_tile(store_dir, product, month, cid)
    elapsed = (time.perf_counter() - t0) * 1000

    return AccessResult(
        pattern="cold_viewport",
        bytes_fetched=total_bytes,
        decode_time_ms=elapsed,
        n_chunks_fetched=len(vp),
        month=month,
        product=product,
        chunk_ids=[grid.chunk_id_str(r, c) for r, c in vp],
    )


def sim_cold_viewport_baseline_b1(
    store_dir: Path,
    grid: ChunkGrid,
    center_row: int,
    center_col: int,
    month: str,
    product: str,
) -> AccessResult:
    """Simulate loading a viewport from Baseline B1 (independent multiband Zarr)."""
    from spacetime.encode.baseline_b import bytes_for_b1_tile, decode_b1
    from spacetime.render import render_product

    vp = viewport_chunk_ids(grid, center_row, center_col)
    total_bytes = 0
    t0 = time.perf_counter()
    for r, c in vp:
        cid = grid.chunk_id_str(r, c)
        total_bytes += bytes_for_b1_tile(store_dir, cid, month)
        bands = decode_b1(store_dir, cid, month)
        _ = render_product(bands, product)
    elapsed = (time.perf_counter() - t0) * 1000

    return AccessResult(
        pattern="cold_viewport",
        bytes_fetched=total_bytes,
        decode_time_ms=elapsed,
        n_chunks_fetched=len(vp),
        month=month,
        product=product,
        chunk_ids=[grid.chunk_id_str(r, c) for r, c in vp],
    )


def sim_cold_viewport_experimental(
    store_dir: Path,
    grid: ChunkGrid,
    center_row: int,
    center_col: int,
    month: str,
    product: str,
) -> AccessResult:
    """Simulate loading a viewport from the experimental store."""
    from spacetime.encode.experimental import bytes_to_decode, decode
    from spacetime.render import render_product

    vp = viewport_chunk_ids(grid, center_row, center_col)
    total_bytes = 0
    t0 = time.perf_counter()
    for r, c in vp:
        cid = grid.chunk_id_str(r, c)
        total_bytes += bytes_to_decode(store_dir, cid, month)
        bands = decode(store_dir, cid, month)
        _ = render_product(bands, product)
    elapsed = (time.perf_counter() - t0) * 1000

    return AccessResult(
        pattern="cold_viewport",
        bytes_fetched=total_bytes,
        decode_time_ms=elapsed,
        n_chunks_fetched=len(vp),
        month=month,
        product=product,
        chunk_ids=[grid.chunk_id_str(r, c) for r, c in vp],
    )


def sim_time_scrub_experimental(
    store_dir: Path,
    grid: ChunkGrid,
    center_row: int,
    center_col: int,
    months: list[str],
    product: str,
) -> list[AccessResult]:
    """Simulate scrubbing through time on the experimental store.

    Returns one AccessResult per month transition.
    """
    from spacetime.encode.experimental import bytes_to_decode, decode
    from spacetime.render import render_product

    vp = viewport_chunk_ids(grid, center_row, center_col)
    results = []

    for month in months:
        total_bytes = 0
        t0 = time.perf_counter()
        for r, c in vp:
            cid = grid.chunk_id_str(r, c)
            total_bytes += bytes_to_decode(store_dir, cid, month)
            bands = decode(store_dir, cid, month)
            _ = render_product(bands, product)
        elapsed = (time.perf_counter() - t0) * 1000

        results.append(
            AccessResult(
                pattern="time_scrub",
                bytes_fetched=total_bytes,
                decode_time_ms=elapsed,
                n_chunks_fetched=len(vp),
                month=month,
                product=product,
                chunk_ids=[grid.chunk_id_str(r, c) for r, c in vp],
            )
        )

    return results


def sim_product_switch(
    store_dir: Path,
    grid: ChunkGrid,
    center_row: int,
    center_col: int,
    month: str,
    products: list[str],
    representation: str,
) -> list[AccessResult]:
    """Simulate switching products on same viewport.

    For Baseline A, each product switch fetches new files.
    For B1/B2/Experimental, bands are already loaded — only re-render cost.
    """
    from spacetime.render import render_product

    vp = viewport_chunk_ids(grid, center_row, center_col)
    results = []

    if representation == "baseline_a":
        from spacetime.encode.baseline_a import bytes_for_tile, decode_tile

        for product in products:
            total_bytes = 0
            t0 = time.perf_counter()
            for r, c in vp:
                cid = grid.chunk_id_str(r, c)
                total_bytes += bytes_for_tile(store_dir, product, month, cid)
                _ = decode_tile(store_dir, product, month, cid)
            elapsed = (time.perf_counter() - t0) * 1000
            results.append(
                AccessResult(
                    pattern="product_switch",
                    bytes_fetched=total_bytes,
                    decode_time_ms=elapsed,
                    n_chunks_fetched=len(vp),
                    month=month,
                    product=product,
                )
            )
    else:
        # For multiband stores, bands fetched once, product is just re-render
        # First product: full fetch
        # Subsequent products: zero additional bytes, only render cost
        for i, product in enumerate(products):
            if i == 0:
                if representation == "baseline_b1":
                    from spacetime.encode.baseline_b import bytes_for_b1_tile, decode_b1

                    total_bytes = 0
                    t0 = time.perf_counter()
                    cached_bands = {}
                    for r, c in vp:
                        cid = grid.chunk_id_str(r, c)
                        total_bytes += bytes_for_b1_tile(store_dir, cid, month)
                        cached_bands[cid] = decode_b1(store_dir, cid, month)
                        _ = render_product(cached_bands[cid], product)
                    elapsed = (time.perf_counter() - t0) * 1000
                elif representation == "experimental":
                    from spacetime.encode.experimental import bytes_to_decode, decode

                    total_bytes = 0
                    t0 = time.perf_counter()
                    cached_bands = {}
                    for r, c in vp:
                        cid = grid.chunk_id_str(r, c)
                        total_bytes += bytes_to_decode(store_dir, cid, month)
                        cached_bands[cid] = decode(store_dir, cid, month)
                        _ = render_product(cached_bands[cid], product)
                    elapsed = (time.perf_counter() - t0) * 1000
            else:
                # Subsequent products: only render from cached bands
                total_bytes = 0
                t0 = time.perf_counter()
                for r, c in vp:
                    cid = grid.chunk_id_str(r, c)
                    _ = render_product(cached_bands[cid], product)
                elapsed = (time.perf_counter() - t0) * 1000

            results.append(
                AccessResult(
                    pattern="product_switch",
                    bytes_fetched=total_bytes,
                    decode_time_ms=elapsed,
                    n_chunks_fetched=len(vp) if i == 0 else 0,
                    month=month,
                    product=product,
                )
            )

    return results
