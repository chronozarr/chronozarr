"""Experimental: keyframe + temporal delta encoding.

Core idea: instead of storing each month independently, store periodic
keyframes (full multiband uint16) and encode intermediate months as
int16 residuals relative to the previous reconstructed frame.

Encoding scheme:
    keyframe at t=0: full uint16 multiband chunk
    delta at t=1:    int16 residual = bands[t=1] - bands[t=0]
    delta at t=2:    int16 residual = bands[t=2] - reconstructed[t=1]
    ...
    keyframe at t=K: full uint16 (reset point)
    delta at t=K+1:  int16 residual = bands[t=K+1] - bands[t=K]
    ...

Reconstruction of month t:
    1. Find nearest preceding keyframe at t_k
    2. Load keyframe
    3. Accumulate deltas t_k+1 ... t
    4. Result = keyframe + sum(deltas)

Storage structure:
    store_dir/
        {chunk_id}/
            keyframes/
                {month}.zarr   (uint16, n_bands × H × W)
            deltas/
                {month}.zarr   (int16, n_bands × H × W)
            masks/
                {month}.zarr   (uint8, H × W, optional change mask)
            meta.json          (month ordering, keyframe indices)

The access-cost advantage: to render month t, you fetch 1 keyframe +
(t - t_k) deltas, not the entire time stack. For keyframe_interval=6
and t near a keyframe, this is much less than the full stack.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import zarr
from numcodecs import Blosc

from spacetime.chunk import ChunkGrid, extract_chunk

logger = logging.getLogger(__name__)

COMPRESSOR = Blosc(cname="zstd", clevel=5, shuffle=Blosc.BITSHUFFLE)

# Delta values that are exactly 0 compress extremely well (run-length friendly).
# For stable scenes (Sahara), most deltas will be near-zero.
# For seasonal scenes (Iowa), deltas will be larger but still structured.


def encode(
    monthly_mosaics: dict[str, np.ndarray],
    grid: ChunkGrid,
    store_dir: Path,
    keyframe_interval: int = 6,
    change_threshold: int = 0,
) -> dict:
    """Encode monthly mosaics as keyframes + deltas.

    Args:
        monthly_mosaics: "YYYY-MM" → (n_bands, H, W) uint16
        grid: Spatial chunk grid
        store_dir: Output directory
        keyframe_interval: Insert a keyframe every N months (1 = all keyframes)
        change_threshold: If > 0, also store a binary change mask (pixels where
            abs(delta) > threshold in any band). Deltas are still stored for all
            pixels; the mask is metadata for potential future optimizations.

    Returns:
        Dict of metrics and metadata.
    """
    store_dir.mkdir(parents=True, exist_ok=True)
    months = sorted(monthly_mosaics.keys())
    n_months = len(months)

    total_keyframe_bytes = 0
    total_delta_bytes = 0
    total_mask_bytes = 0
    n_keyframes = 0
    n_deltas = 0
    delta_stats: list[dict] = []

    for row, col in grid.chunk_ids:
        chunk_id = grid.chunk_id_str(row, col)
        chunk_dir = store_dir / chunk_id
        kf_dir = chunk_dir / "keyframes"
        delta_dir = chunk_dir / "deltas"
        kf_dir.mkdir(parents=True, exist_ok=True)
        delta_dir.mkdir(parents=True, exist_ok=True)

        if change_threshold > 0:
            mask_dir = chunk_dir / "masks"
            mask_dir.mkdir(parents=True, exist_ok=True)

        prev_reconstructed: np.ndarray | None = None
        keyframe_months: list[str] = []
        delta_months: list[str] = []

        for t, month_key in enumerate(months):
            chunk = extract_chunk(monthly_mosaics[month_key], grid, row, col)
            is_keyframe = (t % keyframe_interval == 0)

            if is_keyframe:
                # Store full keyframe
                kf_path = kf_dir / f"{month_key}.zarr"
                z = zarr.open(
                    str(kf_path), mode="w",
                    shape=chunk.shape, dtype=np.uint16,
                    compressor=COMPRESSOR, chunks=chunk.shape,
                )
                z[:] = chunk
                total_keyframe_bytes += _dir_size(kf_path)
                n_keyframes += 1
                keyframe_months.append(month_key)
                prev_reconstructed = chunk.copy()

            else:
                # Store delta relative to previous reconstructed frame
                delta = chunk.astype(np.int32) - prev_reconstructed.astype(np.int32)
                delta_i16 = delta.clip(-32768, 32767).astype(np.int16)

                d_path = delta_dir / f"{month_key}.zarr"
                z = zarr.open(
                    str(d_path), mode="w",
                    shape=delta_i16.shape, dtype=np.int16,
                    compressor=COMPRESSOR, chunks=delta_i16.shape,
                )
                z[:] = delta_i16
                total_delta_bytes += _dir_size(d_path)
                n_deltas += 1
                delta_months.append(month_key)

                # Track delta statistics (for the first chunk only, to avoid bloat)
                if row == 0 and col == 0:
                    abs_delta = np.abs(delta_i16).astype(np.float32)
                    delta_stats.append({
                        "month": month_key,
                        "mean_abs_delta": float(abs_delta.mean()),
                        "max_abs_delta": int(abs_delta.max()),
                        "pct_zero": float((delta_i16 == 0).mean() * 100),
                        "pct_under_10": float((abs_delta < 10).mean() * 100),
                        "pct_under_50": float((abs_delta < 50).mean() * 100),
                    })

                # Optional change mask
                if change_threshold > 0:
                    changed = np.any(np.abs(delta_i16) > change_threshold, axis=0)
                    mask_path = mask_dir / f"{month_key}.zarr"
                    z = zarr.open(
                        str(mask_path), mode="w",
                        shape=changed.shape, dtype=np.uint8,
                        compressor=COMPRESSOR, chunks=changed.shape,
                    )
                    z[:] = changed.astype(np.uint8)
                    total_mask_bytes += _dir_size(mask_path)

                # Reconstruct for next delta (lossless chain)
                prev_reconstructed = (
                    prev_reconstructed.astype(np.int32) + delta_i16.astype(np.int32)
                ).clip(0, 65535).astype(np.uint16)

        # Save metadata per chunk
        meta = {
            "months": months,
            "keyframe_months": keyframe_months,
            "delta_months": delta_months,
            "keyframe_interval": keyframe_interval,
        }
        with open(chunk_dir / "meta.json", "w") as f:
            json.dump(meta, f, indent=2)

    total_bytes = total_keyframe_bytes + total_delta_bytes + total_mask_bytes

    metrics = {
        "total_bytes": total_bytes,
        "keyframe_bytes": total_keyframe_bytes,
        "delta_bytes": total_delta_bytes,
        "mask_bytes": total_mask_bytes,
        "n_keyframes": n_keyframes,
        "n_deltas": n_deltas,
        "n_chunks": grid.n_chunks,
        "n_months": n_months,
        "keyframe_interval": keyframe_interval,
        "delta_stats": delta_stats,
    }

    logger.info(
        "Experimental (kf_interval=%d): %.2f MB total (kf=%.2f, delta=%.2f, mask=%.2f)",
        keyframe_interval,
        total_bytes / 1e6,
        total_keyframe_bytes / 1e6,
        total_delta_bytes / 1e6,
        total_mask_bytes / 1e6,
    )
    return metrics


def decode(
    store_dir: Path,
    chunk_id: str,
    target_month: str,
) -> np.ndarray:
    """Reconstruct a chunk for a specific month.

    Loads the nearest preceding keyframe and accumulates deltas.

    Args:
        store_dir: Root of the experimental store
        chunk_id: Chunk identifier (e.g., "r000_c000")
        target_month: "YYYY-MM" to reconstruct

    Returns:
        uint16 array of shape (n_bands, H, W)
    """
    chunk_dir = store_dir / chunk_id
    with open(chunk_dir / "meta.json") as f:
        meta = json.load(f)

    months = meta["months"]
    keyframe_months = set(meta["keyframe_months"])

    if target_month not in months:
        raise ValueError(f"Month {target_month} not in store (have {months})")

    # Walk backward to find nearest keyframe
    target_idx = months.index(target_month)
    kf_idx = target_idx
    while kf_idx >= 0 and months[kf_idx] not in keyframe_months:
        kf_idx -= 1

    if kf_idx < 0:
        raise ValueError(f"No keyframe found before {target_month}")

    # Load keyframe
    kf_month = months[kf_idx]
    kf_path = chunk_dir / "keyframes" / f"{kf_month}.zarr"
    z = zarr.open(str(kf_path), mode="r")
    result = np.array(z).astype(np.int32)

    # Accumulate deltas
    for i in range(kf_idx + 1, target_idx + 1):
        delta_month = months[i]
        d_path = chunk_dir / "deltas" / f"{delta_month}.zarr"
        z = zarr.open(str(d_path), mode="r")
        delta = np.array(z).astype(np.int32)
        result += delta

    return result.clip(0, 65535).astype(np.uint16)


def bytes_to_decode(store_dir: Path, chunk_id: str, target_month: str) -> int:
    """Calculate bytes that must be read to reconstruct target_month.

    This is the key access-cost metric: how many bytes must be fetched
    from storage to render one chunk at one point in time?
    """
    chunk_dir = store_dir / chunk_id
    with open(chunk_dir / "meta.json") as f:
        meta = json.load(f)

    months = meta["months"]
    keyframe_months = set(meta["keyframe_months"])

    target_idx = months.index(target_month)
    kf_idx = target_idx
    while kf_idx >= 0 and months[kf_idx] not in keyframe_months:
        kf_idx -= 1

    total = 0
    # Keyframe bytes
    kf_month = months[kf_idx]
    total += _dir_size(chunk_dir / "keyframes" / f"{kf_month}.zarr")

    # Delta bytes
    for i in range(kf_idx + 1, target_idx + 1):
        delta_month = months[i]
        total += _dir_size(chunk_dir / "deltas" / f"{delta_month}.zarr")

    return total


def _dir_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
