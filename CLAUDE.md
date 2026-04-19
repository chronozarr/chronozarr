# TileRipper

Low-cost API for temporally coherent EO basemaps with queryable values.
Product name: TileRipper. Internal package name: spacetime.

## Architecture

ChronoFabric v1: star-delta temporal encoding with multiscale LOD pyramid.
Zstd-compressed Zarr stores. Products derived at serve time via band math.

## Project structure

```
src/spacetime/
  api/
    v1.py          # v1 API router (catalog, tiles, query, stats, usage)
    auth.py        # API key auth middleware + rate limiting
    models.py      # Pydantic response models
    products.py    # Product catalog + tier gating
    metering.py    # Usage tracking
    geo.py         # Coordinate transforms (WGS84 ↔ UTM ↔ pixel)
  serve.py         # FastAPI app entry point (mounts v1, auth, CORS)
  render.py        # Band math: true_color, false_color, NDVI, NDWI, water
  chunk.py         # 512px spatial chunking grid
  catalog.py       # STAC search (Planetary Computer, S2 L2A)
  mosaic.py        # Monthly median composite with SCL cloud masking
  static/
    index.html     # Product demo (map + click-to-query values panel)
  encode/
    v1.py          # ChronoFabric v1 encoder (star-delta + LOD pyramid)
experiments/
  aois.yaml        # AOI definitions
scripts/
  ingest_v1.py     # Generic AOI ingestion (download + v1 encode)
data/              # gitignored: mosaics/, stores/, reports/
```

## How to run

```bash
# Tile server + demo (http://localhost:8765)
uv run --extra serve uvicorn spacetime.serve:app --host 0.0.0.0 --port 8765

# Ingest an AOI (full Sentinel-2 archive by default)
uv run python scripts/ingest_v1.py --aoi nile_delta
uv run python scripts/ingest_v1.py --aoi sahara_tamanrasset --start 2020-01-01 --end 2024-12-31
```

## API endpoints (v1)

- `GET /v1/catalog` — list AOIs + products
- `GET /v1/tiles/{aoi}/{month}/{chunk_id}` — rendered tile image
- `GET /v1/query/{aoi}/{month}?lat=...&lng=...` — point query (maps + values)
- `GET /v1/stats/{aoi}/{month}?product=ndvi` — AOI summary statistics
- `GET /v1/products` — product catalog with tier requirements
- `GET /v1/months/{aoi}` — available months
- `GET /v1/usage` — current key usage

## Products and tiers

- Explorer ($3): true_color, false_color, ndvi
- Builder ($5): + ndwi, water
- Pro ($7): + weekly Sentinel-2 (future)
- Dev: all products, no rate limits

## Key design decisions

- B2_chunked Zarr: (n_months, 4, H, W), chunks (1, 4, H, W)
- Chunk-grid serving is the runtime (mosaic endpoint removed)
- manifest.json per store — self-describing
- UTM-per-AOI, never Web Mercator for analysis data
- 512 px default chunk size, 256 px for low-latency
- numcodecs <0.15 for zarr 2.x compatibility
- API key auth via X-API-Key header, Bearer token, or query param
- Dev mode: no key required, defaults to dev tier
