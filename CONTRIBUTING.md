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
