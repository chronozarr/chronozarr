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

For a before and after speed check on the same local fixture:

```bash
node scripts/audit_browser.mjs js data/spike/stress6x6 data/reports/browser-bench.json
```

The script uses headless Chromium with software WebGL, three cold runs and 20 switches. Its additional `warmFullyLoaded` measurement primes all timesteps with a 2 GiB cache, because the ordinary idle prefetch intentionally stops at 64 MiB. Inspect complete frames and bytes alongside timings. Local and loopback results do not measure CDN performance.
