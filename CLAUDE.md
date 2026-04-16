# Spacetime Chunk Prototype

Research prototype comparing spacetime chunk architecture vs conventional precomputed basemaps for Sentinel-2 temporal remote sensing data.

## Project structure

```
src/spacetime/
  catalog.py       # STAC search (Planetary Computer, S2 L2A)
  mosaic.py        # Monthly median composite with SCL cloud masking
  chunk.py         # 512px spatial chunking grid
  render.py        # Band math: true_color, false_color, NDVI, NDWI
  encode/
    baseline_a.py  # Per-product per-month PNGs (conventional strawman)
    baseline_b.py  # Multiband Zarr (B1=independent, B2=stacked, B2_chunked)
    experimental.py # Keyframe + int16 delta + zstd
  decode/          # (decode functions live in encode modules)
  access.py        # Access pattern simulation (viewport, pan, scrub, product switch)
  bench.py         # DuckDB-backed metrics (storage, access, quality)
  qc.py            # Visual comparison panels, delta stats plots
experiments/
  aois.yaml        # AOI definitions (Sahara + Iowa)
  run_experiment.py # Full pipeline orchestrator (phases 1-5)
  quick_bench.py   # Quick bench on partial data
data/              # gitignored: raw/, mosaics/, stores/, reports/
```

## How to run

```bash
# Full pipeline for one AOI
uv run python experiments/run_experiment.py --aoi sahara_tamanrasset

# Skip download if mosaics already exist
uv run python experiments/run_experiment.py --aoi sahara_tamanrasset --skip-download --phases 2,3,4,5

# Quick benchmark on whatever mosaics exist
uv run python experiments/quick_bench.py --aoi sahara_tamanrasset
```

## Key design decisions

- Store in UTM-per-AOI, never Web Mercator for analysis data
- Median monthly composite with SCL cloud masking, carry-forward for gaps
- Lossless delta encoding (int16 residuals, zstd compressed)
- numcodecs pinned <0.15 for zarr 2.x compatibility
- All encoders track bytes written for benchmarking

## Representations compared

| Label | Description | When better |
|-------|-------------|-------------|
| A     | Per-product per-month PNGs | Simplest serving, worst storage |
| B1    | Independent multiband Zarr per chunk/month | Good balance, no product duplication |
| B2    | Time-stacked Zarr (bulk compressed) | Best raw compression, worst partial access |
| B2_chunked | Time-stacked with per-month Zarr chunks | Good compression + partial access |
| X_kf{N} | Keyframe every N months + int16 deltas | Best partial access for time scrub |
