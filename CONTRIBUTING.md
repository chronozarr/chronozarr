# Contributing

## Checks

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run these
commands from the checkout. `.python-version` selects Python 3.13; the package
continues to support Python 3.11 and newer. The default `dev` dependency group
installs the test and lint tools. Add development tools with `uv add --dev`.

```bash
uv sync --locked --extra geo --extra netcdf --extra dask
uv run python scripts/check_architecture.py
uv run coverage run -m pytest -q -m unit
uv run coverage report
uv run coverage json
uv run coverage xml
npm ci --prefix js
npm run test:coverage --prefix js
npm run test:browser --prefix js
```

The architecture checker and `sentrux check .` share `.sentrux/rules.toml`. First-party imports must be acyclic. Shared writer and store modules must not depend on their callers. MapLibre and shared rendering must not depend on the demo.

The dependency-free checker runs in CI. It includes deferred Python imports and static JavaScript imports and re-exports. Computed runtime imports are outside its scope.

Python coverage measures every package module. It writes JSON and XML to `data/reports/coverage/python/`.

Native Node coverage measures only modules loaded by the Node tests under `chronozarr`, `shared`, `maplibre` and `demo`. It does not measure the browser-only viewer or GPU execution. Browser tests verify those behaviors separately.

CI retains both coverage reports for 14 days.

The Speed gates workflow measures the viewer of a pull request that changes `js/` against the viewer of its base, on the same runner, and fails when the head is slower than the tolerances in `js/support/gate-bench/compare.js` allow or when a warm switch uses the network. Its step summary gives both builds' figures. The same before and after check, locally (`/path/to/base` is a checkout of the base commit with `npm ci` run in its `js/`):

```bash
node /path/to/base/js/support/build-viewer.mjs /tmp/gates/viewers/base
node js/support/build-viewer.mjs /tmp/gates/viewers/head
node js/support/gate-bench/store.mjs /tmp/gates/store
node js/support/gate-bench/run.mjs --base /tmp/gates/viewers/base --head /tmp/gates/viewers/head --store /tmp/gates/store
```

The runner alternates the two builds in pairs of runs, each a new headless Chromium with software WebGL, and compares each pair on its own. The store is 36 cells at level 0. `--gpu metal` draws on the GPU of a Mac instead. Timings on a machine that other work loads are noise: check `uptime` first. Local and loopback results do not measure CDN performance.

`--cache compact` measures the cold open and the warm switches with the cache that the public demo store gets. The cache policy (`js/demo/cache-policy.js`) packs every chunk of a `uint16` store in memory when the loop of level 0 is more than 0.9 of the cache cap, which the demo's 8.4 GiB loop against 1.5 GiB is and the gate store's 576 MiB is not, so the default pass never runs that path. The compact pass opens the same store with a 560 MiB cap, which the loop exceeds and the packed loop (72 % of it) fits, and fails if the cache was not packed or evicted a chunk (an eviction would make the warm switches fetch, and read as the network). The default pass only warns, and says so in the summary, if a change to the cache policy makes it pack the gate store. Decode is not measured again: it is the same code. The workflow does not run it: a compact run took 12 s on a 16-core Mac with software WebGL (95 s for `--pairs 3` and the warm-up pair), the runner is slower, and the 12 runs of the default pass already take 15 minutes, so the pass would likely add more than 3 minutes. Run it for a change to packing or to the compact shader:

```bash
node js/support/gate-bench/run.mjs --cache compact --base /tmp/gates/viewers/base --head /tmp/gates/viewers/head --store /tmp/gates/store
```

The gates measure a page whose workers, shader programs and connections are warm from the opens before. Before each counted run of the default pass, the runner also makes a first visit in a new browser with an empty profile (`first-visit.mjs`; the local server sends no cache headers, so the page and each worker download the codec themselves), and the summary lists its marks next to the gates: the start of the navigation to the first chunk request, the time spent in shader compile and link, a decode worker's spawn to its first message, the codec module load, and the navigation to the first whole frame and to the frame at the target level. They are for information: no gate reads them, and a visit that fails is logged without failing the job. A change to what happens before the first chunk is requested shows there and not in the gates.

A local run of either runner refuses to start when the 1-minute load average is above half the logical cores (the runner's own Chromium is part of the load) or the Mac is on battery power (`pmset`), which caps animation frames at 30 Hz. It also discards a run, up to three times in a row before it stops, when the median animation frame interval of its browser, measured on a blank page before and after the run, is above 18 ms; a faster display passes. The discards are in the JSON. `--ignore-guards` measures anyway, and the figures are then noise. CI is not guarded: a shared runner has no battery and a load that the job cannot lower, and the pairs put both builds under it. The summary and the JSON say whether the guards were on, and the load at the start and the end is in the summary either way.

The gates cannot see what a slow link does to the viewer: they measure on an unthrottled link with every chunk in memory. `js/support/gate-bench/link.mjs` measures the same two builds on throttled links, for changes to what the viewer fetches and what it shows while it waits. It is not a CI job. It needs `openssl`, and a store with more timesteps than the viewer holds at once, which `store.mjs --times` makes by hard-linking the chunk files of the 8 generated timesteps (the same 510 MB on disk):

```bash
node js/support/gate-bench/store.mjs /tmp/gates/store-long --times 48
node js/support/gate-bench/link.mjs --base /tmp/gates/viewers/base --head /tmp/gates/viewers/head --store /tmp/gates/store-long --out /tmp/gates/link.json
```

Both builds and the store are served over HTTP/2. A run throttles the link with CDP (50 Mbit/s with 40 ms, and 8 Mbit/s with 80 ms; `--links`) before it loads the page, so the viewer, its workers and its first frame arrive over the link (the local server does not compress, so they cost more bytes than from a CDN). The viewer then idles for 5 s, and the run records what the reader holds at that point (`detail.start`), because runs that start from different states are not comparable. One pair of the first scenario is run first and not counted, and the raw page results are written to `--out` as each run ends, so a failure late in the job keeps what came before it. The scenario (`--scenarios`) is one of: a drag of the timeline over the whole time axis at 40 or 150 ms per step, a press half the axis away, or Play at 4 or 30 steps/s for 30 s (`--play-seconds`). It reports the frames drawn at the target level and at a coarser one (a repaint of the frame on screen is not a frame), the time the frame on screen was older than the timestep asked for, the timesteps never shown, the delay from each input, counted from when it was due, to a frame, the time to the first whole frame and to the target level after a jump, the steps, rate and buffering pauses of playback, and the requests and wire bytes of each. The summary flags a metric only when the head differs from the base beyond a tolerance and a floor; those are provisional, so run `--base` and `--head` on the same build once to see a metric's noise before reading a difference. `--gpu metal` (the default) draws on the GPU of a Mac; use `--gpu swiftshader` elsewhere. Three pairs of the five scenarios on both links take about 30 minutes (a run is 12 to 55 s: the open on the 8 Mbit/s link alone is 10 s, and the 30 s of playback is most of the rest); `--pairs`, `--links` and `--scenarios` shorten that.
