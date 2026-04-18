# Experimental Results — Spacetime Chunk Prototype

**Date:** 2026-04-15
**AOIs:** Sahara (Tamanrasset, Algeria), Iowa (Ames, IA)
**Period:** 24 months (2023-01 through 2024-12)
**Bands:** B02, B03, B04, B08 (10m Sentinel-2 L2A)
**Chunk size:** 512 px (5.12 km)

## Storage by representation

| Repr | Sahara (MB) | Iowa (MB) | Description |
|------|------------|----------|-------------|
| A | 1067 | 802 | Per-product per-month PNGs (3 products) |
| B1 | 869 | 702 | Independent multiband Zarr per chunk/month |
| B2 | 869 | 701 | Time-stacked Zarr (one bulk chunk) |
| B2_chunked | 869 | 701 | Time-stacked Zarr (per-month chunks) |
| X_kf1 | 869 | 702 | Keyframe every month (= B1 with overhead) |
| X_kf3 | 780 | 694 | Keyframe every 3 months |
| X_kf6 | 756 | 691 | Keyframe every 6 months |
| X_kf12 | 749 | 691 | Keyframe every 12 months |

## Hypothesis outcomes

### H1: Product unification saves storage — CONFIRMED

Storing raw multiband data once and deriving products at render time eliminates
per-product duplication.

| AOI | A (MB) | B1 (MB) | Savings |
|-----|--------|---------|---------|
| Sahara | 1067 | 869 | **18.5%** |
| Iowa | 802 | 702 | **12.5%** |

Savings scale with the number of pre-rendered products. With 3 products the
floor is ~33% if compression were equal; the gap narrows because PNG (A) and
Zarr/zstd (B) have different compression characteristics. With more products
(NDWI, custom indices, band ratios) the savings compound further.

The three B variants (B1, B2, B2_chunked) are essentially identical in total
storage — within 0.3% of each other. B2_chunked is the best tradeoff: same
compression as B2 with per-month random access like B1.

### H2: Temporal delta encoding — WEAK / LANDSCAPE-DEPENDENT

| AOI | Best delta (kf interval) | vs B2_chunked |
|-----|--------------------------|---------------|
| Sahara | X_kf12: 749 MB | **13.9%** savings |
| Iowa | X_kf12: 691 MB | **1.4%** savings |

Sahara is near-best-case for delta encoding: hyper-arid desert with atmospheric
noise as the dominant temporal signal (60-200 mean |delta| per month). Even so,
the win is modest — zstd already exploits the low-entropy content of desert
reflectance.

Iowa is a stress case: corn/soy phenology + seasonal snow produce large
temporal swings (80-4700 mean |delta|). Delta encoding provides effectively
zero benefit here.

**Conclusion:** Raw monthly reflectance deltas do not have enough temporal
redundancy to justify the complexity of keyframe + delta coding as a core
architectural primitive. The benefit is landscape-dependent, modest at best,
and disappears under seasonal variability.

### H3: Incremental access cost — MODERATE

Delta steps are 17-21% cheaper in bytes than a full B1 tile fetch. But
B2_chunked per-month access is roughly equivalent — you get the same
single-month granularity without reconstruction overhead.

The incremental access argument for delta encoding only holds when:
- You are already holding the previous frame in memory (time scrub)
- The landscape is temporally stable (low delta magnitude)

For cold viewport access or random time jumps, B2_chunked wins on simplicity.

### V2: Brightness/norm decomposition — REJECTED

Decomposing reflectance into brightness (L2 norm) + spectral shape was tested
as a way to isolate illumination changes from spectral changes. Result:
decomposed representation was **35% larger** than raw reflectance when
compressed. Spectral shape changes between months are real land-surface signals
(phenology, moisture, snow), not just illumination noise. This path is a dead
end for monthly composites.

## Chunk size (Iowa sweep)

| Chunk size | Chunks/AOI | Notes |
|-----------|-----------|-------|
| 256 px | ~144 | Lowest per-tile latency, highest object count |
| 512 px | ~36 | Good balance, acceptable latency |
| 1024 px | ~9 | Too coarse for interactive viewport updates |

Storage is nearly identical across chunk sizes (zstd compression is
chunk-size-insensitive at this scale). The differences are operational:
latency per tile fetch and number of objects to manage.

**Recommendation:** Default 512 px. Use 256 px as a latency-first option.
Drop 1024 px for interactive paths.

## Architectural conclusion

The architectural moat is **not** "video codec for monthly composites."

It **is** "native multiband substrate with late-bound products."

The core value proposition:
1. Store raw multiband reflectance once (B2_chunked format)
2. Derive any product (true color, false color, NDVI, NDWI, custom) at render time
3. Chunk-first spatial access enables efficient viewport operations
4. Per-month Zarr chunks enable efficient temporal navigation
5. No per-product storage duplication

## What to stop investigating

- More raw monthly delta variants
- Brightness/norm decomposition
- Chasing large H2 wins in the same representation space

## Where temporal encoding might still be worth testing (future)

- Sub-monthly cadence (weekly/daily composites with higher redundancy)
- Different delta domains (band ratios, decorrelated channels)
- Seasonal-baseline + anomaly decomposition
