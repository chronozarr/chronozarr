# SAR playback after merging main

Measured 2026-10-22 on an Apple M3 Max (16 cores), headless Chromium on the Metal backend, HTTP cache off, local stores
with warm operating-system file caches. Three builds were measured, one fresh browser per run, alternated within each
pass with the order rotated, so that the load of the machine falls on all of them:

| Build | Tree |
|---|---|
| main | `origin/main` at `04395f4` |
| branch | this branch before the merge, `3c91d9b` |
| merged | this branch with `origin/main` merged |

main and branch ran as whole trees from `git archive` with their own static servers, so no Playwright route sits in front of
their modules. (An earlier pass served their `js/` through `BASELINE_DIR` routes. Every module request then paid a route
round trip, which lengthened cold frames by 40 to 60 ms and one branch run's initial buffering to 5.6 and 6.2 s; its
ordering of the three builds was the same. Those runs are not used here.) Other work shared the machine: the 1-minute load
average was 7.7 before the first run and 8 to 36 at the start of the runs of the pass reported here, much of it the preceding
run's own Chromium. The speed-gate runs below are the ones whose load went above 100.

Numbers are per run, in the order of the runs. Cache figures are resident cache accounting, as in the other reports.

## Native LOD 0, whole NISAR scene (72 cells, 26 dates), default caches

Ordinary Play at 30 steps/s with no explicit prefetch, 12 loops (312 frames), cap 45 s (`startup.mjs`, N = 3):

| | Frames in the cap | Steps/s | First step after Play | Requests after the first step | Peak imagery cache |
|---|---|---|---|---|---|
| main | 134, 131, 133 of 312 | 3.66, 3.63, 3.63 | 8.6, 9.1, 8.6 s | 19,525, 19,215, 19,412 (3.5 GiB) | 1536 MiB (the cap) |
| branch | 312 | 30.001 | 3.156, 3.093, 3.106 s | 0 | 1310.5 MiB |
| merged | 312 | 30.001 | 3.113, 3.136, 3.118 s | 0 | 1310.5 MiB |

Explicit prefetch, then 12 loops (`steady30.mjs`, N = 3):

| | Frames in the cap | Steps/s | Prefetch | Requests during Play | Frame interval p95 |
|---|---|---|---|---|---|
| main | 145, 130, 139 of 312 | 3.62, 3.20, 3.41 | 5.0, 5.0, 5.5 s | 23,001, 18,710, 19,993 | 367 to 400 ms |
| branch | 312 | 30.000, 30.003, 29.980 | 4.38, 4.65, 4.61 s | 0 | 33.7 to 34.6 ms |
| merged | 312 | 29.993, 29.997, 29.990 | 4.34, 4.60, 4.74 s | 0 | 34.2 to 35.0 ms |

Merged and branch: zero refill pauses, no frame interval over 50 ms, 72 cells at LOD 0 in every paint, auxiliary cache peak
93.4 MiB, GPU textures 383.1 MiB. 60 loops (1560 frames, `LOOPS=60`): branch 29.995 steps/s with 8 intervals over 50 ms
(maximum 50.6 ms), merged 29.997 with none (maximum 41.2 ms).

The earlier "does not start" result of `default-cache.mjs` (main before the fix: no progress in 10 s) is a crawl on current main:
its link rule lowers the speed to what the link feeds, but the 1872 MiB decoded loop still exceeds the 1536 MiB cap, so frames
are fetched again. 3 loops at 30 and then 60 steps/s requested (N = 1 each):

| | 30 requested | 60 requested | Requests (30, 60) |
|---|---|---|---|
| main | 3.38 steps/s | 3.55 | 11,661 and 11,098 |
| branch | 29.99 | 59.54 | 0 and 0 |
| merged | 29.93 | 59.51 | 0 and 0 |

The three merged runs of the routed pass gave 29.94, 30.09 and 30.01 at 30 requested and 60.13, 59.58 and 59.56 at 60, with no requests.

## The built viewer

`BUILT_ROOT=<folder holding demo/> node bench/sar/startup.mjs ...` opens the viewer as `js/support/build-viewer.mjs` writes it
(the bundle and `decode-worker.js`, where the packing now runs). N = 3 each, built from `origin/main` and from this tree:

| | Steps/s | First step after Play | Requests after the first step |
|---|---|---|---|
| main, startup | 3.38, 3.40, 3.44 (122 to 124 of 312 in 45 s) | 9.1 s | 16,551 to 17,300 |
| merged, startup | 30.001 | 3.382, 3.243, 3.316 s | 0 |
| main, explicit prefetch | 3.34, 3.13, 3.20 | | |
| merged, explicit prefetch | 30.006, 29.976, 29.996 | | 0 |

One merged run (29.976) had six intervals over 50 ms (maximum 52.1 ms) while the machine's load was 36 at its start; its
loops ran at 29.45 to 30.55 steps/s. Bundle sizes, minified and brotli level 5: `viewer.js` 64,188 to 66,671 bytes
(+2,483), `decode-worker.js` 8,060 to 8,509 (+449).

## Sentinel-1 (9 cells, 110 dates)

| | Steps/s, Play | Intervals over 50 ms | Requests after the first step | Steps/s, after prefetch |
|---|---|---|---|---|
| main | 27.42 | 25 | 3,865 | 22.75 |
| merged | 30.001 | 0 | 771 | 29.99 |

`default-cache.mjs` (routed pass): main 19.86 steps/s at 30 requested and 1.56 at 60 (71 of 330 frames in 45 s); merged 29.99 and
59.83, with no requests. At Play, main waits 0.78 s and merged 1.19 s for the start buffer of 60 frames. During Play main
requested 3,964 masks for 990 distinct ones (its mask cache evicts them and reads them again), merged 1,002 (21 repeats).

## A link that cannot carry the speed

Open on an unthrottled link, limit it by CDP, press Play at 30 steps/s (`throttled.mjs`: first step, steps in the window, pauses):

| Store and link | main | branch | merged |
|---|---|---|---|
| Sentinel-1, 100 Mbit/s, 40 s | first step 1.9 s, 63 steps, 0 pauses, 374 MiB | first step 30.4 s, 62 steps, 1 pause, 471 MiB | first step 1.9 s, 63 steps, 0 pauses, 374 MiB |
| NISAR LOD 0, 800 Mbit/s, 50 s | first step 1.4 s, 36 steps (0.25 steps/s at the end, 4,608 requests) | first step 17.0 s, 994 steps | first step 2.1 s, 1001 steps; 1 to 3 steps/s while the loop loads, 30 steps/s from about 18 s on, 0 pauses |

On a limited link main lowers the speed to what the link feeds (6 to 9 steps/s at the start of the NISAR loop on a fast local
link), the branch alone waits for 60 frames or the whole loop, and merged starts like main and, once the packed loop is
resident, speeds up to the requested speed, which main cannot do because its decoded loop does not fit.

## Speed gates, head against base

`js/support/gate-bench/run.mjs`, base built from `origin/main`, head from this tree, 5 pairs after one warm-up pair, the 36-cell
gate store (it has no mask and fits the cache, so it measures the unpacked path):

| Gate | SwiftShader (as CI), base / head | Metal, base / head |
|---|---|---|
| Cold open, level 0, 36 cells (gate 500 ms) | 129 / 136 ms (1.04) | 138 / 143 ms (1.00) |
| Warm switch, stepping (gate 16 ms) | 9.3 / 9.3 ms | 6.8 / 6.8 ms |
| Warm switch, jumping | 5.0 / 4.7 ms | 8.2 / 8.2 ms |
| Decode per 2 MiB chunk (gate 5 ms) | 4.5 / 4.6 ms | 4.5 / 4.5 ms |
| Wire bytes on a warm switch | 0 | 0 |

Both runs pass the job's tolerances. The 1-minute load average was 46 at the start and 64 at the end of the SwiftShader run
and 115 at the start and 21 at the end of the Metal run (SwiftShader's threads count in it). Head's SwiftShader cold open was
at or above base's in all five pairs (+8, +6, +1, +8, +3 ms); the Metal run shows no such order. `bench/sar/gates.mjs` still
runs on the merged code: decode median 1.8 ms (Sentinel-1) and 1.9 ms (NISAR), cold LOD 0 frame 35 and 159 ms.

## Reproduce

```sh
# main as a whole tree, with this branch's bench scripts and the local stores
git archive origin/main | tar -x -C /path/main && cp bench/sar/*.mjs /path/main/bench/sar/
ln -s "$PWD/bench/node_modules" /path/main/bench/node_modules; ln -s "$PWD/js/node_modules" /path/main/js/node_modules
mkdir -p /path/main/data && ln -s "$PWD/data/stores" /path/main/data/stores
(cd /path/main && CAP_MS=45000 node bench/sar/startup.mjs main 3)
CAP_MS=45000 node bench/sar/startup.mjs merged 3
CAP_MS=45000 node bench/sar/steady30.mjs merged 3
node js/support/build-viewer.mjs /path/viewer && BUILT_ROOT=/path/viewer CAP_MS=45000 node bench/sar/startup.mjs built 3
node bench/sar/throttled.mjs merged sentinel1 100 40; node bench/sar/throttled.mjs merged nisar 800 50
```

Local stores, one machine, Chromium with Metal; not a CDN. The constrained-link rows use CDP throttling after the page loaded.
