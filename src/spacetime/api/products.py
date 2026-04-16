"""Product catalog and tier-based access gating.

Defines the products TileRipper can derive from raw multiband reflectance.
Each product has a minimum tier required for access.
"""

from __future__ import annotations

PRODUCTS: dict[str, dict] = {
    "true_color": {
        "id": "true_color",
        "name": "True Color",
        "description": "Natural color satellite imagery (RGB)",
        "formula": "RGB(B04, B03, B02)",
        "unit": None,
        "range": None,
        "tier": "explorer",
        "format": "rgb",
    },
    "false_color": {
        "id": "false_color",
        "name": "False Color (NIR)",
        "description": "NIR false color composite highlighting vegetation in red",
        "formula": "RGB(B08, B04, B03)",
        "unit": None,
        "range": None,
        "tier": "explorer",
        "format": "rgb",
    },
    "ndvi": {
        "id": "ndvi",
        "name": "NDVI",
        "description": "Normalized Difference Vegetation Index",
        "formula": "(NIR - Red) / (NIR + Red)",
        "unit": "index",
        "range": [-1.0, 1.0],
        "tier": "explorer",
        "format": "index",
    },
    "ndwi": {
        "id": "ndwi",
        "name": "NDWI",
        "description": "Normalized Difference Water Index",
        "formula": "(Green - NIR) / (Green + NIR)",
        "unit": "index",
        "range": [-1.0, 1.0],
        "tier": "builder",
        "format": "index",
    },
    "water": {
        "id": "water",
        "name": "Water Classification",
        "description": "Binary water mask from NDWI thresholding",
        "formula": "NDWI > 0",
        "unit": "class",
        "range": [0.0, 1.0],
        "tier": "builder",
        "format": "class",
    },
}

# Ordered from lowest to highest access
_TIER_RANK = {"explorer": 0, "builder": 1, "pro": 2, "dev": 99}


def tier_can_access(tier: str, product_id: str) -> bool:
    """Check if a tier has access to a product."""
    if product_id not in PRODUCTS:
        return False
    required = PRODUCTS[product_id]["tier"]
    return _TIER_RANK.get(tier, -1) >= _TIER_RANK.get(required, 0)


def products_for_tier(tier: str) -> list[str]:
    """Return product IDs accessible at a given tier."""
    return [pid for pid in PRODUCTS if tier_can_access(tier, pid)]


def all_product_ids() -> list[str]:
    """All product IDs in catalog order."""
    return list(PRODUCTS.keys())
