# SAR creation, storage and playback experiments

Measured locally on 2026-10-18, starting at `ca799a1`. These experiments use
Sentinel-1 RTC monthly backscatter at Ucayali and NISAR GCOV at Yuma. Original
stores and published objects are unchanged. Nothing was released or deployed.

After merging `origin/main` (2026-10-22): [the same claims on the merged code](merge-main.md),
against main and against the branch before the merge, on the built viewer, and on a link that cannot carry the speed.

Previous: [faster NISAR Play startup](startup.md) measures decoder-worker packing
while preserving steady native 30 frames/s and the same default caches.

Earlier: [steady native NISAR playback at 30 frames/s](steady30.md) measures
lossless in-memory packing within the unchanged default caches. The earlier
14 frames/s results in [default-cache playback](default-cache.md) are historical.

The subsequent mask-retention fix and matched LOD 0 playback measurements are
documented in [full-resolution playback](full-resolution.md). The baseline and
60 steps/s diagnostic below describe the original reader, before that fix.
The later [default-cache playback fix](default-cache.md) removes NISAR's
whole-image startup deadlock without raising the imagery cache limit.

## Reproduce

Run from the repository root, with the existing local stores and acquisition
caches. Run timing jobs sequentially, with other benchmark processes stopped.

```sh
uv run python bench/sar/creation_benchmark.py > bench/sar/creation_results.json
uv run python bench/sar/creation_nisar_cache.py > bench/sar/creation_nisar_results.json
uv run python bench/sar/storage_benchmark.py
node bench/sar/playback.mjs 3 natural
node bench/sar/playback-summary.mjs bench/sar/results/playback-natural.jsonl > bench/sar/results/playback-summary.json
```

The scripts retain raw measurements, environment details and source fingerprints.
Local datasets are not bundled with the benchmark. Source archive download speed
is excluded: the creation experiment starts from cached acquisitions and the
storage experiment starts from decoded windows of existing stores.

## Creation and storage

Sentinel-1 monthly compositing now consumes cached acquisitions as an iterator
rather than retaining the entire month. It preserves the original input order,
float64 sums and joint validity. Three largest cached months, three alternating
isolated repeats per implementation, gave median aggregation times of 104.1 ms
before and 89.9 ms after. Median process peak RSS fell from 337.1 to 259.9 MB
(22.9%). VV, VH and mask hashes match across all eighteen processes. These are
six- and seven-acquisition months; typical months contain fewer acquisitions and will
benefit less. Timing excludes remote reads, output archive writes, percentiles
and Zarr encoding. Peak RSS includes imports and conversion to stored values.
See `creation_results.json`; the iterator and complete monthly-file path have
unit coverage in `tests/test_sentinel1_ingest.py`.

NISAR's intermediate NPZ cache has a separate creation tradeoff. Across three
full spatial cached windows (first/middle/last, three isolated repeats each),
median compressed-cache write/read times were 2.958 s / 0.226 s, versus
0.025 s / 0.022 s uncompressed. Fully covered windows increased from about
56.4 MB to 89.1 MB (58% more disk); the partially covered first window grew
from 27.2 MB to 89.1 MB. Arrays matched exactly. The first window is an older
partial date excluded from the public v2 store. These timings exclude remote
HDF5 extraction and Zarr encoding, use warm local filesystem caches, and do
not force durable disk writes. This supports offering an uncompressed scratch
cache for creation speed where disk permits; the current NISAR default was
not changed. Raw results and provenance: `creation_nisar_results.json`.

Validation: 19 Sentinel-1/NISAR ingest unit tests passed. Ruff checks passed for
the changed Python sources and benchmark scripts; JavaScript benchmark syntax
checks and the final diff whitespace check passed. The only production edit
is Sentinel-1's streaming monthly composite. No read-path code was modified.

The storage matrix ran sixty isolated encodes: three repeats of ten candidates
on both sensors. It uses three dates and distributed 1024-square windows per
sensor. Keep the existing zstd level 5 and 512-pixel chunks: zstd 9 saved less
than 0.04% and increased process peak memory; byte shuffle did not improve
size. 256-pixel chunks roughly doubled local complete-window read time.
All thirty recorded lossless pyramid comparisons matched baseline data and
auxiliary planes. Full matrix, scope and provenance: [storage report](storage_report.md).

Experimental 0.05 dB rounding saved 9.5% for NISAR and 14.1% for Sentinel-1,
with at most 0.02 dB additional error relative to the existing stores. At 0.1 dB,
savings were 19.8% and 24.0%, with at most 0.05 dB additional error. These
changes were not adopted. Sampling dates and locations vary together; neither
full-store savings nor scientific equivalence is established.

## Playback baseline

Three fresh Chromium runs per store, each containing three cold reader reloads,
on an Apple M3 Max with Metal rendering. HTTP cache is disabled, but operating
system filesystem caches are warm. The drawing canvas is 2280 × 1584 pixels.
These are local delivery measurements, not uncached CDN or constrained-link
measurements. Metadata timing excludes downloading the viewer application.

| Measurement | Sentinel-1 | NISAR |
|---|---:|---:|
| Median metadata open | 2.2 ms | 2.5 ms |
| Median complete LOD 0 frame | 28.5 ms | 186.8 ms |
| Median decode of a 1 MiB data chunk | 2.5 ms | 2.5 ms |
| Automatic warm playback at requested 30 steps/s | ~30 steps/s, LOD 0, 9 cells | ~30 steps/s, LOD 2, 6 cells |
| Holds / partial frames in paced runs | 0 / 0 | 0 / 0 |

The stores have different extents, date counts and cell counts. This table does
not establish a sensor speed ratio. Sentinel-1's isolated LOD 0 cached stepping
completed in median 2.5–2.9 ms across runs with zero requests. NISAR's generic
LOD 0 "warm" test had 19 incomplete switches out of 20 in each run: its
prefetch exceeded the cache budget, so those timings are not successful warm
switch measurements. Automatic movie level selection instead chose LOD 2 and
completed the paced runs.

Smooth playback does not imply zero network traffic. The automatic runs still
issued background requests; pinned LOD 2 runs completed without requests. See
`results/playback-summary.json`, raw `results/playback-natural.jsonl`, and the
separate preliminary measurements. Reader counters and page CDP counters have
different scopes; page CDP alone does not observe all worker traffic.

## Interpretation and next decisions

The existing GPU/codec path handles the tested SAR data without a SAR-specific
renderer. The highest-priority playback question is unnecessary background
traffic and mask-cache retention, rather than a different SAR shader. Full
resolution playback must respect memory and link budgets.

Storage candidates are experiments, not adopted defaults. Rounding to coarser
dB increments changes data; errors are measured relative to the current 0.01 dB
stores, not the original floating-point powers. No scientific acceptance of
coarser precision follows from a compression result. The current pyramid still
averages stored dB values, as required by the existing convention; this work
does not change that rule.

### 60 steps/s diagnostic and mask traffic

A separate fresh-browser diagnostic requested 60 steps/s. Sentinel-1 automatic
level selection made **zero movie steps** before the existing playback deadline;
explicitly pinned LOD 2 then completed 110 steps at 59.71 steps/s with zero
requests. NISAR was interrupted while closing that diagnostic and has no usable
60 steps/s measurement. A second attempt with explicitly bounded prefetch was
stopped without a completed record. These are diagnostic outcomes, not repeated
60 steps/s performance results. The 30 steps/s baseline had first run the full
cold/decode/switch benchmark, so it had different cache conditioning; its success
cannot be directly compared with the fresh-browser 60 steps/s failure.

The completed Sentinel-1 diagnostic's **whole-session** CDP log contains 7,869
LOD 0 mask requests versus 1,034 LOD 0 data requests. Individual mask paths were
requested 273 times. Its playback reader counter reported 6,843 requests and
8.66 MiB while the automatic movie made zero steps. These scopes differ:
whole-session CDP counts include warmup, reloads and post-play switches; they do
not classify every request inside the measured movie window. The browser's
resource timing buffer also truncated that first diagnostic. This is strong
evidence to investigate repeated auxiliary-mask reads and cache retention, but
does not independently prove which cache policy caused them. No reader change
was made. Failure records and the successful pinned run are retained in
`results/playback-60fps.jsonl`.

The final harness includes a 120-second cap per benchmark evaluation, a
15-second explicit-prefetch cap for `PLAY_ONLY=1`, an expanded resource timing
buffer, and records prefetch results and per-switch completeness. The older
`playBench` helper's quiet-network loop can otherwise wait indefinitely while
requests continue. Reproduce the bounded diagnostic with:

```sh
FPS=60 PLAY_ONLY=1 OUT=bench/sar/results/playback-60fps-new.jsonl node bench/sar/playback.mjs 1 natural
```
