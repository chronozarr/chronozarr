# Full-resolution SAR playback and mask-cache verification

Local Chromium/Metal measurements, 2026-10-18. No release, upload, deployment,
or stored-data modification. The benchmark pins **LOD 0** throughout; it does
not permit automatic overview selection.

The remaining default-cache startup failure documented here was subsequently
fixed with a rolling buffer. See [default-cache playback](default-cache.md) for
the later implementation and measurements; this report retains the mask-fix
benchmark as a historical comparison.

## Method

`full-resolution.mjs` launches a fresh Chromium process for each sensor, view
and repeat. It disables browser HTTP caching and uses the local static server;
operating-system filesystem caches remain warm. The canvas is 2280 × 1584
pixels. Every case reopens at LOD 0, waits for a target frame, explicitly
prefetches, plays two loops at 30 steps/s and then two at 60 steps/s, and performs
20 isolated timestep switches. Each phase has a 15-second evaluation cap in
these runs, with a separate outer deadline. A failed or incomplete playback is
reported as such. The 30 and 60 runs share the same case's caches; their cache
conditioning is intentionally identical before and after the change.

Full extent uses nine cells × 110 dates for Sentinel-1, and 72 cells × 26 dates
for NISAR. The bounded view targets a 1024-pixel horizontal footprint near
(768, 768) in level-0 coordinates and touches nine cells for both stores. It is
full spatial resolution over a smaller viewport, rather than the whole NISAR
extent. Both use unchanged defaults: a 1.5 GiB combined imagery cache, an
additional 153.6 MiB auxiliary cache, and a 384 MiB GPU slot budget. NISAR's
whole 1872 MiB decoded loop exceeds the imagery cache; its nine-cell 234 MiB
window fits. No enlarged-cache result is presented as default behavior.

Reader network counters include worker traffic and are the primary request and
byte measurements. Phase-specific page CDP logs classify masks, data, duplicate
paths and transferred bytes; page CDP alone can omit worker requests. The raw
records preserve both scopes, exact production-source hashes, store-root hashes,
script hashes, frame completeness/LOD/cell counts, actual budgets and GPU slots.

The baseline is served from an immutable snapshot of the pre-fix `js/chronozarr`
and `js/demo` sources. Its source hashes match HEAD at the start of this work.
The optional `BASELINE_DIR` routes those files into the same viewer/server;
store objects and benchmark code are shared between the measurements.

`before-matched` contains one repeat of all cases. Its Sentinel-1 timing was
contended by an older preliminary process and is superseded by the isolated
Sentinel-1 `before-isolated` rerun. Its NISAR cases ran after that process was
terminated. The older `before` records are preliminary and excluded from the
comparison. The final after run uses three independent browser repeats.

## Reproduce

From the repository root, with no other timing jobs running:

```sh
# Snapshot the pre-fix production JS before editing it, preserving js/... paths.
BASELINE_DIR=/absolute/pre-fix-snapshot CAP_MS=15000 node bench/sar/full-resolution.mjs before-matched 1
CAP_MS=15000 node bench/sar/full-resolution.mjs after 3
node bench/sar/full-resolution-summary.mjs bench/sar/results/full-resolution-after.jsonl > bench/sar/results/full-resolution-after-summary.json
```

`SENSORS=sentinel1` or `SENSORS=nisar` restricts sensors;
`CASES=full` or `CASES=bounded` restricts viewports. Local stores are not bundled.

## Results

Medians of three final independent browser runs. Rates count actual timestep
advances between the first and last step, not animation callbacks. Each
successful playback completed at least two complete temporal loops, with no
partial or overview frames. All successful playback frames were LOD 0. Reader
counters, rather than page CDP byte totals, support the zero-wire statements.

| Case | Requested 30 steps/s | Requested 60 steps/s | Playback network |
|---|---:|---:|---|
| Sentinel-1, whole extent, default cache | 29.99 | 59.98 | zero requests and bytes in all six runs |
| Sentinel-1, nine-cell viewport, default cache | 29.98 | 59.92 | zero requests and bytes in all six runs |
| NISAR, whole extent, default cache | did not start | did not start | repeated imagery reads; see cache limit below |
| NISAR, nine-cell viewport, default cache | 30.00 | 59.66 | 30: zero in two runs; 12 requests / 4.86 MB in one; 60: zero in all three |
| NISAR, whole extent, **explicit 3 GiB cache diagnostic** | 29.98 | 54.79 | zero requests and bytes in all six runs |

NISAR's nine-cell 30-step/s outlier had six newly read imagery chunks and six
new masks, rather than repeated mask paths. Its explicit prefetch reported six
extra skipped chunks (219 fetched, 15 skipped versus the other runs' 225/9), so
that first paced run was incompletely warmed. All three subsequent 60-step/s
runs and all isolated switches were zero-wire. No claim of universally zero-wire
playback follows from a prefetch promise alone.

The pre-fix isolated Sentinel-1 baseline delivered 30.03 steps/s at 30 requested,
but issued 2023 mask requests, including 1033 repeated requests. At 60 requested
it made no progress for 15 seconds and issued 1528 mask requests, including 1481
repeats across 47 paths. The final code completed both loops at 59.98 steps/s
with zero requests. Prefetch mask paths were unique in every final case. NISAR
whole-extent mask repeats likewise disappeared: its first final 30-step/s trial
read 187 previously missing masks once each, and its following 60-step/s trial
read no masks.

The implementation retains exact binary masks in a compact, bounded memory tier
and restores decoded masks on demand. A first candidate removed network
repetition but restored arrays during every readiness scan, yielding only
15.92 steps/s at 60 requested in Sentinel-1. A nonmaterializing readiness check
removed that CPU bottleneck. Those rejected-candidate measurements are retained
in `results/full-resolution-candidate-v1.jsonl`, not pooled with the final data.

### Remaining whole-extent NISAR limit

Forcing all 72 NISAR cells at level 0 needs 1872 MiB for its 26-date decoded
imagery loop, before masks and GPU allocations. The default imagery cap is
1536 MiB. At these requested speeds the start buffer asks for 25 upcoming
frames, or 1800 MiB; it cannot retain that buffer under the default cap. Every
final default-cache whole-extent trial therefore made zero temporal advances
within 15 seconds and repeatedly fetched imagery chunks. This is a failed
playback result, not a slower successful rate. It is separate from the repaired
mask cache. Normal automatic playback may select an overview; this forced-level
benchmark explicitly prohibited that escape.

A separate diagnostic doubled the imagery cap to 3 GiB through the existing
`setBudgets` API, which also raises the auxiliary cap to 307.2 MiB. The GPU cap
remained 384 MiB. All three runs prefetched the complete 26-date × 72-cell loop, both
paced tests completed, and playback was zero-wire. Thirty steps/s held; sixty
requested delivered 53.36–55.63 steps/s (median 54.79). This establishes the
remaining memory and frame-throughput limits; the diagnostic does not change
the production default.

### Cold frames and isolated warm switches

| Final case | Median cold complete LOD 0 frame | Median warm switch | Warm p95 / maximum | Complete switches / attempts | Switch network |
|---|---:|---:|---:|---:|---|
| Sentinel-1, whole extent | 46.1 ms | 2.3 ms | 4.2 / 5.6 ms | 60 / 60 | zero |
| NISAR, nine cells | 48.3 ms | 2.0 ms | 2.9 / 3.2 ms | 60 / 60 | zero |
| NISAR, whole extent, default cache | 236.2 ms | 5.6 ms | 9.8 / 15.1 ms | 57 / 60 | nonzero; not a successful warm-cache gate |
| NISAR, whole extent, explicit 3 GiB | 233.5 ms before budget change | 5.5 ms | 8.8 / 16.9 ms | 60 / 60 | zero |

The cold gate of 500 ms passed locally even for NISAR's 72 cells. Whole-extent
NISAR with an enlarged cache had one 16.9 ms isolated switch; a strict every-
switch-under-16-ms claim is unsupported. These local warm-filesystem results do
not establish CDN or constrained-network behavior.

Final raw records and summaries are `results/full-resolution-after.jsonl`,
`results/full-resolution-after-summary.json`, `results/full-resolution-after-3gib.jsonl`
and `results/full-resolution-after-3gib-summary.json`. There were no page errors
in any final run. Each final record preserves the same production-source hashes.
The harness gained only an optional budget phase after the main runs; its
changed hash is recorded in the budget diagnostic, while the default execution
path is unchanged. Reproduce that explicit experiment with:

```sh
TOTAL_GIB=3 SENSORS=nisar CASES=full CAP_MS=15000 node bench/sar/full-resolution.mjs after-3gib 3
```

## Validation and decode gate

The separate existing-reader gate (`results/mask-fix-gates.json`) measured
80 WASM decodes per sensor: median **2.5 ms**, p95 **2.6 ms** for both, matching
the earlier median 2.5 ms baseline and passing the 5 ms chunk-decode gate.
That run's cold complete LOD 0 frames were 25.3 ms for Sentinel-1 and 182.2 ms
for NISAR. Its NISAR default-cache whole-extent stepping had three incomplete
switches out of four; those are excluded as successful warm-switch measurements.
The dedicated table above gives the fully cached smaller viewport and explicit
larger-budget results.

Validation recorded in `results/mask-fix-validation.json`: **570 JavaScript unit
tests passed with zero skips**, five GPU/browser tests passed, viewer package
checks passed, and the whitespace check passed. Tests cover exact mask round
trips, constrained-cache retention, abort/concurrency behavior and reader wiring.
No packages were released and no site or store was deployed.
