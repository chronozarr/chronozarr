"""Experimental V2: brightness + normalized spectral decomposition.

Hypothesis: raw monthly reflectance deltas are large because atmospheric
and illumination changes dominate. If we decompose into:
    brightness = mean(all bands)          -- 1 channel, captures illumination
    norm_bands = band / brightness        -- 4 channels, captures spectral shape

then the spectral-shape deltas should be much smaller and more compressible,
while brightness concentrates the atmospheric noise into a single channel.

This tests whether the delta-coding failure is structural (temporal change
is genuinely high) or representational (the raw reflectance domain amplifies
atmospheric noise across all bands).

Encoding:
    For each chunk-month:
        brightness: float32 (H, W)
        norm_bands: float32 (4, H, W), values in [0, 1]
    Keyframe: full brightness + norm_bands
    Delta: int16 quantized residuals for each channel separately

Reconstruction:
    bands_reconstructed = norm_bands_reconstructed * brightness_reconstructed
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

# Quantization scale for normalized bands: store as int16 with this precision
# norm values are [0, 1], so scale of 10000 gives 0.0001 precision
NORM_SCALE = 10000
# Brightness scale: store as int16 delta with this precision
# brightness is ~1000-10000, delta might be -5000 to +5000
BRIGHT_SCALE = 1


def decompose(bands: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Decompose multiband uint16 into brightness + normalized spectral shape.

    Args:
        bands: uint16 (n_bands, H, W)

    Returns:
        brightness: float32 (H, W) — mean band value
        norm_bands: float32 (n_bands, H, W) — each band / brightness, [0, 1]
    """
    bands_f = bands.astype(np.float32)
    brightness = bands_f.mean(axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        norm_bands = np.where(
            brightness[np.newaxis, :, :] > 0,
            bands_f / brightness[np.newaxis, :, :],
            0.0,
        )
    return brightness, norm_bands


def recompose(brightness: np.ndarray, norm_bands: np.ndarray) -> np.ndarray:
    """Recompose bands from brightness + normalized spectral shape.

    Returns:
        uint16 (n_bands, H, W)
    """
    bands_f = norm_bands * brightness[np.newaxis, :, :]
    return np.clip(bands_f, 0, 65535).astype(np.uint16)


def analyze_decomposition(
    monthly_mosaics: dict[str, np.ndarray],
    grid: ChunkGrid,
    chunk_row: int = 0,
    chunk_col: int = 0,
) -> list[dict]:
    """Analyze delta entropy in raw vs decomposed domain.

    Returns per-month-transition stats comparing raw deltas to decomposed deltas.
    """
    months = sorted(monthly_mosaics.keys())
    stats = []

    prev_chunk = None
    prev_bright = None
    prev_norm = None

    for month in months:
        chunk = extract_chunk(monthly_mosaics[month], grid, chunk_row, chunk_col)
        bright, norm = decompose(chunk)

        if prev_chunk is not None:
            # Raw domain delta
            raw_delta = chunk.astype(np.int32) - prev_chunk.astype(np.int32)
            raw_abs = np.abs(raw_delta).astype(np.float32)

            # Brightness delta
            bright_delta = bright - prev_bright
            bright_abs = np.abs(bright_delta)

            # Normalized spectral delta
            norm_delta = norm - prev_norm
            norm_abs = np.abs(norm_delta)

            # Compression ratios (proxy for entropy)
            compressor = Blosc(cname="zstd", clevel=5, shuffle=Blosc.BITSHUFFLE)

            raw_i16 = raw_delta.clip(-32768, 32767).astype(np.int16)
            raw_comp = len(compressor.encode(raw_i16))

            bright_i16 = (bright_delta * BRIGHT_SCALE).clip(-32768, 32767).astype(np.int16)
            bright_comp = len(compressor.encode(bright_i16))

            norm_i16 = (norm_delta * NORM_SCALE).clip(-32768, 32767).astype(np.int16)
            norm_comp = len(compressor.encode(norm_i16))

            total_decomposed_comp = bright_comp + norm_comp

            stats.append(
                {
                    "month": month,
                    "raw_mean_abs": float(raw_abs.mean()),
                    "bright_mean_abs": float(bright_abs.mean()),
                    "norm_mean_abs": float(norm_abs.mean()),
                    "raw_compressed_bytes": raw_comp,
                    "bright_compressed_bytes": bright_comp,
                    "norm_compressed_bytes": norm_comp,
                    "decomposed_total_bytes": total_decomposed_comp,
                    "ratio_decomposed_vs_raw": total_decomposed_comp / max(raw_comp, 1),
                    "norm_pct_under_001": float((norm_abs < 0.01).mean() * 100),
                    "norm_pct_under_005": float((norm_abs < 0.05).mean() * 100),
                }
            )

        prev_chunk = chunk
        prev_bright = bright
        prev_norm = norm

    return stats


def encode(
    monthly_mosaics: dict[str, np.ndarray],
    grid: ChunkGrid,
    store_dir: Path,
    keyframe_interval: int = 6,
) -> dict:
    """Encode using brightness + normalized spectral decomposition.

    Keyframes store full brightness (float32) + norm_bands (int16 scaled).
    Deltas store int16 residuals for brightness and norm_bands separately.
    """
    store_dir.mkdir(parents=True, exist_ok=True)
    months = sorted(monthly_mosaics.keys())
    n_months = len(months)

    total_keyframe_bytes = 0
    total_delta_bytes = 0
    n_keyframes = 0
    n_deltas = 0

    for row, col in grid.chunk_ids:
        chunk_id = grid.chunk_id_str(row, col)
        chunk_dir = store_dir / chunk_id
        kf_dir = chunk_dir / "keyframes"
        delta_dir = chunk_dir / "deltas"
        kf_dir.mkdir(parents=True, exist_ok=True)
        delta_dir.mkdir(parents=True, exist_ok=True)

        prev_bright: np.ndarray | None = None
        prev_norm: np.ndarray | None = None
        keyframe_months: list[str] = []
        delta_months: list[str] = []

        for t, month_key in enumerate(months):
            chunk = extract_chunk(monthly_mosaics[month_key], grid, row, col)
            bright, norm = decompose(chunk)

            # Quantize for storage
            bright_q = bright.astype(np.float32)
            norm_q = (norm * NORM_SCALE).clip(0, 32767).astype(np.int16)

            is_keyframe = t % keyframe_interval == 0

            if is_keyframe:
                # Store keyframe: brightness (float32) + norm (int16)
                kf_path = kf_dir / f"{month_key}.zarr"
                root = zarr.open(str(kf_path), mode="w")
                root.create_dataset(
                    "brightness",
                    data=bright_q,
                    compressor=COMPRESSOR,
                    chunks=bright_q.shape,
                )
                root.create_dataset(
                    "norm",
                    data=norm_q,
                    compressor=COMPRESSOR,
                    chunks=norm_q.shape,
                )
                total_keyframe_bytes += _dir_size(kf_path)
                n_keyframes += 1
                keyframe_months.append(month_key)
                prev_bright = bright_q.copy()
                prev_norm = norm_q.copy()
            else:
                # Delta: residuals for brightness and norm separately
                bright_delta = (bright_q - prev_bright).astype(np.float32)
                bright_delta_i16 = bright_delta.clip(-32768, 32767).astype(np.int16)

                norm_delta = norm_q.astype(np.int32) - prev_norm.astype(np.int32)
                norm_delta_i16 = norm_delta.clip(-32768, 32767).astype(np.int16)

                d_path = delta_dir / f"{month_key}.zarr"
                root = zarr.open(str(d_path), mode="w")
                root.create_dataset(
                    "brightness",
                    data=bright_delta_i16,
                    compressor=COMPRESSOR,
                    chunks=bright_delta_i16.shape,
                )
                root.create_dataset(
                    "norm",
                    data=norm_delta_i16,
                    compressor=COMPRESSOR,
                    chunks=norm_delta_i16.shape,
                )
                total_delta_bytes += _dir_size(d_path)
                n_deltas += 1
                delta_months.append(month_key)

                # Reconstruct for next delta
                prev_bright = prev_bright + bright_delta_i16.astype(np.float32)
                prev_norm = (
                    (prev_norm.astype(np.int32) + norm_delta_i16.astype(np.int32))
                    .clip(0, 32767)
                    .astype(np.int16)
                )

        meta = {
            "months": months,
            "keyframe_months": keyframe_months,
            "delta_months": delta_months,
            "keyframe_interval": keyframe_interval,
            "encoding": "brightness_norm_v2",
        }
        with open(chunk_dir / "meta.json", "w") as f:
            json.dump(meta, f, indent=2)

    total_bytes = total_keyframe_bytes + total_delta_bytes

    metrics = {
        "total_bytes": total_bytes,
        "keyframe_bytes": total_keyframe_bytes,
        "delta_bytes": total_delta_bytes,
        "n_keyframes": n_keyframes,
        "n_deltas": n_deltas,
        "n_chunks": grid.n_chunks,
        "n_months": n_months,
        "keyframe_interval": keyframe_interval,
        "variant": f"v2_kf{keyframe_interval}",
    }

    logger.info(
        "V2 Experimental (kf_interval=%d): %.2f MB (kf=%.2f, delta=%.2f)",
        keyframe_interval,
        total_bytes / 1e6,
        total_keyframe_bytes / 1e6,
        total_delta_bytes / 1e6,
    )
    return metrics


def decode(store_dir: Path, chunk_id: str, target_month: str) -> np.ndarray:
    """Reconstruct a chunk, returning uint16 (n_bands, H, W).

    Note: recomposition introduces small quantization error from
    brightness * norm_bands rounding. This is the expected lossy tradeoff.
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

    # Load keyframe
    kf_month = months[kf_idx]
    kf_path = chunk_dir / "keyframes" / f"{kf_month}.zarr"
    root = zarr.open(str(kf_path), mode="r")
    brightness = np.array(root["brightness"]).astype(np.float32)
    norm = np.array(root["norm"]).astype(np.int32)

    # Accumulate deltas
    for i in range(kf_idx + 1, target_idx + 1):
        d_month = months[i]
        d_path = chunk_dir / "deltas" / f"{d_month}.zarr"
        root = zarr.open(str(d_path), mode="r")
        brightness += np.array(root["brightness"]).astype(np.float32)
        norm += np.array(root["norm"]).astype(np.int32)

    norm = norm.clip(0, 32767).astype(np.float32) / NORM_SCALE
    return recompose(brightness, norm)


def bytes_to_decode(store_dir: Path, chunk_id: str, target_month: str) -> int:
    """Bytes needed to reconstruct target_month."""
    chunk_dir = store_dir / chunk_id
    with open(chunk_dir / "meta.json") as f:
        meta = json.load(f)

    months = meta["months"]
    keyframe_months = set(meta["keyframe_months"])

    target_idx = months.index(target_month)
    kf_idx = target_idx
    while kf_idx >= 0 and months[kf_idx] not in keyframe_months:
        kf_idx -= 1

    total = _dir_size(chunk_dir / "keyframes" / f"{months[kf_idx]}.zarr")
    for i in range(kf_idx + 1, target_idx + 1):
        total += _dir_size(chunk_dir / "deltas" / f"{months[i]}.zarr")
    return total


def _dir_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
