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


def true_color(bands: np.ndarray) -> np.ndarray:
    """Render true-color RGB using the Sentinel Hub L2A optimized pipeline.

    Physics-based tone mapping on raw reflectance — no per-scene percentile
    statistics, so color is consistent across all tiles.

    Pipeline: reflectance → highlight compression → gamma → saturation → sRGB.

    Reference: https://custom-scripts.sentinel-hub.com/sentinel-2/l2a_optimized/

    Args:
        bands: uint16 array of shape (4, H, W) — [B02, B03, B04, B08]

    Returns:
        uint8 array of shape (H, W, 3) — RGB
    """
    r = bands[B04].astype(np.float32) / 10000
    g = bands[B03].astype(np.float32) / 10000
    b = bands[B02].astype(np.float32) / 10000

    # Highlight compression + contrast curve + gamma
    r_t = _s_adj(r)
    g_t = _s_adj(g)
    b_t = _s_adj(b)

    # Saturation boost
    r_s, g_s, b_s = _sat_enhance(r_t, g_t, b_t, _TC_SAT)

    # sRGB encoding → float [0, 1]
    rgb = np.stack([_linear_to_srgb(r_s), _linear_to_srgb(g_s), _linear_to_srgb(b_s)], axis=-1)

    # Global contrast expansion — use the SAME lo/hi for all 3 channels
    # to preserve the color ratios from the physics pipeline. This fills
    # [0, 255] without introducing per-band hue shifts.
    nodata = (bands[B04] == 0) & (bands[B03] == 0) & (bands[B02] == 0)
    valid = rgb[~nodata]
    if len(valid) > 0:
        lo = float(np.percentile(valid, 2))
        hi = float(np.percentile(valid, 98))
        if hi > lo:
            rgb = (rgb - lo) / (hi - lo)

    out = np.clip(rgb * 255, 0, 255).astype(np.uint8)
    out[nodata] = 0
    return out


# --- True color tone-mapping parameters ---
# Based on Sentinel Hub L2A optimized script, tuned for cloud-masked monthly
# median composites where reflectance stays in [0, ~0.5] (no clouds/snow).
_TC_MAX_R = 1.5  # max reflectance (handles snow ~1.3 without blowout, good contrast for land)
_TC_MID_R = 0.13  # midpoint reflectance
_TC_GAMMA = 1.5  # gamma (1.8 = SH default; lower = more contrast in midtones)
_TC_SAT = 1.5  # saturation boost (1.2 = SH default; composites need more to show color)
_TC_G_OFF = 0.01  # gamma offset to avoid crushing blacks
_TC_G_OFF_POW = _TC_G_OFF**_TC_GAMMA
_TC_G_OFF_RANGE = (1 + _TC_G_OFF) ** _TC_GAMMA - _TC_G_OFF_POW


def _highlight_compress(a: np.ndarray) -> np.ndarray:
    """Rational curve for contrast enhancement with highlight compression."""
    ar = np.clip(a / _TC_MAX_R, 0, 1)
    tx_norm = _TC_MID_R / _TC_MAX_R
    num = ar * (ar * (tx_norm) - 1)
    den = ar * (2 * tx_norm - 1) - tx_norm
    with np.errstate(invalid="ignore", divide="ignore"):
        result = np.where(np.abs(den) > 1e-10, num / den, 0)
    return np.clip(result, 0, 1)


def _adj_gamma(b: np.ndarray) -> np.ndarray:
    """Gamma adjustment with offset to preserve shadow detail."""
    return (np.power(b + _TC_G_OFF, _TC_GAMMA) - _TC_G_OFF_POW) / _TC_G_OFF_RANGE


def _s_adj(a: np.ndarray) -> np.ndarray:
    """Combined highlight compression + gamma for one channel."""
    return _adj_gamma(_highlight_compress(a))


def _sat_enhance(
    r: np.ndarray, g: np.ndarray, b: np.ndarray, sat: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Boost saturation by pulling channels away from their mean."""
    avg = (r + g + b) / 3.0 * (1 - sat)
    return (
        np.clip(avg + r * sat, 0, 1),
        np.clip(avg + g * sat, 0, 1),
        np.clip(avg + b * sat, 0, 1),
    )


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


def ndwi_colormap(ndwi_arr: np.ndarray) -> np.ndarray:
    """Map NDWI values to a dry-to-wet diverging colormap.

    Brown (dry, -1) → gray (0) → blue (wet, +1).

    Args:
        ndwi_arr: float32 (H, W), range [-1, 1]

    Returns:
        uint8 (H, W, 3) RGB
    """
    norm = np.clip((ndwi_arr + 1.0) / 2.0, 0, 1)
    # Three stops: brown(0) → gray(0.5) → blue(1)
    # Brown: (181, 101, 29), Gray: (192, 192, 192), Blue: (26, 82, 118)
    r = np.where(
        norm < 0.5,
        181 + (192 - 181) * norm / 0.5,
        192 + (26 - 192) * (norm - 0.5) / 0.5,
    )
    g = np.where(
        norm < 0.5,
        101 + (192 - 101) * norm / 0.5,
        192 + (82 - 192) * (norm - 0.5) / 0.5,
    )
    b = np.where(
        norm < 0.5,
        29 + (192 - 29) * norm / 0.5,
        192 + (118 - 192) * (norm - 0.5) / 0.5,
    )
    rgb = np.stack([r, g, b], axis=-1)
    rgb = np.clip(rgb, 0, 255).astype(np.uint8)
    rgb[np.isnan(ndwi_arr)] = 0
    return rgb


def water_mask(bands: np.ndarray) -> np.ndarray:
    """Classify water pixels using NDWI > 0 threshold.

    Args:
        bands: uint16 array of shape (4, H, W)

    Returns:
        bool array of shape (H, W). True = water.
    """
    green = bands[B03].astype(np.float32)
    nir = bands[B08].astype(np.float32)
    denom = green + nir
    with np.errstate(invalid="ignore", divide="ignore"):
        ndwi_val = np.where(denom > 0, (green - nir) / denom, np.nan)
    return ndwi_val > 0


def water_rgb(bands: np.ndarray) -> np.ndarray:
    """Render water classification: blue for water, dark gray elsewhere.

    Args:
        bands: uint16 array of shape (4, H, W)

    Returns:
        uint8 (H, W, 3) RGB
    """
    mask = water_mask(bands)
    h, w = mask.shape
    rgb = np.full((h, w, 3), [26, 26, 46], dtype=np.uint8)
    # Water pixels: blue (#2980b9)
    rgb[mask] = [41, 128, 185]
    # Nodata pixels: black
    green = bands[B03].astype(np.float32)
    nir = bands[B08].astype(np.float32)
    nodata = (green + nir) == 0
    rgb[nodata] = 0
    return rgb


def ndvi_colormap(ndvi_arr: np.ndarray) -> np.ndarray:
    """Map NDVI to an adaptive colormap using Otsu thresholding.

    Uses Otsu's method to find the vegetation/non-vegetation boundary,
    then maps three segments:
        < 0:            dark blue-gray (water / shadow)
        0 → threshold:  brown → tan (bare soil / urban)
        threshold → 1:  light green → dark green (vegetation)

    Args:
        ndvi_arr: float32 (H, W), range [-1, 1]

    Returns:
        uint8 (H, W, 3) RGB
    """
    from skimage.filters import threshold_otsu

    h, w = ndvi_arr.shape
    rgb = np.zeros((h, w, 3), dtype=np.float32)
    nan_mask = np.isnan(ndvi_arr)

    # Compute Otsu threshold on valid positive NDVI
    valid = ndvi_arr[~nan_mask]
    positive = valid[valid > 0]
    if len(positive) > 100:
        try:
            thresh = float(threshold_otsu(positive))
        except ValueError:
            thresh = 0.2
    else:
        thresh = 0.2
    thresh = np.clip(thresh, 0.05, 0.6)

    # Color stops (RGB float [0, 1])
    c_water = np.array([26, 35, 126]) / 255  # dark indigo
    c_shadow = np.array([69, 90, 100]) / 255  # blue-gray
    c_brown = np.array([93, 64, 55]) / 255  # dark brown
    c_tan = np.array([215, 204, 200]) / 255  # warm tan
    c_lgreen = np.array([174, 213, 129]) / 255  # light green
    c_dgreen = np.array([27, 94, 32]) / 255  # deep forest green

    # Segment 1: water/shadow — NDVI < 0
    mask = (~nan_mask) & (ndvi_arr < 0)
    if np.any(mask):
        t = np.clip((ndvi_arr[mask] + 1.0), 0, 1)  # [-1, 0] → [0, 1]
        for c in range(3):
            rgb[mask, c] = c_water[c] + (c_shadow[c] - c_water[c]) * t

    # Segment 2: bare soil — 0 ≤ NDVI < threshold
    mask = (~nan_mask) & (ndvi_arr >= 0) & (ndvi_arr < thresh)
    if np.any(mask):
        t = np.clip(ndvi_arr[mask] / thresh, 0, 1)
        for c in range(3):
            rgb[mask, c] = c_brown[c] + (c_tan[c] - c_brown[c]) * t

    # Segment 3: vegetation — NDVI ≥ threshold
    mask = (~nan_mask) & (ndvi_arr >= thresh)
    if np.any(mask):
        t = np.clip((ndvi_arr[mask] - thresh) / (1.0 - thresh), 0, 1)
        for c in range(3):
            rgb[mask, c] = c_lgreen[c] + (c_dgreen[c] - c_lgreen[c]) * t

    out = np.clip(rgb * 255, 0, 255).astype(np.uint8)
    out[nan_mask] = 0
    return out


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
        "ndwi_rgb": lambda b: ndwi_colormap(ndwi(b)),
        "water": water_rgb,
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


def _stretch_global_to_uint8(
    rgb: np.ndarray, percentile_stretch: tuple[float, float]
) -> np.ndarray:
    """Global percentile stretch + sRGB gamma for natural color.

    Computes a SINGLE lo/hi from all 3 bands combined, then applies
    the same stretch to each band. This preserves color ratios — no
    per-band amplification that shifts hue toward pink/purple.
    """
    # Pool all valid pixels across all 3 bands for a single stretch range
    all_valid = rgb[:, :, :][rgb > 0]
    if len(all_valid) == 0:
        return np.zeros((*rgb.shape[1:], 3), dtype=np.uint8)

    lo = np.percentile(all_valid, percentile_stretch[0])
    hi = np.percentile(all_valid, percentile_stretch[1])
    if hi <= lo:
        hi = lo + 1

    out = np.zeros((*rgb.shape[1:], 3), dtype=np.uint8)
    for i in range(3):
        linear = np.clip((rgb[i] - lo) / (hi - lo), 0, 1)
        srgb = _linear_to_srgb(linear)
        out[:, :, i] = np.clip(srgb * 255, 0, 255).astype(np.uint8)
    return out


def _stretch_to_uint8(rgb: np.ndarray, percentile_stretch: tuple[float, float]) -> np.ndarray:
    """Per-band percentile stretch + sRGB gamma.

    Each band gets its own lo/hi. Good for false-color composites where
    bands have very different dynamic ranges (e.g. NIR vs visible).
    """
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
        linear = np.clip((band - lo) / (hi - lo), 0, 1)
        srgb = _linear_to_srgb(linear)
        out[:, :, i] = np.clip(srgb * 255, 0, 255).astype(np.uint8)
    return out


def _linear_to_srgb(linear: np.ndarray) -> np.ndarray:
    """Apply sRGB transfer function (IEC 61966-2-1) to linear [0, 1] values."""
    return np.where(
        linear <= 0.0031308,
        12.92 * linear,
        1.055 * np.power(linear, 1.0 / 2.4) - 0.055,
    )
