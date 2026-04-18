"""Unit tests for spacetime.render module.

All functions are pure numpy — synthetic uint16 (4, H, W) band arrays
are used to verify correct behavior.
"""

import numpy as np
import pytest

from spacetime.render import (
    B02,
    B03,
    B04,
    B08,
    false_color_nir,
    ndvi,
    ndvi_colormap,
    ndwi,
    ndwi_colormap,
    render_product,
    true_color,
    water_mask,
    water_rgb,
)

# -----------------------------------------------------------------------------
# Test fixtures
# -----------------------------------------------------------------------------


def make_synthetic_bands(shape=(4, 8, 8), seed=42):
    """Create synthetic uint16 band data for testing."""
    rng = np.random.default_rng(seed)
    bands = rng.integers(0, 10000, size=shape, dtype=np.uint16)
    return bands


def make_zero_bands(shape=(4, 8, 8)):
    """Create all-zero uint16 band data for testing."""
    return np.zeros(shape, dtype=np.uint16)


def make_constant_bands(values, shape=(8, 8)):
    """Create bands with constant values.

    Args:
        values: dict mapping band index to constant value
        shape: (H, W) spatial shape
    """
    bands = np.zeros((4, *shape), dtype=np.uint16)
    for band_idx, val in values.items():
        bands[band_idx] = val
    return bands


# -----------------------------------------------------------------------------
# true_color tests
# -----------------------------------------------------------------------------


@pytest.mark.unit
def test_true_color_output_shape_dtype():
    """true_color: input uint16 (4,H,W) → output uint8 (H,W,3)."""
    bands = make_synthetic_bands(shape=(4, 8, 8))
    result = true_color(bands)

    assert result.shape == (8, 8, 3), f"Expected shape (8, 8, 3), got {result.shape}"
    assert result.dtype == np.uint8, f"Expected dtype uint8, got {result.dtype}"


@pytest.mark.unit
def test_true_color_all_zero_input():
    """true_color: all-zero input → all-zero output."""
    bands = make_zero_bands(shape=(4, 8, 8))
    result = true_color(bands)

    assert result.shape == (8, 8, 3)
    assert np.all(result == 0), "All-zero input should produce all-zero output"


@pytest.mark.unit
def test_true_color_output_range():
    """true_color: output values in valid uint8 range [0, 255]."""
    bands = make_synthetic_bands(shape=(4, 8, 8))
    result = true_color(bands)

    assert np.all(result >= 0), "Output values should be >= 0"
    assert np.all(result <= 255), "Output values should be <= 255"


# -----------------------------------------------------------------------------
# false_color_nir tests
# -----------------------------------------------------------------------------


@pytest.mark.unit
def test_false_color_nir_shape_dtype():
    """false_color_nir: shape/dtype correctness."""
    bands = make_synthetic_bands(shape=(4, 8, 8))
    result = false_color_nir(bands)

    assert result.shape == (8, 8, 3), f"Expected shape (8, 8, 3), got {result.shape}"
    assert result.dtype == np.uint8, f"Expected dtype uint8, got {result.dtype}"


@pytest.mark.unit
def test_false_color_nir_band_ordering():
    """false_color_nir: band ordering is (NIR, Red, Green)."""
    # Create bands where only ONE band has varying values at a time
    # This tests that the correct input band maps to the correct output position
    rng = np.random.default_rng(42)

    # Test 1: Only NIR has varying values → output[0] (R) should be bright
    bands_nir = np.zeros((4, 8, 8), dtype=np.uint16)
    bands_nir[B08] = rng.integers(1000, 9000, size=(8, 8), dtype=np.uint16)
    result_nir = false_color_nir(bands_nir)
    # Channel 0 (R) should have non-zero values from NIR
    assert np.any(result_nir[:, :, 0] > 0), "NIR input should produce non-zero R channel"
    assert np.all(result_nir[:, :, 1] == 0), "Green channel should be zero when only NIR has data"
    assert np.all(result_nir[:, :, 2] == 0), "Blue channel should be zero when only NIR has data"

    # Test 2: Only Red has varying values → output[1] (G) should be bright
    bands_red = np.zeros((4, 8, 8), dtype=np.uint16)
    bands_red[B04] = rng.integers(1000, 9000, size=(8, 8), dtype=np.uint16)
    result_red = false_color_nir(bands_red)
    # Channel 1 (G) should have non-zero values from Red
    assert np.any(result_red[:, :, 1] > 0), "Red input should produce non-zero G channel"
    assert np.all(result_red[:, :, 0] == 0), "Red channel should be zero when only Red has data"
    assert np.all(result_red[:, :, 2] == 0), "Blue channel should be zero when only Red has data"

    # Test 3: Only Green has varying values → output[2] (B) should be bright
    bands_green = np.zeros((4, 8, 8), dtype=np.uint16)
    bands_green[B03] = rng.integers(1000, 9000, size=(8, 8), dtype=np.uint16)
    result_green = false_color_nir(bands_green)
    # Channel 2 (B) should have non-zero values from Green
    assert np.any(result_green[:, :, 2] > 0), "Green input should produce non-zero B channel"
    assert np.all(result_green[:, :, 0] == 0), (
        "Red channel should be zero when only Green has data"
    )
    assert np.all(result_green[:, :, 1] == 0), (
        "Green channel should be zero when only Green has data"
    )

    # Test 4: Verify B02 (Blue) is NOT used - only Blue input should not produce output
    bands_blue = np.zeros((4, 8, 8), dtype=np.uint16)
    bands_blue[B02] = rng.integers(1000, 9000, size=(8, 8), dtype=np.uint16)
    result_blue = false_color_nir(bands_blue)
    # All channels should be zero (B02 is not used in false_color_nir)
    assert np.all(result_blue == 0), "Blue input should not affect output"


# -----------------------------------------------------------------------------
# ndvi tests
# -----------------------------------------------------------------------------


@pytest.mark.unit
def test_ndvi_known_values():
    """ndvi: known values (NIR=8000, Red=2000 → NDVI=0.6)."""
    bands = make_constant_bands({B08: 8000, B04: 2000}, shape=(8, 8))
    result = ndvi(bands)

    expected = (8000 - 2000) / (8000 + 2000)  # = 6000 / 10000 = 0.6
    msg = f"Expected NDVI={expected}, got {result[0, 0]}"
    assert np.allclose(result, expected, rtol=1e-5), msg
    assert result.shape == (8, 8)
    assert result.dtype == np.float32


@pytest.mark.unit
def test_ndvi_zero_denom():
    """ndvi: zero denominator → NaN."""
    bands = make_constant_bands({B08: 0, B04: 0}, shape=(8, 8))
    result = ndvi(bands)

    assert np.all(np.isnan(result)), "Zero denominator should produce NaN"


@pytest.mark.unit
def test_ndvi_range():
    """ndvi: output in range [-1, 1]."""
    bands = make_synthetic_bands(shape=(4, 8, 8))
    result = ndvi(bands)

    # Filter out NaN values
    valid = result[~np.isnan(result)]
    if len(valid) > 0:
        assert np.all(valid >= -1.0), "NDVI should be >= -1"
        assert np.all(valid <= 1.0), "NDVI should be <= 1"


@pytest.mark.unit
def test_ndvi_extreme_values():
    """ndvi: extreme values (pure NIR → 1, pure Red → -1)."""
    # Pure NIR
    bands_nir = make_constant_bands({B08: 10000, B04: 0}, shape=(4, 4))
    result_nir = ndvi(bands_nir)
    expected_nir = 1.0  # (10000 - 0) / (10000 + 0) = 1.0
    # Note: when Red=0, denom=10000, so NDVI=1.0 (not NaN)
    assert np.allclose(result_nir, expected_nir, rtol=1e-5)

    # Pure Red
    bands_red = make_constant_bands({B08: 0, B04: 10000}, shape=(4, 4))
    result_red = ndvi(bands_red)
    expected_red = -1.0  # (0 - 10000) / (0 + 10000) = -1.0
    assert np.allclose(result_red, expected_red, rtol=1e-5)


# -----------------------------------------------------------------------------
# ndwi tests
# -----------------------------------------------------------------------------


@pytest.mark.unit
def test_ndwi_known_values():
    """ndwi: known values (Green=8000, NIR=2000 → NDWI=0.6)."""
    bands = make_constant_bands({B03: 8000, B08: 2000}, shape=(8, 8))
    result = ndwi(bands)

    expected = (8000 - 2000) / (8000 + 2000)  # = 6000 / 10000 = 0.6
    msg = f"Expected NDWI={expected}, got {result[0, 0]}"
    assert np.allclose(result, expected, rtol=1e-5), msg
    assert result.shape == (8, 8)
    assert result.dtype == np.float32


@pytest.mark.unit
def test_ndwi_formula():
    """ndwi: formula is (Green - NIR) / (Green + NIR)."""
    # Test case: Green > NIR → positive NDWI
    bands_positive = make_constant_bands({B03: 8000, B08: 2000}, shape=(4, 4))
    result_positive = ndwi(bands_positive)
    assert np.all(result_positive > 0), "Green > NIR should give positive NDWI"

    # Test case: Green < NIR → negative NDWI
    bands_negative = make_constant_bands({B03: 2000, B08: 8000}, shape=(4, 4))
    result_negative = ndwi(bands_negative)
    assert np.all(result_negative < 0), "Green < NIR should give negative NDWI"


@pytest.mark.unit
def test_ndwi_zero_denom():
    """ndwi: zero denominator → NaN."""
    bands = make_constant_bands({B03: 0, B08: 0}, shape=(8, 8))
    result = ndwi(bands)

    assert np.all(np.isnan(result)), "Zero denominator should produce NaN"


@pytest.mark.unit
def test_ndwi_range():
    """ndwi: output in range [-1, 1]."""
    bands = make_synthetic_bands(shape=(4, 8, 8))
    result = ndwi(bands)

    # Filter out NaN values
    valid = result[~np.isnan(result)]
    if len(valid) > 0:
        assert np.all(valid >= -1.0), "NDWI should be >= -1"
        assert np.all(valid <= 1.0), "NDWI should be <= 1"


# -----------------------------------------------------------------------------
# water_mask tests
# -----------------------------------------------------------------------------


@pytest.mark.unit
def test_water_mask_positive_ndwi():
    """water_mask: NDWI > 0 → True (water)."""
    # Green > NIR → positive NDWI → water
    bands = make_constant_bands({B03: 8000, B08: 2000}, shape=(8, 8))
    result = water_mask(bands)

    assert result.shape == (8, 8)
    assert result.dtype == bool
    assert np.all(result), "NDWI > 0 should classify as water (True)"


@pytest.mark.unit
def test_water_mask_negative_ndwi():
    """water_mask: NDWI < 0 → False (non-water)."""
    # Green < NIR → negative NDWI → non-water
    bands = make_constant_bands({B03: 2000, B08: 8000}, shape=(8, 8))
    result = water_mask(bands)

    assert result.shape == (8, 8)
    assert result.dtype == bool
    assert np.all(~result), "NDWI < 0 should classify as non-water (False)"


@pytest.mark.unit
def test_water_mask_mixed():
    """water_mask: correctly handles mixed water/non-water scene."""
    bands = np.zeros((4, 8, 8), dtype=np.uint16)
    # Left half: water (Green > NIR)
    bands[B03, :, :4] = 8000  # Green
    bands[B08, :, :4] = 2000  # NIR
    # Right half: non-water (Green < NIR)
    bands[B03, :, 4:] = 2000
    bands[B08, :, 4:] = 8000

    result = water_mask(bands)

    assert np.all(result[:, :4]), "Left half should be water"
    assert np.all(~result[:, 4:]), "Right half should be non-water"


# -----------------------------------------------------------------------------
# water_rgb tests
# -----------------------------------------------------------------------------


@pytest.mark.unit
def test_water_rgb_water_pixels():
    """water_rgb: water pixels are blue [41, 128, 185]."""
    bands = make_constant_bands({B03: 8000, B08: 2000}, shape=(8, 8))  # Water
    result = water_rgb(bands)

    assert result.shape == (8, 8, 3)
    assert result.dtype == np.uint8

    expected_blue = np.array([41, 128, 185])
    for i in range(8):
        for j in range(8):
            assert np.array_equal(result[i, j], expected_blue), (
                f"Water pixel at ({i},{j}) should be blue {expected_blue}, got {result[i, j]}"
            )


@pytest.mark.unit
def test_water_rgb_non_water_pixels():
    """water_rgb: non-water pixels are dark [26, 26, 46]."""
    bands = make_constant_bands({B03: 2000, B08: 8000}, shape=(8, 8))  # Non-water
    result = water_rgb(bands)

    expected_dark = np.array([26, 26, 46])
    for i in range(8):
        for j in range(8):
            assert np.array_equal(result[i, j], expected_dark), (
                f"Non-water pixel at ({i},{j}) should be dark {expected_dark}, got {result[i, j]}"
            )


@pytest.mark.unit
def test_water_rgb_nodata_pixels():
    """water_rgb: nodata pixels (Green+NIR=0) are black [0, 0, 0]."""
    bands = make_constant_bands({B03: 0, B08: 0}, shape=(8, 8))
    result = water_rgb(bands)

    expected_black = np.array([0, 0, 0])
    for i in range(8):
        for j in range(8):
            assert np.array_equal(result[i, j], expected_black), (
                f"Nodata pixel at ({i},{j}) should be black {expected_black}, got {result[i, j]}"
            )


@pytest.mark.unit
def test_water_rgb_mixed_scene():
    """water_rgb: correctly renders mixed water/non-water scene."""
    bands = np.zeros((4, 8, 8), dtype=np.uint16)
    # Top-left: water
    bands[B03, :4, :4] = 8000
    bands[B08, :4, :4] = 2000
    # Top-right: non-water
    bands[B03, :4, 4:] = 2000
    bands[B08, :4, 4:] = 8000
    # Bottom: nodata
    bands[B03, 4:, :] = 0
    bands[B08, 4:, :] = 0

    result = water_rgb(bands)

    expected_blue = np.array([41, 128, 185])
    expected_dark = np.array([26, 26, 46])
    expected_black = np.array([0, 0, 0])

    # Check water region
    assert np.all(result[:4, :4] == expected_blue), "Top-left should be blue (water)"
    # Check non-water region
    assert np.all(result[:4, 4:] == expected_dark), "Top-right should be dark (non-water)"
    # Check nodata region
    assert np.all(result[4:, :] == expected_black), "Bottom should be black (nodata)"


# -----------------------------------------------------------------------------
# ndvi_colormap tests
# -----------------------------------------------------------------------------


@pytest.mark.unit
def test_ndvi_colormap_shape_dtype():
    """ndvi_colormap: output uint8 (H,W,3)."""
    ndvi_arr = np.random.uniform(-1, 1, size=(8, 8)).astype(np.float32)
    result = ndvi_colormap(ndvi_arr)

    assert result.shape == (8, 8, 3), f"Expected shape (8, 8, 3), got {result.shape}"
    assert result.dtype == np.uint8, f"Expected dtype uint8, got {result.dtype}"


@pytest.mark.unit
def test_ndvi_colormap_nan_to_black():
    """ndvi_colormap: NaN → black [0, 0, 0]."""
    ndvi_arr = np.full((8, 8), np.nan, dtype=np.float32)
    result = ndvi_colormap(ndvi_arr)

    expected_black = np.array([0, 0, 0])
    for i in range(8):
        for j in range(8):
            assert np.array_equal(result[i, j], expected_black), (
                f"NaN pixel at ({i},{j}) should be black {expected_black}, got {result[i, j]}"
            )


@pytest.mark.unit
def test_ndvi_colormap_output_range():
    """ndvi_colormap: output values in valid uint8 range [0, 255]."""
    ndvi_arr = np.random.uniform(-1, 1, size=(8, 8)).astype(np.float32)
    result = ndvi_colormap(ndvi_arr)

    assert np.all(result >= 0), "Output values should be >= 0"
    assert np.all(result <= 255), "Output values should be <= 255"


@pytest.mark.unit
def test_ndvi_colormap_extreme_values():
    """ndvi_colormap: extreme NDVI values produce valid colors."""
    # Test with -1 (water/shadow)
    ndvi_neg = np.full((4, 4), -1.0, dtype=np.float32)
    result_neg = ndvi_colormap(ndvi_neg)
    assert result_neg.shape == (4, 4, 3)
    assert np.all(result_neg[:, :, 0] > 0), "Negative NDVI should have non-zero red"

    # Test with +1 (dense vegetation)
    ndvi_pos = np.full((4, 4), 1.0, dtype=np.float32)
    result_pos = ndvi_colormap(ndvi_pos)
    assert result_pos.shape == (4, 4, 3)


# -----------------------------------------------------------------------------
# ndwi_colormap tests
# -----------------------------------------------------------------------------


@pytest.mark.unit
def test_ndwi_colormap_shape_dtype():
    """ndwi_colormap: output uint8 (H,W,3)."""
    ndwi_arr = np.random.uniform(-1, 1, size=(8, 8)).astype(np.float32)
    result = ndwi_colormap(ndwi_arr)

    assert result.shape == (8, 8, 3), f"Expected shape (8, 8, 3), got {result.shape}"
    assert result.dtype == np.uint8, f"Expected dtype uint8, got {result.dtype}"


@pytest.mark.unit
def test_ndwi_colormap_nan_to_black():
    """ndwi_colormap: NaN → black [0, 0, 0]."""
    ndwi_arr = np.full((8, 8), np.nan, dtype=np.float32)
    result = ndwi_colormap(ndwi_arr)

    expected_black = np.array([0, 0, 0])
    for i in range(8):
        for j in range(8):
            assert np.array_equal(result[i, j], expected_black), (
                f"NaN pixel at ({i},{j}) should be black {expected_black}, got {result[i, j]}"
            )


@pytest.mark.unit
def test_ndwi_colormap_output_range():
    """ndwi_colormap: output values in valid uint8 range [0, 255]."""
    ndwi_arr = np.random.uniform(-1, 1, size=(8, 8)).astype(np.float32)
    result = ndwi_colormap(ndwi_arr)

    assert np.all(result >= 0), "Output values should be >= 0"
    assert np.all(result <= 255), "Output values should be <= 255"


@pytest.mark.unit
def test_ndwi_colormap_color_gradient():
    """ndwi_colormap: dry (-1) is brownish, wet (+1) is blue."""
    # Dry (NDWI = -1) → should be brown
    ndwi_dry = np.full((4, 4), -1.0, dtype=np.float32)
    result_dry = ndwi_colormap(ndwi_dry)
    # Brown has high red, medium green, low blue
    msg = "Dry pixels should be more red than blue"
    assert np.all(result_dry[:, :, 0] > result_dry[:, :, 2]), msg

    # Wet (NDWI = +1) → should be blue
    ndwi_wet = np.full((4, 4), 1.0, dtype=np.float32)
    result_wet = ndwi_colormap(ndwi_wet)
    # Blue has low red, medium green, high blue
    msg = "Wet pixels should be more blue than red"
    assert np.all(result_wet[:, :, 2] > result_wet[:, :, 0]), msg


# -----------------------------------------------------------------------------
# render_product dispatch tests
# -----------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "product",
    [
        "true_color",
        "false_color",
        "ndvi",
        "ndwi",
        "ndvi_rgb",
        "ndwi_rgb",
        "water",
    ],
)
def test_render_product_valid_names(product):
    """render_product: valid product names work and produce correct output."""
    bands = make_synthetic_bands(shape=(4, 8, 8))
    result = render_product(bands, product)

    # Check that result is a numpy array
    assert isinstance(result, np.ndarray)

    # Check shape based on product type
    if product in ("ndvi", "ndwi"):
        assert result.shape == (8, 8), f"Product {product} should produce 2D output"
        assert result.dtype == np.float32
    else:
        assert result.shape == (8, 8, 3), f"Product {product} should produce RGB output"
        assert result.dtype == np.uint8


@pytest.mark.unit
def test_render_product_invalid_name_raises():
    """render_product: invalid product name raises ValueError."""
    bands = make_synthetic_bands(shape=(4, 8, 8))

    with pytest.raises(ValueError) as exc_info:
        render_product(bands, "invalid_product")

    assert "Unknown product" in str(exc_info.value)
    assert "invalid_product" in str(exc_info.value)


@pytest.mark.unit
def test_render_product_empty_string_raises():
    """render_product: empty string product name raises ValueError."""
    bands = make_synthetic_bands(shape=(4, 8, 8))

    with pytest.raises(ValueError):
        render_product(bands, "")


@pytest.mark.unit
def test_render_product_case_sensitive():
    """render_product: product names are case-sensitive."""
    bands = make_synthetic_bands(shape=(4, 8, 8))

    # Uppercase should fail
    with pytest.raises(ValueError):
        render_product(bands, "TRUE_COLOR")

    # Mixed case should fail
    with pytest.raises(ValueError):
        render_product(bands, "True_Color")
