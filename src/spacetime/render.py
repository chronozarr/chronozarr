"""Band math: derive visual products from multiband Sentinel-2 data.

All functions are pure numpy. Input is uint16 surface reflectance
(scaled by 10000). Output is uint8 RGB or float32 index.

Band ordering convention (matching REQUIRED_BANDS in catalog.py):
    0=B02(Blue), 1=B03(Green), 2=B04(Red), 3=B08(NIR)
"""

from __future__ import annotations

import numpy as np

# Band indices
B02, B03, B04, B08 = 0, 1, 2, 3


def true_color(bands: np.ndarray, percentile_stretch: tuple[float, float] = (2, 98)) -> np.ndarray:
    """Render true-color RGB from multiband data.

    Args:
        bands: uint16 array of shape (4, H, W) — [B02, B03, B04, B08]
        percentile_stretch: Low/high percentiles for contrast stretch

    Returns:
        uint8 array of shape (H, W, 3) — RGB
    """
    # R=B04, G=B03, B=B02
    rgb = np.stack([bands[B04], bands[B03], bands[B02]], axis=0).astype(np.float32)
    return _stretch_to_uint8(rgb, percentile_stretch)


def false_color_nir(
    bands: np.ndarray, percentile_stretch: tuple[float, float] = (2, 98)
) -> np.ndarray:
    """Render NIR false-color composite (NIR, Red, Green).

    Args:
        bands: uint16 array of shape (4, H, W)
        percentile_stretch: Low/high percentiles for contrast stretch

    Returns:
        uint8 array of shape (H, W, 3) — RGB
    """
    rgb = np.stack([bands[B08], bands[B04], bands[B03]], axis=0).astype(np.float32)
    return _stretch_to_uint8(rgb, percentile_stretch)


def ndvi(bands: np.ndarray) -> np.ndarray:
    """Compute NDVI = (NIR - Red) / (NIR + Red).

    Args:
        bands: uint16 array of shape (4, H, W)

    Returns:
        float32 array of shape (H, W), range [-1, 1]. NaN where both bands are 0.
    """
    nir = bands[B08].astype(np.float32)
    red = bands[B04].astype(np.float32)
    denom = nir + red
    with np.errstate(invalid="ignore", divide="ignore"):
        result = np.where(denom > 0, (nir - red) / denom, np.nan)
    return result


def ndwi(bands: np.ndarray) -> np.ndarray:
    """Compute NDWI = (Green - NIR) / (Green + NIR).

    Args:
        bands: uint16 array of shape (4, H, W)

    Returns:
        float32 array of shape (H, W), range [-1, 1]. NaN where both bands are 0.
    """
    green = bands[B03].astype(np.float32)
    nir = bands[B08].astype(np.float32)
    denom = green + nir
    with np.errstate(invalid="ignore", divide="ignore"):
        result = np.where(denom > 0, (green - nir) / denom, np.nan)
    return result


def ndvi_colormap(ndvi_arr: np.ndarray) -> np.ndarray:
    """Map NDVI values to a diverging brown-green colormap.

    Args:
        ndvi_arr: float32 (H, W), range [-1, 1]

    Returns:
        uint8 (H, W, 3) RGB
    """
    # Normalize to [0, 1]
    norm = np.clip((ndvi_arr + 1.0) / 2.0, 0, 1)
    # Simple diverging: brown (low NDVI) → white (0) → green (high NDVI)
    r = np.where(norm < 0.5, 180 + 150 * (0.5 - norm) / 0.5, 180 * (1.0 - norm) / 0.5)
    g = np.where(norm < 0.5, 100 * norm / 0.5, 100 + 155 * (norm - 0.5) / 0.5)
    b = np.where(norm < 0.5, 50 * norm / 0.5, 50 * (1.0 - norm) / 0.5)
    rgb = np.stack([r, g, b], axis=-1)
    rgb = np.clip(rgb, 0, 255).astype(np.uint8)
    # Set NaN pixels to black
    nan_mask = np.isnan(ndvi_arr)
    rgb[nan_mask] = 0
    return rgb


def render_product(bands: np.ndarray, product: str) -> np.ndarray:
    """Dispatch to the appropriate rendering function.

    Args:
        bands: uint16 array of shape (4, H, W)
        product: One of "true_color", "false_color", "ndvi", "ndwi", "ndvi_rgb"

    Returns:
        Rendered output (uint8 RGB or float32 index).
    """
    dispatch = {
        "true_color": true_color,
        "false_color": false_color_nir,
        "ndvi": ndvi,
        "ndwi": ndwi,
        "ndvi_rgb": lambda b: ndvi_colormap(ndvi(b)),
    }
    if product not in dispatch:
        raise ValueError(f"Unknown product '{product}'. Available: {list(dispatch.keys())}")
    return dispatch[product](bands)


def save_png(arr: np.ndarray, path) -> None:
    """Save a rendered product as PNG.

    Args:
        arr: uint8 (H, W, 3) RGB or float32 (H, W) index
        path: Output file path
    """
    from pathlib import Path

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if arr.ndim == 3 and arr.dtype == np.uint8:
        plt.imsave(str(path), arr)
    elif arr.ndim == 2:
        fig, ax = plt.subplots(1, 1, figsize=(10, 10))
        im = ax.imshow(arr, cmap="RdYlGn", vmin=-1, vmax=1)
        plt.colorbar(im, ax=ax, shrink=0.8)
        ax.set_axis_off()
        fig.savefig(str(path), dpi=150, bbox_inches="tight")
        plt.close(fig)


def _stretch_to_uint8(rgb: np.ndarray, percentile_stretch: tuple[float, float]) -> np.ndarray:
    """Percentile contrast stretch of a (3, H, W) float array to (H, W, 3) uint8."""
    out = np.zeros((*rgb.shape[1:], 3), dtype=np.uint8)
    for i in range(3):
        band = rgb[i]
        valid = band[band > 0]
        if len(valid) == 0:
            continue
        lo = np.percentile(valid, percentile_stretch[0])
        hi = np.percentile(valid, percentile_stretch[1])
        if hi <= lo:
            hi = lo + 1
        stretched = np.clip((band - lo) / (hi - lo) * 255, 0, 255)
        out[:, :, i] = stretched.astype(np.uint8)
    return out
