# chronozarr

[![CI](https://github.com/chronozarr/chronozarr/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/chronozarr/chronozarr/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/chronozarr)](https://pypi.org/project/chronozarr/)
[![npm](https://img.shields.io/npm/v/chronozarr)](https://www.npmjs.com/package/chronozarr)

chronozarr lets you share a georeferenced raster time series as an interactive map. A recipient can open a link, play or scrub through the dates, zoom in and click a pixel to see its values over time. The browser reads the stored bands directly from static files, and Python can read the same values for analysis.

[Live demo](https://chronozarr.org/demo/): 117 monthly Sentinel-2 composites of the Ucayali River in Peru, read directly from a storage bucket. Documentation: [chronozarr.org](https://chronozarr.org).

## Quickstart

Use the sample below or [choose the guide for your input format](https://chronozarr.org/guides/convert).

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then the `chronozarr` command:

```bash
uv tool install --python 3.13 'chronozarr[geo]'
```

chronozarr supports Python 3.11 and newer. `--python 3.13` makes uv use 3.13 even when an older Python comes first on your computer. With pip, in an environment with Python 3.11 or newer:

```bash
python -m pip install 'chronozarr[geo]'
```

Export twelve months of the public demo as GeoTIFFs.

```bash
chronozarr export-cog https://data.chronozarr.org/ucayali_santa_maria_v03 sample --level 2 --times 2024-01..2024-12
```

Check and convert the folder:

```bash
chronozarr convert sample my_store --dry-run
chronozarr convert sample my_store
```

The dry run checks every file before `convert` writes the store. To use your own dated GeoTIFFs on one grid, replace `sample` with their folder.

Open the map on your computer.

```bash
chronozarr preview my_store
```

Press Space to play the series or click a pixel to see its values over time. Press Ctrl-C in the terminal when you finish.

The [full quickstart](https://chronozarr.org/quickstart) shows the expected output. If Chrome asks for local network access, allow it so the viewer can read your store.

## Share a store

| Link | Command | Lasts | Needs |
|---|---|---|---|
| Temporary | `chronozarr share my_store` | While the command runs | `cloudflared`, your computer on and online |
| Permanent | `chronozarr publish my_store --destination s3://BUCKET/PREFIX` | As long as you keep the files | Cloud storage that you control: S3, R2, Google Cloud Storage or Azure |

Anyone with the link can read the store. The recipient needs only a web browser. chronozarr.org serves the viewer page and stores no data. See [Share a temporary link](https://chronozarr.org/guides/share) and [Publish a permanent link](https://chronozarr.org/guides/publish).

## Read the values

The tool installation above installs the `chronozarr` command in its own environment. To read the data from Python, install the library in the environment used by your script or notebook.

Work in the folder that contains `my_store`. If that folder is not already a uv project, create one there:

```bash
uv init --bare --python 3.13
```

Then install the library in that project:

```bash
uv add chronozarr
```

Use `uv run python` to start Python in that folder, or `uv run python your_script.py` to run a script. The `"my_store"` path below is relative to that folder. To use an existing Python 3.11+ environment instead, install the library with:

```bash
python -m pip install chronozarr
```

A notebook must use that environment as its kernel. Then run:

```python
import xarray as xr

ds = xr.open_dataset("my_store", engine="chronozarr")
```

A store is a Zarr v3 group, so xarray, zarr-python and GDAL 3.13 also read it without chronozarr. They return the stored values: plain xarray and CarbonPlan's zarr-layer 0.11.0 do not apply its band scale and offset or its mask ([GeoZarr review](https://github.com/chronozarr/chronozarr/blob/main/spec/review-2026-10-geozarr.md)). See [Analyze a store in Python](https://chronozarr.org/guides/analyze).

## JavaScript

```bash
npm install chronozarr
```

The npm package has a DOM-free reader, a MapLibre layer, a GeoLibre plugin and the production viewer for [self-hosting](https://chronozarr.org/guides/viewer-distribution). See the [JavaScript reader](https://chronozarr.org/reference/javascript).

## Versions

Package versions and store format versions are independent. Packages 0.5.1 and later read both draft store formats. Stores with an EPSG code use **v0.3.0**. Stores without one use **v0.4.0**. To use a v0.2 store, convert it with `chronozarr convert OLD_STORE NEW_STORE`.

Use `chronozarr --version` for the installed package version. `chronozarr info my_store` reports the store's format version.

## Documentation

| Topic | Page |
|---|---|
| What chronozarr is, and how it compares | [What is chronozarr?](https://chronozarr.org/what-is-chronozarr) |
| Every input type | [Convert your data](https://chronozarr.org/guides/convert) |
| Icechunk and Tile-Ripper imports in 0.5.2 | [0.5.2 release notes in the source tree](https://github.com/chronozarr/chronozarr/blob/main/docs/release-0.5.2.md) |
| Sentinel-2 for your own area and dates (a repository checkout) | [Sentinel-2 for your area](https://chronozarr.org/guides/sentinel-2) |
| SWOT, NISAR and model output | [Examples](https://chronozarr.org/guides/examples) |
| Every command | [Command line](https://chronozarr.org/reference/cli) |
| Python functions | [Python API](https://chronozarr.org/reference/python) |
| Format rules | [Specification](https://chronozarr.org/specification) |
| Measured speed, memory and correctness | [Performance and evidence](https://chronozarr.org/performance) |
| Problems and fixes | [Troubleshooting](https://chronozarr.org/troubleshooting) |
| Instructions for coding agents, and these docs as plain text | [Agent skill](https://github.com/chronozarr/chronozarr/blob/main/skills/chronozarr/SKILL.md), [llms.txt](https://chronozarr.org/llms.txt) |

The source for chronozarr.org is in [`docs/`](https://github.com/chronozarr/chronozarr/tree/main/docs). Use the [source installation guide](https://chronozarr.org/guides/development) when developing from a checkout, or see [CONTRIBUTING.md](https://github.com/chronozarr/chronozarr/blob/main/CONTRIBUTING.md) to contribute. Packages and the hosted site are released separately.

## License

Apache-2.0. See [LICENSE](https://github.com/chronozarr/chronozarr/blob/main/LICENSE).
