"""Pydantic response models for the TileRipper v1 API."""

from __future__ import annotations

from pydantic import BaseModel, Field

# --- Catalog ---


class GridInfo(BaseModel):
    n_rows: int
    n_cols: int
    chunk_size: int
    mosaic_height: int
    mosaic_width: int


class AOISummary(BaseModel):
    id: str = Field(description="AOI identifier, e.g. 'sahara_tamanrasset'")
    name: str = Field(description="Human-readable label")
    source: str = Field(description="Data source, e.g. 'Sentinel-2 L2A'")
    epsg: int = Field(description="Native CRS EPSG code (UTM)")
    months: list[str] = Field(description="Available months as YYYY-MM strings")
    n_months: int
    grid: GridInfo
    products: list[str] = Field(description="Available product IDs for this AOI")
    bbox_wgs84: list[float] | None = Field(
        default=None,
        description="Bounding box in WGS84 [west, south, east, north]",
    )


class ProductInfo(BaseModel):
    id: str
    name: str
    description: str
    formula: str | None = None
    unit: str | None = None
    value_range: list[float] | None = Field(default=None, alias="range")
    tier: str = Field(description="Minimum tier required: explorer, builder, or pro")
    format: str = Field(
        description="Output type: 'rgb' for imagery, 'index' for values, 'class' for masks"
    )


class CatalogResponse(BaseModel):
    aois: list[AOISummary]
    products: list[ProductInfo]


class MonthsResponse(BaseModel):
    aoi: str
    months: list[str]
    n_months: int
    first: str
    last: str


# --- Point query ---


class PixelLocation(BaseModel):
    row: int
    col: int
    chunk_id: str
    utm_x: float
    utm_y: float


class PointQueryResponse(BaseModel):
    aoi: str
    month: str
    lat: float
    lng: float
    pixel: PixelLocation
    values: dict[str, float | None] = Field(
        description="Product values at this point, e.g. {'ndvi': 0.72, 'ndwi': -0.13}"
    )
    bands: dict[str, int] = Field(
        description="Raw band reflectance values (uint16, scale_factor=10000)"
    )
    source: str
    composite_method: str


# --- Stats ---


class StatsResponse(BaseModel):
    aoi: str
    month: str
    product: str
    stats: dict[str, float] = Field(
        description="Summary statistics: mean, median, std, min, max, p10, p90"
    )
    valid_pixels: int
    total_pixels: int


# --- Usage ---


class UsageResponse(BaseModel):
    tier: str
    period: str
    requests: int
    tile_requests: int
    query_requests: int
    quota: int | None = Field(description="Monthly request quota, null for unlimited")


# --- Errors ---


class ErrorResponse(BaseModel):
    error: str
    detail: str | None = None


# --- Jobs (re-exported from jobs module) ---
