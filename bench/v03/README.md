# v0.3 browser delivery benchmark

Measures what a browser pays to show one Sentinel-2 monthly time series from three delivery paths, on the same imagery
and the same machine, with the same view and the same interactions.

| id | system | data it reads |
|---|---|---|
| A | the chronozarr viewer (`js/demo`, this repository) | the v0.3 store |
| B | CarbonPlan zarr-layer 0.10.0 on MapLibre GL 6.11.2, unmodified, `zarrVersion: 3` only | the same v0.3 store |
| C | MapLibre raster tiles from TiTiler 2.4.0 (official image, pinned by digest) | one COG per date, written from the store with `chronozarr export-cog --level 0` |

Data: the Ucayali store `ucayali_santa_maria_v03` (117 monthly timesteps, 4 bands B02/B03/B04/B08 as uint16, UTM 18S,
level 0 = 2759 x 2765 px at 10 m, unsharded, zstd 5, 512 px chunks, 5,893 objects, 6.45 GB). It is published at
`https://data.chronozarr.org/ucayali_santa_maria_v03`; the benchmark reads a local copy so that the link is the one
the profiles define. The copy used for the first results matched the published root `zarr.json`, every other
`zarr.json` and 40 random chunks byte for byte (`sha256`). This repository has no download script for it.

## One command

From the repository root, once (setup):

```bash
cd bench && npm ci && cd ..          # playwright 1.63.0, maplibre-gl, zarr-layer, esbuild: from bench/package-lock.json
uv sync --extra dev --extra geo      # rasterio for export-cog and the reference read
# Playwright's Chromium (about 170 MB): `cd bench && npx playwright install chromium` if it is not installed yet
# Docker Desktop running; the store at data/stores/ucayali_santa_maria_v03 (or BENCH_DATA=<dir that holds stores/...>)
```

Then everything (values check, then 5 rounds of 3 profiles x 4 views x 3 systems, tables; `zoom-matched` runs system C only):

```bash
node bench/v03/run.mjs
```

It prepares what is missing: the 117 COGs (`data/bench/v03/cogs`, 6.2 GB, about 10 minutes), the zarr-layer bundle, and the
TiTiler image (pulled by digest). It takes several hours: the throttled profiles are slow by design, and a session whose
animation-frame rate was not nominal is repeated. Output is `data/bench/v03/results/<timestamp>/` (gitignored):

| file | content |
|---|---|
| `environment.json` | machine, OS, browser build and GPU renderer, Node, Python, GDAL, library versions, Docker, TiTiler image tag/digest/settings, git commit, store and COG facts |
| `values.json` | the pixel-value check (below) |
| `rounds.jsonl` | one line per round: time, load average, the busiest processes |
| `runs.jsonl` | every session attempt, appended as it finishes (kept and discarded) |
| `results.json` | the kept sessions, the discards, the round log |
| `summary.md`, `summary.json` | tables and per-round numbers (`summarize.mjs`) |

Options (all optional): `--rounds 5`, `--profiles natural,50Mbit-40ms,10Mbit-100ms`,
`--views overview,zoom,overview-matched,zoom-matched`, `--systems A,B,C`, `--steps 30`, `--attempts 3`,
`--titiler-workers 4`, `--no-values`, `--no-warm-files`, `--out <dir>`, `--resume` (with `--out`: continue an interrupted
run, skipping the finished cells). `node bench/v03/summarize.mjs <results.json>` prints the tables again.
`node bench/v03/values.mjs` runs the value check alone. `cd bench && npm test` runs the unit tests (statistics, view
geometry, block detection, table assembly).

## Method

**Browser.** Playwright's full Chromium build (`channel: 'chromium'`, new headless) with `--ignore-gpu-blocklist
--use-angle=metal`: the machine's GPU, not a software renderer (the run refuses to start if WebGL2 reports one). Window
1440 x 900 CSS px at device pixel ratio 2 (canvas 2880 x 1800). A fresh browser, context and empty cache for every
session; the HTTP cache is disabled over CDP so that bytes are network bytes. Never the Claude app browser pane. Playwright
artifacts and browser profiles go to a private temporary directory (`private-tmp.mjs`).

**Servers.** Two range-capable static servers from `js/support/static-server.js` (HTTP/1.1, CORS `*`): one for the pages
and scripts, one for the data, so data requests are cross-origin as from a CDN. The data server holds the store and the
COGs. TiTiler runs in a container started cold per session (a fresh container for every C session), published on
127.0.0.1, reading the COGs from the data server over HTTP range requests (`host.docker.internal`). Before each C session
three waves of 16 concurrent warm-up tile requests (4 per worker) read COGs of dates the session never shows, so Python, GDAL and
the gunicorn workers have started. The OS file cache is warmed by reading every file once at the start of each round.

**Throttling.** Chrome DevTools `Network.emulateNetworkConditions` on the page, applied after the page and its scripts
have loaded, so only data requests are throttled: `natural` (unthrottled), `50Mbit-40ms`, `10Mbit-100ms` (download and
upload throughput, latency per request). The link between TiTiler and the data server is not throttled (a tile server sits
next to its storage).

All code is loaded before the link is throttled, as for a user who has the page open: the page, the zarr-layer bundle
and MapLibre (B and C), and for A the decode workers and their zstd WASM. The viewer starts its workers when a store
opens, and with the HTTP cache off each of the 8 fetches its own copy, so a first version of this harness charged A about
4 s of script downloads inside its 10 Mbit/s open. A driver now opens and closes the store once before throttling
(`prewarm` in `driver-a.js`; the reader keeps the worker pool for 30 s) and the viewer's own open reuses the pool.

**Views.** Both centred on the AOI and drawn at the same ground scale in all three systems (the viewer in UTM, MapLibre in
Web Mercator):

- `overview`: the whole AOI fits the window height.
- `zoom`: a 993 x 621 level-0 pixel window centred on the corner shared by chunks (2,2), (2,3), (3,2), (3,3): four 512 px
  chunks of level 0 and nothing else.

Each system chooses its own level of detail, and they differ (the first run's tables show it in the "pixel size" column).
`overview-matched` and `zoom-matched` repeat a view with the systems that can be held to a pixel size held to it: A on
level 2 and C on tiles of at most zoom 11 in the overview (about 40 m, what zarr-layer chooses by itself), C on tiles of at
most zoom 13 (9.5 m) in the zoom view. zarr-layer has no option to pin its level.

**Script of a session** (the same for all systems, `session.mjs`):

1. `open`: the call that opens the data, to the first complete frame at timestep 10.
2. `step`: 30 steps forward (timesteps 11 to 40), one input, wait for the complete frame, think 400 ms, again. A step goes
   to a date the session has not shown. The 400 ms is longer than the viewer's 300 ms scrub window, so a step is a tap and not a scrub.
3. `revisit`: the same 30 dates back again (timesteps 39 to 10), dates already shown.
4. `jump`: one input to timestep 60, 50 dates from where the revisit ended.
5. `idle`: 3 s with nothing asked, to count what a system keeps fetching.

**What "complete frame" means.** Each system's own rule, then the same end: the GPU has finished the frame.
A: the viewer's `paint` event with `complete` (every visible cell at the level it wants for the view, one timestep, never a
mixture). B: a MapLibre render after which every visible region of zarr-layer's active level holds the selector of the
step. C: a second raster source for the new date is added hidden, becomes visible when all its tiles are loaded, and the
old one is removed, so no blank or mixed frame (what a careful MapLibre page does). Then a one-pixel `readPixels` on the
canvas's WebGL context, which cannot return before the GPU is done. The compositor's presentation at the next vsync is
not included for any system. The viewer's first whole frame at any level (`coarseMs`, `firstWholeMs`) is reported next to it.

**Bytes and requests.** From CDP events of the page for the origin the browser reads from (data server for A and B,
TiTiler for C): `encodedDataLength` of each finished request (headers and body), and the body bytes received for an
aborted one. For C the table also reports what TiTiler read from the COGs (server side). Per phase: the requests and bytes
that began between its start and its end. The bytes "per step" are the whole step phase divided by the number of steps, so
a viewer's speculative fetching counts. The reader's own counters (A) are stored to cross-check.

**Validity of a session.** The median gap between animation frames of the idle page, measured for 1.5 s before the session
and 1.5 s after it, must be within 15.5 to 18 ms (60 Hz is 16.7 ms). Otherwise the session is discarded, kept in
`runs.jsonl` with the reason, and repeated (up to `--attempts`). Sessions that fail are repeated the same way.

**Rounds.** Every round runs every profile x view x system once; the order of the systems rotates by round (A,B,C; B,C,A;
C,A,B; ...). Tables give the median over rounds and the range of the rounds. The load average and the busiest
processes are recorded at every round start and the 1-minute load average at every session start. **The machine is
shared, so numbers are provisional**: they show orderings and orders of magnitude, not exact values.

**Values check (`values.mjs`).** Reference values come from zarr-python on the store and rasterio on each date's COG, with
no browser and no chronozarr reader. (1) Reader level: six level-0 pixels at three dates, all four bands, read
through A's JS reader, B's `queryData` at its finest level and C's `/cog/point`: 18 pixel-date samples x 5 sources.
(2) Displayed: three pixels (one in each of cells (2,2), (3,3), (2,3), each with neighbours of different colour) in the
zoom view at the screen position each system's projection gives for the pixel centre. A: the viewer's click-to-inspect reports the
stored values there. B and C: the canvas is read back and the uniform block of the pixel must have the colour the system's
own transfer function gives for the pixel's values (B: `clamp(v/3000)` with gamma 1/2.2 within 2/255; C: TiTiler's linear 0..3000
rescale within 1/255), and its middle must lie within half a level-0 pixel of the projected centre. Colours of A are tone-mapped with a measured stretch and are
not compared; its block position is.

## What it does not show

- One machine (Apple M3 Max, macOS), one Chromium build, one store, localhost servers over HTTP/1.1 (at most 6
  connections per origin for every system; a CDN with HTTP/2 or HTTP/3 helps the system that needs the most requests per
  view, C most). No CDN misses, TLS, slow start, or real-link jitter. TiTiler and the browser share this machine's CPU.
- C ships rendered 8-bit PNG tiles at the screen's resolution; A and B ship the true stored values (uint16, four bands) at
  source resolution. The bytes and latencies are for different products: only A and B let the page do band math or read
  true values, and C's tiles at a view with fewer pixels than the data are smaller than the data they come from.
- TiTiler's configuration is the documented recommendation with 4 workers (the image default is 1); other settings move C.
  Its COGs are the exporter's (DEFLATE with predictor, 512 px tiles, overviews 2/4/8), not the best possible COGs.
- A's speculative prefetch is part of what is measured (it is why warm steps are fast and why bytes per step exceed what
  the view needs). B and C fetch only what is asked.
- The HTTP cache is off for all systems, so C's revisit re-requests tiles; with caching on a browser would reuse them.
- Timing includes the GPU's completion of the frame, not the compositor's presentation. A level-0 pixel is drawn as about
  3 device pixels at the zoom view, so the displayed check can tell a one-pixel misplacement from a correct placement but
  not sub-pixel resampling differences.
