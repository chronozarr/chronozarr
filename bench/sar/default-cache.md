# Full-resolution playback within the default cache

## Method

`default-cache.mjs` is the prior full-resolution SAR harness with three temporal
loops instead of two, 50 ms cache sampling, and an input timestep trace. Each
case uses a fresh headless Chromium/Metal process, disabled browser HTTP caching,
the local static server and warm operating-system file caches. It opens LOD 0,
explicitly prefetches, requests 30 steps/s and 60 steps/s in that order, then
performs 20 isolated switches. The paced phases share caches within one case.
The whole NISAR view covers 72 cells × 26 dates; the Sentinel-1 regression covers
nine cells × 110 dates. No level fallback or cache-budget override is allowed.

The imagery budget remains 1536 MiB **jointly** for decoded and compressed
chunks. The auxiliary budget remains 153.6 MiB, and the separate existing GPU
slot budget remains 384 MiB. Samples record the simultaneous decoded,
compressed, imagery-total and auxiliary bytes; maxima of separate columns need
not occur simultaneously. Sampling observes resident cache accounting, rather
than process RSS or temporary network/decode allocations. It can miss peaks
between samples; cache enforcement is additionally covered by unit tests.

Reader network counters include workers. Page CDP gives request classes,
duplicate paths, and transferred bytes, but can omit worker traffic. Playback
input traces check temporal continuity and wrap progression. Paint records
check complete LOD 0 frames and the native-view cell count. Source hashes,
store-root hashes and the harness hash are retained in every raw record.

The baseline routes production JavaScript from the immutable snapshot at
`/tmp/chronozarr-nisar-default-baseline`, captured before this change and after
the mask-cache fix. Store objects and the harness are shared.

```sh
BASELINE_DIR=/tmp/chronozarr-nisar-default-baseline SENSORS=nisar CASES=full CAP_MS=10000 node bench/sar/default-cache.mjs before 1
SENSORS=nisar CASES=full CAP_MS=45000 node bench/sar/default-cache.mjs after 3
SENSORS=sentinel1 CASES=full CAP_MS=45000 node bench/sar/default-cache.mjs sentinel1-after 1
node bench/sar/default-cache-summary.mjs bench/sar/results/default-cache-after.jsonl > bench/sar/results/default-cache-after-summary.json
```

These measurements describe local playback with warm filesystem data. They do
not establish CDN or constrained-network throughput. A loop larger than the
cache must stream data again; zero network traffic is not a success condition.

## Results

The default-cache change removes the native NISAR start deadlock. It does **not**
make this 72-cell view sustain 30 or 60 steps/s on this machine. The 1872 MiB
decoded loop exceeds the 1536 MiB imagery cap, so continued streaming and buffer
pauses remain necessary with the current cache policy.

| Case | Requested steps/s | Achieved steps/s | Completion | Playback traffic |
|---|---:|---:|---|---|
| NISAR baseline, native 72 cells | 30 / 60 | zero progress | neither started within 10 s | repeated imagery requests |
| NISAR final, native 72 cells | 30 | **14.42** median; 14.27–14.48 | three loops in all three repeats | median 1.306 GB |
| NISAR final, native 72 cells | 60 | **17.57** median; 14.10–17.73 | three loops in all three repeats | median 1.108 GB |
| Sentinel-1 final, native nine cells | 30 | **30.00** | three loops, one regression repeat | zero requests / bytes |
| Sentinel-1 final, native nine cells | 60 | **59.95** | three loops, one regression repeat | zero requests / bytes |

Rates count actual successive timestep advances from the first to last step;
they include pauses between those steps. The NISAR runs all had four refill
pauses per paced phase, totaling 2.75–2.83 s at 30 requested and 2.70–3.80 s at
60 requested. Initial buffering after explicit prefetch was zero at 30 requested
and 41–49 ms at 60 requested. The slowest final 60 trial included a 1.77 s refill
pause; its third wrap segment achieved only 8.65 steps/s. The raw per-loop rates
are retained rather than hiding this variation in a single median.

All **470** final NISAR paced paints were complete LOD 0 frames covering all
72 cells, with three temporal wraps in every paced phase and no skipped or
repeated timestep advances. Two 60-step/s phases advanced one extra frame before
the harness's next 10 ms poll, hence 79 rather than 78 steps. Sentinel-1 produced
660 complete nine-cell LOD 0 paced paints with the same continuity checks.
No paced run used partial frames or overview fallback. `movieInfo` can retain
automatic-level planning diagnostics despite the explicit LOD override; the
actual paint events and their 72-cell LOD 0 coverage establish resolution.

There were **zero repeated mask paths** in every phase of the final default-cache
runs. NISAR's first paced phase fetched 504 distinct masks missing after bounded
prefetch, once each; its following 60-step/s phase fetched no masks. Imagery
paths repeat because the decoded loop cannot remain resident. Reader network
bytes in the table include workers; page CDP duplicate-path counts are a separate
scope and can differ around in-flight phase boundaries.

Every final sample stayed within the unchanged imagery and auxiliary limits.
The largest sampled NISAR joint imagery usage was 1535.99 MiB; auxiliary usage
peaked at 93.44 MiB. The budgets still report 1536 MiB imagery, 153.6 MiB
auxiliary and the existing 384 MiB GPU slot limit. This is cache accounting,
not a claim that browser RSS or total CPU/GPU memory stays below 1536 MiB.
The final median complete cold LOD 0 NISAR frame was 259.7 ms locally. Sentinel-1's
single cold frame was 242.8 ms. No page errors occurred in any final case.

The exact-harness baseline is `results/default-cache-before-matched.jsonl`;
final data are `results/default-cache-after.jsonl` (three NISAR repeats) and
`results/default-cache-sentinel1-after.jsonl` (one Sentinel-1 regression repeat),
with corresponding `-summary.json` files. They share harness hash
`6eca752c31a5c0365d88520ddf85be03cc636acb4dc8cc6dbff41eb0edc05154`.
The baseline uses a 10 s phase cap, and the final runs a 20 s failure cap; all
final phases finished before 11 s. Baseline prefetch itself hit its 10 s cap,
and both paced phases made zero advances while repeating imagery requests.
Its cold frame was 882.5 ms; cold timings are not used to claim a speedup.
The earlier `before` and `pilot` records are preliminary and excluded from
the final medians.

## Rejected cache-allocation diagnostic

`default-cache-diagnostic.mjs` temporarily lowered the decoded ceiling to
384 MiB while preserving the **same 1536 MiB joint cap**, and removed the new
prefetch timestep bound to allow distant compressed backing. This was a harness
experiment, not production behavior. It delivered 15.68 / 15.56 steps/s at
30 / 60 requested, with 69 / 77 short refill pauses and roughly 0.72 / 0.71 GB
of reader traffic. Its 1152 MiB compressed capacity still could not hold the
roughly 1359 MiB compressed loop alongside decoded frames. It did not establish
a useful playback improvement and was not adopted.

The completed diagnostic is `results/default-cache-compressed-diagnostic-v2.jsonl`.
The earlier unversioned diagnostic was interrupted during test overlap, used an
incorrect prefetch override signature, and is excluded. Production-source hashes
and explicit budget/override records distinguish these experiments from the
final default-cache runs.

## Verification

573 JavaScript unit tests passed with zero skips. Six selected GPU/browser
checks passed, including mask behavior, SAR rendering, playback lifecycle and
coarse playback. The first coarse-playback attempt overlapped the discarded
diagnostic and failed its timing check; its isolated rerun passed. Viewer-package
guards and whitespace checks passed. The separate decode/cold/switch gate results
are recorded in `results/default-cache-gates.json` by the parent verification run.
No package, site or store was released, uploaded or deployed.

The separate gate measured 80 decodes per sensor: median 2.5 ms, p95 2.7 ms,
passing the 5 ms per-chunk target. Its cold frames were 30.7 ms for Sentinel-1
and 193 ms for NISAR. Sentinel-1's four isolated warm switches were all complete,
median 2.4 ms, maximum 2.9 ms, with zero wire traffic. The generic gate's paused
full-extent NISAR switch check had three incomplete switches out of four; these
are excluded from successful warm-switch claims. A bounded streaming movie can
complete without making arbitrary paused switches into an oversized decoded
loop instantaneous.
