# ChronoFabric: v1 Architecture

**Status:** Proposal
**Date:** 2026-04-18
**Branch:** `feat/chronofabric`

Evolves the v0 chunk spec. All novelty is in two places: the **data model**
(how time and scale are encoded in Zarr) and the **browser runtime** (how the
client reconstructs, caches, and renders). Everything else stays boring.

---

## 1. What changes from v0

| Aspect | v0 | v1 |
|--------|----|----|
| Spatial resolution | Single (10m) | Multiscale pyramid (10m → 640m) |
| Temporal encoding | Independent months | Star-delta: anchors + residuals |
| Rendering | Server-side PNG/WebP | Client-side WebGPU from raw bands |
| Transport | One tile image per request | Zarr chunk byte ranges |
| Caching | HTTP cache only | OPFS persistent chunk store in browser |

| Aspect | v0 | v1 |
|--------|----|----|
| Storage format | Zarr v2 | Zarr v2 (unchanged) |
| Compression | Blosc(zstd, bitshuffle) | Zstd(level=5) — no Blosc wrapper |
| Server | FastAPI | Same |
| API style | REST | Same |
| Auth | API key | Same |
| Edge | Cloudflare | Same |

The top table is the novelty. The bottom table is what stays the same.

---

## 2. Data Model

### 2.1 Multiscale pyramid

Each AOI gets a Zarr group hierarchy with resolution levels:

```
{store_root}/{aoi}/
  manifest.json
  lod/
    0/                          # 10m native
      chunks/
        r000_c000/stack.zarr    # (n_months, 4, 512, 512) uint16
        r000_c001/stack.zarr
        ...
    1/                          # 20m (2x downsample)
      chunks/
        r000_c000/stack.zarr    # (n_months, 4, 256, 256) uint16
        ...
    2/                          # 40m
    3/                          # 80m
    4/                          # 160m
    5/                          # 320m
    6/                          # 640m
```

Each LOD level halves pixel dimensions and doubles ground resolution.
LOD 0 = native 10m Sentinel-2. LOD 6 = 640m, ~7 levels total.

Chunk size stays 512px at LOD 0. Lower LODs use whatever pixel count results
from the downsample (a 6x6 grid at LOD 0 becomes 3x3 at LOD 1, etc.).

Downsampling method: area-weighted mean (antialiased). Computed at encode time,
not on the fly.

### 2.2 Star-delta temporal encoding

Instead of storing every month at full uint16 fidelity, store **anchors** at
full fidelity and **deltas** against the nearest anchor for everything else.

**Star topology, not chain.** Every delta references a fixed anchor, not the
previous frame. Decoding any month requires exactly one anchor + one delta.
No chain accumulation, no drift.

```
Anchor schedule: every 6 months (configurable per AOI)

Timeline:
  2023-01  [anchor]
  2023-02  [delta vs 2023-01]
  2023-03  [delta vs 2023-01]
  2023-04  [delta vs 2023-01]  ← worst case: 3 months of drift
  2023-05  [delta vs 2023-07]  ← forward reference is fine
  2023-06  [delta vs 2023-07]
  2023-07  [anchor]
  2023-08  [delta vs 2023-07]
  ...
```

Each delta month references whichever anchor is closer in time.

### 2.3 On-disk layout per cell

```
r000_c000/
  stack.zarr        # (n_months, 4, H, W) uint16 — anchors at full fidelity
                    # delta months stored as: anchor_value + delta = true_value
                    # physically: anchor chunks are uint16
                    #             delta chunks are int16 (reinterpreted)
```

**Critical choice: single array, mixed dtype via flag.**

We keep one `stack.zarr` per cell. The manifest declares which time indices are
anchors (uint16) and which are deltas (int16, stored in the same uint16 array
via reinterpret cast). The client knows how to reconstruct:

```python
# Decode month t:
if t is anchor:
    return stack[t]                      # uint16, use directly
else:
    anchor_t = manifest.nearest_anchor(t)
    anchor = stack[anchor_t]             # uint16
    delta = stack[t].view(np.int16)      # reinterpret uint16 → int16
    return (anchor.astype(int32) + delta.astype(int32)).clip(0, 65535).astype(uint16)
```

This avoids separate files for anchors vs deltas. The Zarr chunk layout stays
`(1, 4, H, W)` — each time slice is independently addressable via byte range.

### 2.4 Manifest v1

```json
{
  "version": "1.0.0",
  "aoi": "sahara_tamanrasset",
  "epsg": 32631,
  "transform": [10.0, 0.0, 599700.0, 0.0, -10.0, 2541200.0],
  "mosaic_height": 2814,
  "mosaic_width": 2616,
  "bands": ["B02", "B03", "B04", "B08"],
  "dtype": "uint16",
  "nodata": 0,
  "months": ["2023-01", "2023-02", "..."],

  "lod_levels": 7,
  "lod_factor": 2,
  "chunk_sizes": [512, 256, 128, 64, 32, 16, 8],
  "grid_dims": [[6,6], [3,3], [2,2], [1,1], [1,1], [1,1], [1,1]],

  "temporal": {
    "encoding": "star-delta",
    "anchor_indices": [0, 6, 12, 18],
    "anchor_months": ["2023-01", "2023-07", "2024-01", "2024-07"],
    "delta_reference": {
      "1": 0, "2": 0, "3": 0, "4": 6, "5": 6,
      "7": 6, "8": 6, "9": 6, "10": 12, "11": 12,
      "13": 12, "14": 12, "15": 12, "16": 18, "17": 18
    }
  },

  "cells": {
    "r000_c000": {
      "volatility": 0.03,
      "valid_months": [0,1,2,3,4,5,6,7,8,9,10,11]
    },
    "r002_c003": {
      "volatility": 0.41,
      "valid_months": [0,1,2,3,4,5,6,7,8,9,10,11]
    }
  }
}
```

`volatility` is the mean absolute delta across all bands, normalized to [0,1].
Clients use it for prefetch priority: high-volatility cells need their deltas
fetched; low-volatility cells look fine with anchor-only display.

### 2.5 Compression benefit

For a stable scene (Sahara), experimental.py showed deltas compress to ~14% of
anchor size because most pixels are near-zero. Star-delta is slightly worse than
chain-delta (can't exploit sequential correlation) but the random-access benefit
is decisive for scrubbing.

For a seasonal scene (Iowa cropland), deltas are larger but still structured —
change is spatially coherent (whole fields flip), which bitshuffle + zstd handle
well. Expected 30-50% size reduction vs independent months.

---

## 3. Browser Runtime

This is where the real departure from v0 lives. The browser becomes a temporal
data engine, not an image blitter.

### 3.1 Module architecture

```
Main thread
  ├── UI / map interaction / timeline scrubber
  ├── Planner (viewport → cell list → fetch priority)
  └── GPU renderer (WebGPU)

Worker pool (2-4 dedicated workers)
  ├── Net worker: fetch manifest, stream Zarr chunks via range requests
  ├── Decode worker(s): decompress zstd+bitshuffle, reconstruct from delta
  └── Cache worker: OPFS read/write, eviction

Shared state
  ├── Decoded cell buffers (transferable ArrayBuffers, not SAB for now)
  └── Cell residency table (which cells/months are GPU-resident, RAM-cached, OPFS-cached)
```

**No SharedArrayBuffer in MVP.** SAB requires cross-origin isolation headers
(COOP/COEP) which break third-party map tile loads (Mapbox, OSM basemaps).
Use transferable ArrayBuffers instead. SAB is a future optimization.

### 3.2 Planner

The planner maps viewport state to a prioritized fetch list:

```
Input:  viewport bounds, zoom level, target month, scrub velocity
Output: ordered list of (cell_id, lod, month_index, priority)
```

Priority rules:
1. Visible cells at appropriate LOD > offscreen cells
2. Center of viewport > periphery
3. Anchor months > delta months (anchors are reusable)
4. Low-volatility cells: anchor only when scrubbing fast
5. Prefetch: adjacent months, panning-direction fringe cells

LOD selection: `lod = clamp(max_lod - floor(zoom), 0, max_lod)`. The map zoom
level maps directly to the pyramid level. No per-cell adaptive LOD in MVP.

### 3.3 Fetch + decode pipeline

```
1. Planner emits fetch request: (cell_id, lod, month_index)
2. Cache check:
   a. GPU-resident? → skip, already displayable
   b. RAM cache?    → skip fetch, send to GPU upload
   c. OPFS cache?   → read from disk, decompress, send to GPU
   d. Miss          → fetch from server
3. Fetch:
   - GET /v1/raw/{aoi}/{lod}/{chunk_id}/{month_index}
   - Returns raw Zarr chunk bytes (zstd+bitshuffle compressed uint16)
   - Or: byte-range into the Zarr store directly (future, when on object storage)
4. Decode (in worker):
   - Decompress zstd+bitshuffle → uint16[4][H][W]
   - If delta month: also fetch/lookup anchor, reconstruct
   - Transfer decoded ArrayBuffer to main thread
5. GPU upload:
   - Create GPUTexture from decoded bands
   - Band math runs in fragment/compute shader
6. OPFS writeback:
   - Cache worker writes compressed chunk to OPFS for future sessions
```

### 3.4 WebGPU rendering

Replaces current WebGL2 path. Same band math, better temporal capabilities.

**Texture format:** `r16uint` per band. Four bands = four textures per cell,
or one `rgba16uint` texture if packing is worth the complexity.

**Render pipeline:**

```wgsl
// Fragment shader: true color from raw bands
@fragment
fn true_color(@location(0) uv: vec2f) -> @location(0) vec4f {
    let b02 = f32(textureLoad(t_b02, vec2i(uv * dims), 0).r) / 10000.0;
    let b03 = f32(textureLoad(t_b03, vec2i(uv * dims), 0).r) / 10000.0;
    let b04 = f32(textureLoad(t_b04, vec2i(uv * dims), 0).r) / 10000.0;

    // Same highlight compression + gamma as shaders.js
    let r = sAdj(b04);
    let g = sAdj(b03);
    let b = sAdj(b02);
    return vec4f(satEnhance(vec3f(r, g, b), 1.5), 1.0);
}
```

**Temporal interpolation** (the novel part):

```wgsl
// Blend between two decoded frames for smooth scrubbing
@fragment
fn temporal_blend(@location(0) uv: vec2f) -> @location(0) vec4f {
    let px = vec2i(uv * dims);
    let val_a = textureLoad(t_frame_a, px, 0);  // month A bands
    let val_b = textureLoad(t_frame_b, px, 0);  // month B bands
    let t = uniforms.blend_factor;               // 0.0 = frame A, 1.0 = frame B

    // Per-band linear interpolation in reflectance space
    let blended = mix(vec4f(val_a), vec4f(val_b), t);

    // Then apply product shader (true_color, ndvi, etc.)
    return product_shader(blended);
}
```

When scrubbing between March and April:
- If both months are decoded and GPU-resident, interpolation is a single
  shader dispatch. Sub-millisecond.
- If the target month isn't loaded yet, display the nearest anchor with a
  loading indicator. No pop-in — just progressive refinement.

### 3.5 OPFS cache

The browser persists decoded chunks in the Origin Private File System:

```
/tileripper/
  /manifests/
    sahara_tamanrasset.json
  /chunks/
    /sahara_tamanrasset/
      /lod0/
        /r000_c000/
          /m000.bin    # compressed Zarr chunk bytes
          /m001.bin
        /r000_c001/
          ...
      /lod1/
        ...
  /meta.json           # LRU table, total size, last access times
```

Eviction policy: LRU with temporal bias. A cached anchor is worth more than a
cached delta (anchors serve multiple months). Target cache size: 200 MB
(configurable). The cache worker runs eviction checks when approaching quota.

### 3.6 Time scrubbing interaction model

```
Scrub velocity → quality mode:

  STOPPED (v=0):
    Load target month at full LOD.
    Fetch delta if needed, reconstruct, display observed data.
    Label: "March 2024 (observed)"

  SLOW (v < 2 months/sec):
    Load target month, accept anchor-only if delta not cached.
    Prefetch adjacent months.
    Label: "March 2024 (observed)" or "March 2024 (anchor)"

  FAST (v >= 2 months/sec):
    GPU-interpolate between nearest two cached anchors.
    Don't fetch deltas.
    Show low-LOD if high-LOD not cached.
    Label: "~March 2024 (interpolated)"

  RELEASED (v drops to 0):
    Refine: fetch target month at full LOD, replace interpolated frame.
    Progressive: low LOD → high LOD → delta-corrected.
```

### 3.7 Progressive loading sequence

For a cold viewport (nothing cached):

```
Phase 0 (instant):    Show map basemap tiles, empty cells with loading state
Phase 1 (~100ms):     Fetch manifest
Phase 2 (~200ms):     Fetch LOD 6 (one chunk covers entire AOI) → coarse preview
Phase 3 (~400ms):     Fetch LOD 4 anchor for target month → medium preview
Phase 4 (~800ms):     Fetch LOD 0 anchors for visible cells → full res anchors
Phase 5 (~1200ms):    Fetch LOD 0 deltas for visible cells → observed data
```

For a warm viewport (OPFS has anchors):

```
Phase 0 (instant):    Display cached anchor from OPFS
Phase 1 (~200ms):     Fetch delta for target month → observed data
```

---

## 4. Server (stays boring)

The server adds two things to the current v1 API:

### New endpoint: raw chunk access

```
GET /v1/raw/{aoi}/lod/{lod}/chunk/{chunk_id}/month/{month_index}
```

Returns the raw Zarr chunk bytes. Content-Type: `application/octet-stream`.
Content-Encoding: identity (chunk is already zstd-compressed internally by
Zarr's Blosc codec).

This is the byte-range equivalent without needing the client to understand
Zarr's chunk file layout. The server reads the single Zarr chunk and returns it.

### Manifest endpoint

```
GET /v1/manifest/{aoi}
```

Returns the v1 manifest JSON from section 2.4.

### What the server does NOT do

- No raster rendering (that's the browser's job now)
- No temporal reconstruction (client does anchor + delta)
- No reprojection (client or not at all — storage stays UTM)
- No custom binary protocol
- No WebSocket streaming
- No server-side cache management

The existing PNG/WebP tile endpoints stay for backward compatibility and for
clients that can't run the WebGPU path (curl, QGIS, simple embeds).

---

## 5. Transport (stays boring)

HTTP/1.1 or HTTP/2. Standard GET requests. Cloudflare caches responses by URL.
No custom headers beyond what exists today (`X-Timing-Ms`, `X-API-Key`).

When the data moves to object storage (B2/S3), the raw chunk endpoint becomes
a signed URL redirect or a direct range request against the Zarr store. The
browser's fetch path doesn't change — it's still "GET bytes, decompress, display."

---

## 6. Encode pipeline changes

`baseline_b.py` → `encode_v1.py`:

```python
def encode_v1(monthly_mosaics, grid, store_dir, anchor_interval=6, n_lods=7):
    """
    1. For each LOD level:
       a. Downsample all monthly mosaics by 2^lod
       b. Compute anchor schedule
       c. For anchor months: store uint16 directly
       d. For delta months: compute int16 delta vs nearest anchor,
          store as uint16 (reinterpret cast)
       e. Write single stack.zarr per cell with (1, 4, H, W) chunking
    2. Compute per-cell volatility scores
    3. Write manifest.json
    """
```

The encoder runs once at ingest time. It replaces `encode_b2_chunked_time()`
and subsumes `experimental.py`.

---

## 7. Migration from v0

1. Re-encode existing AOIs with `encode_v1()` (adds pyramid + delta encoding)
2. Serve both v0 and v1 endpoints during transition
3. v0 tile endpoints (`/v1/tiles/`) keep working, backed by LOD 0 anchors
4. New browser runtime uses v1 endpoints exclusively
5. Drop v0 tile rendering when the WebGPU client is stable

No data model migration needed — v0 stores are just "v1 with 1 LOD level and
all-anchor temporal encoding." The v1 reader handles both.

---

## 8. What this is NOT

- Not a new binary format. It's Zarr.
- Not a new transport protocol. It's HTTP.
- Not a video codec. Deltas are block-compressed arrays, not I/P/B frames.
- Not a spatial indexing innovation. Grid-of-chunks, same as v0.
- Not a server-side rendering system. The browser does all rendering.

The novelty is: **the temporal encoding in the Zarr layout** (star-delta with
volatility metadata) and **the browser engine that reconstructs from it**
(WebGPU, workers, OPFS, progressive temporal refinement).

Everything else is deliberately conventional.

---

## 9. Decisions (frozen 2026-04-18)

All six open questions resolved. Benchmark data in `/static/bench.html`.

### Q1: Zarr v2 vs v3
**v2 now, v3 mental model.** v2 chunk files (`0.0.0.0`, etc.) work fine —
the server serves individual files, no range math. Revisit v3 sharding when
the number of AOIs or months makes many-small-files a real problem.

### Q2: Delta axis
**Star-delta.** Every delta references a fixed anchor, not the previous frame.
Decoding any month = one anchor + one delta. No chain, no drift.
Annual or seasonal anchor cadence.

### Q3: Pyramid scheme
**Powers of 2 from 10m native.** 10 → 20 → 40 → 80 → 160 → 320 → 640m.
7 levels. Part of the temporal runtime, not a sidecar.

### Q4: WebGPU
**Not required for MVP.** WebGL2 is the primary path. WebGPU is Phase 2
for client-side band math, temporal interpolation, and compute compositing.

### Q5: Smooth scrubbing
**Month-step is enough for MVP.** Smooth scrubbing is a future delight.
The product wedge is temporal access + queryable values, not cinematic playback.

### Q6: Browser codec (benchmarked)
**Plain zstd via fzstd (3.8KB pure JS). Drop Blosc wrapper.**

Benchmark results (2MB chunks, median of 10 runs):

| Codec | Decode (ms) | Ratio | Browser path |
|-------|------------|-------|--------------|
| zstd (fzstd) | 16 | 1.33x | pure JS, 3.8KB |
| gzip (native) | 7.8 | 1.34x | DecompressionStream |
| star-delta (2× zstd + add) | 29 | 1.65x | fzstd + loop |
| Blosc(zstd,bs) | not tested | 1.55x | needs WASM (~150KB) |

Verdict: zstd decode fits in one frame (16ms budget). Star-delta is 29ms
total but in practice the anchor is cached — real cost is ~17ms.
Blosc's 12% size advantage does not justify a WASM dependency.

**v1 stores use `numcodecs.Zstd(level=5)`, not `Blosc`.**
