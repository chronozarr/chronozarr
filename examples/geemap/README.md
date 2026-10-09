# chronozarr in geemap

`add_chronozarr` draws a chronozarr store on a `geemap.maplibregl.Map`, the same way it does on a leafmap map. The notebook opens the NISAR store of the [NISAR example](../nisar/README.md). Viewing a static store needs no Earth Engine account. Call the helper before you display the map.

## Run it

1. Install the extra with `uv sync --extra geemap`.
2. Build the NISAR store as the [NISAR example](../nisar/README.md) describes. It needs source files that are not in this repository.
3. Open `demo.ipynb` in a local notebook.

The arguments of `add_chronozarr` are the ones in the [leafmap example](../leafmap/README.md).

## Check it in a browser

```sh
uv run python examples/geemap/prepare_check.py
node examples/nisar/browser_check.mjs
```

`prepare_check.py` writes the geemap widget to `data/reports/nisar`, where `browser_check.mjs` reads it. The check overwrites the leafmap widget that `examples/nisar/prepare_check.py` writes there. The browser check needs Node.js and `npm ci --prefix js`. Add `--hv` to both commands to check the HV band.

The example runs only where the NISAR store exists. [`scripts/notebook_ci`](../../scripts/notebook_ci/prepare.py) checks geemap against a synthetic store.
