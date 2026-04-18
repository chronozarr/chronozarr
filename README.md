# TileRipper

Low-cost API for temporally coherent Sentinel-2 and Landsat basemaps, vegetation indices, and water layers — with maps plus values.

## What it does

TileRipper serves month-specific satellite imagery and queryable environmental layers from native multiband chunk stores. Products (NDVI, NDWI, water classification) are derived on demand from raw reflectance — no pre-rendering, no duplication.

**Maps**: Monthly Sentinel-2 mosaics as tile images (true color, false color, NDVI, NDWI, water).

**Values**: Point query any location and get actual band values, vegetation indices, and water classification — not just pixels.

## Quick start

```bash
# Start the tile server
uv run --extra serve uvicorn spacetime.serve:app --host 0.0.0.0 --port 8765

# Open the demo
open http://localhost:8765
```

## API

All endpoints are under `/v1/`. No API key required for local development.

### List available data

```bash
curl http://localhost:8765/v1/catalog
```

### Get a tile

```bash
# True color tile for Sahara, January 2023, chunk r002_c003
curl http://localhost:8765/v1/tiles/sahara_tamanrasset/2023-01/r002_c003?product=true_color \
  -o tile.jpg
```

### Query values at a point

```bash
# Get NDVI, NDWI, water classification, and raw band values
curl "http://localhost:8765/v1/query/sahara_tamanrasset/2023-06?lat=23.3&lng=5.5"
```

```json
{
  "aoi": "sahara_tamanrasset",
  "month": "2023-06",
  "lat": 23.3,
  "lng": 5.5,
  "values": {
    "ndvi": 0.0312,
    "ndwi": -0.4231,
    "water": 0.0
  },
  "bands": {
    "B02": 1842,
    "B03": 2156,
    "B04": 2734,
    "B08": 2910
  },
  "source": "Sentinel-2 L2A via Planetary Computer",
  "composite_method": "monthly median, SCL cloud mask, carry-forward gap fill"
}
```

### AOI summary statistics

```bash
curl "http://localhost:8765/v1/stats/iowa_ames/2023-07?product=ndvi"
```

### Python

```python
import httpx

base = "http://localhost:8765/v1"
client = httpx.Client(headers={"X-API-Key": "tr_dev_local"})

# List AOIs
catalog = client.get(f"{base}/catalog").json()

# Point query
result = client.get(f"{base}/query/iowa_ames/2023-07", params={
    "lat": 42.03,
    "lng": -93.62,
}).json()
print(f"NDVI: {result['values']['ndvi']}")
```

### JavaScript / Leaflet

```javascript
// Query values at a click location
const res = await fetch(
  `/v1/query/iowa_ames/2023-07?lat=${lat}&lng=${lng}`
);
const data = await res.json();
console.log(`NDVI: ${data.values.ndvi}, Water: ${data.values.water}`);
```

## Products

| Product | Description | Tier |
|---------|-------------|------|
| `true_color` | Natural color RGB | Explorer |
| `false_color` | NIR false color (vegetation in red) | Explorer |
| `ndvi` | Normalized Difference Vegetation Index | Explorer |
| `ndwi` | Normalized Difference Water Index | Builder |
| `water` | Binary water classification | Builder |

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/v1/catalog` | List all AOIs and products |
| GET | `/v1/catalog/{aoi}` | AOI detail |
| GET | `/v1/products` | Product catalog with tier requirements |
| GET | `/v1/months/{aoi}` | Available months |
| GET | `/v1/tiles/{aoi}/{month}/{chunk_id}` | Rendered tile image |
| GET | `/v1/query/{aoi}/{month}?lat=...&lng=...` | Point query (maps + values) |
| GET | `/v1/stats/{aoi}/{month}?product=...` | AOI summary statistics |
| GET | `/v1/usage` | Current API key usage |

## Architecture

TileRipper stores raw 4-band Sentinel-2 reflectance (B02, B03, B04, B08) as native multiband Zarr chunks. Products are derived at serve time from the same underlying data.

- **Format**: B2_chunked Zarr — `(n_months, 4, H, W)` uint16, per-month random access
- **Chunks**: 512px default (5.12 km at 10m resolution)
- **CRS**: UTM per AOI (never Web Mercator for analysis data)
- **Compression**: Blosc/zstd, lossless
- **Band cache**: LRU cache means product switches are instant (bands stay cached)

See [ARCHITECTURE_DECISION.md](ARCHITECTURE_DECISION.md) for the full spec.

## Auth

Three ways to pass an API key:

```bash
# Header
curl -H "X-API-Key: YOUR_KEY" http://localhost:8765/v1/catalog

# Bearer token
curl -H "Authorization: Bearer YOUR_KEY" http://localhost:8765/v1/catalog

# Query param (for browser testing)
curl http://localhost:8765/v1/catalog?key=YOUR_KEY
```

No key required for local development — defaults to dev tier with full access.

## Development

```bash
# Install with dev extras
uv sync --extra dev --extra serve

# Run server with reload
uv run --extra serve uvicorn spacetime.serve:app --reload --port 8765

# Lint
uv run ruff check src/
uv run ruff format src/

# Run the full ingestion pipeline for one AOI
uv run python experiments/run_experiment.py --aoi sahara_tamanrasset
```
