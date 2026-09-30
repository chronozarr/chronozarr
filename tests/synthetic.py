"""Synthetic inputs and independent reference implementations for chronozarr tests."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import xarray as xr

import chronozarr
from chronozarr.encode import EncodeReport

CRS = "EPSG:32631"
TRANSFORM = (10.0, 0.0, 746090.0, 0.0, -10.0, 2540440.0)
BANDS = ["B04", "B08"]


def make_truth(n_time: int, n_band: int, height: int, width: int, seed: int = 7) -> np.ndarray:
    """Smooth field plus noise, a drifting mean per timestep, and a nodata (0) corner."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    base = 2000 + 1500 * np.sin(yy / 90) * np.cos(xx / 70)
    truth = np.zeros((n_time, n_band, height, width), dtype=np.uint16)
    for t in range(n_time):
        for b in range(n_band):
            field = base * (1.0 + 0.8 * b) + 120 * t + rng.normal(0, 60, size=(height, width))
            truth[t, b] = np.clip(field, 1, 65535).astype(np.uint16)
    truth[:, :, height * 9 // 10 :, width * 9 // 10 :] = 0
    return truth


def make_times(n_time: int) -> np.ndarray:
    months = np.arange(n_time).astype("timedelta64[M]")
    return (np.datetime64("2024-01", "M") + months).astype("datetime64[ns]")


def make_da(truth: np.ndarray, bands: list[str] | None = None) -> xr.DataArray:
    n_time, n_band = truth.shape[:2]
    names = bands if bands is not None else [f"B{i:02d}" for i in range(n_band)]
    return xr.DataArray(
        truth,
        dims=("time", "band", "y", "x"),
        coords={"time": make_times(n_time), "band": names},
        attrs={"crs": CRS, "transform": TRANSFORM},
    )


def build_store(
    path: Path,
    truth: np.ndarray,
    *,
    shard: bool,
    anchor_interval: int = 2,
    chunk_size: int = 512,
    n_lods: int | None = None,
) -> EncodeReport:
    return chronozarr.encode(
        make_da(truth, BANDS[: truth.shape[1]] if truth.shape[1] <= len(BANDS) else None),
        path,
        anchor_interval=anchor_interval,
        chunk_size=chunk_size,
        n_lods=n_lods,
        shard=shard,
    )


def reference_downsample(level: np.ndarray) -> np.ndarray:
    """Independent 2x block average of (time, band, y, x): loops, no shared code with encode."""
    n_time, n_band, height, width = level.shape
    out_h, out_w = -(-height // 2), -(-width // 2)
    out = np.zeros((n_time, n_band, out_h, out_w), dtype=np.uint16)
    for t in range(n_time):
        for b in range(n_band):
            for i in range(out_h):
                for j in range(out_w):
                    values = []
                    for di in (0, 1):
                        for dj in (0, 1):
                            y = min(2 * i + di, height - 1)  # edge replication for odd sizes
                            x = min(2 * j + dj, width - 1)
                            values.append(int(level[t, b, y, x]))
                    valid = [v for v in values if v != 0]
                    out[t, b, i, j] = sum(valid) // len(valid) if valid else 0
    return out


def reference_anchor_schedule(n_time: int, interval: int) -> dict[int, int]:
    """Brute force nearest anchor per non-anchor timestep; ties to the earlier anchor."""
    anchors = list(range(0, n_time, interval))
    return {
        t: min(anchors, key=lambda a: (abs(a - t), a)) for t in range(n_time) if t not in anchors
    }
