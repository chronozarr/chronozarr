# chronozarr

[![CI](https://github.com/chronozarr/chronozarr/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/chronozarr/chronozarr/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/chronozarr)](https://pypi.org/project/chronozarr/)
[![npm](https://img.shields.io/npm/v/chronozarr)](https://www.npmjs.com/package/chronozarr)

chronozarr lets you share a georeferenced raster time series as an interactive map. A recipient can open a link, play or scrub through the dates, zoom in and click a pixel to see its values over time. The browser reads the stored bands directly from static files, and Python can read the same values for analysis.

[Live demo](https://chronozarr.org/demo/): 117 monthly Sentinel-2 composites of the Ucayali River in Peru, read directly from a storage bucket. Documentation: [chronozarr.org](https://chronozarr.org).

## Quickstart

This quickstart targets Python and npm **0.5.2**. The [release notes](https://chronozarr.org/reference/release-0.5.2) cover the viewer, preview and sharing fixes, new import routes, and compatibility with existing stores.

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then the `chronozarr` command:

```bash
uv tool install --python 3.13 'chronozarr[geo]==0.5.2'
```

chronozarr supports Python 3.11 and newer. `--python 3.13` makes uv use 3.13 even when an older Python comes first on your computer. With pip, in an environment with Python 3.11 or newer:

```bash
python -m pip install 'chronozarr[geo]==0.5.2'
```

Use a folder with one GeoTIFF per date, with the date in each file name, such as `ndvi_2024-01.tif`. All files share one grid. To try it without your own files, export twelve months of the public demo as GeoTIFFs:

```bash
chronozarr export-cog https://data.chronozarr.org/ucayali_santa_maria_v03 sample --level 2 --times 2024-01..2024-12
```

Check and convert the folder:

```bash
chronozarr convert sample my_store --dry-run
chronozarr convert sample my_store
```

The dry run checks every file before `convert` writes the store.

To send a temporary public link to a colleague, install [`cloudflared`](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/), then run:

```bash
chronozarr share my_store
```

Keep this command running and your computer on while the colleague uses the link. Anyone with the link can read the store. Press Ctrl-C to stop sharing before running another command in the same terminal.

For a view that opens only on your computer, use this instead. It needs no `cloudflared`:

```bash
chronozarr preview my_store
```

Preview also keeps running until you press Ctrl-C.

The [full quickstart](https://chronozarr.org/quickstart) has a sample dataset, the expected output of each command and the pip route.

For [Icechunk](https://github.com/chronozarr/chronozarr/blob/main/docs/icechunk.md), choose the variable, region and snapshot to convert.
The tested examples cover a materialized GFS window and virtual GRIB; converting
Float64 values requires an explicit choice to round to Float32.

The [Tile-Ripper Python bridge](https://github.com/chronozarr/chronozarr/blob/main/docs/tile-ripper.md) reads completed COG
observations, joins each scene to its selected quality mask and retains source
identities. It does not need an intermediate stacked store.

## Share a store

| Link | Command | Lasts | Needs |
|---|---|---|---|
| Temporary | `chronozarr share my_store` | While the command runs | `cloudflared`, your computer on and online |
| Permanent | `chronozarr publish my_store --destination s3://BUCKET/PREFIX` | As long as you keep the files | Cloud storage that you control: S3, R2, Google Cloud Storage or Azure |

The recipient needs only a web browser. chronozarr.org serves the viewer page and stores no data. See [Share a temporary link](https://chronozarr.org/guides/share) and [Publish a permanent link](https://chronozarr.org/guides/publish).

## Read the values

The tool installation above installs the `chronozarr` command in its own environment. To read the data from Python, install the library in the environment used by your script or notebook.

Work in the folder that contains `my_store`. If that folder is not already a uv project, create one there:

```bash
uv init --bare --python 3.13
```

Then install the library in that project:

```bash
uv add 'chronozarr==0.5.2'
```

Use `uv run python` to start Python in that folder, or `uv run python your_script.py` to run a script. The `"my_store"` path below is relative to that folder. To use an existing Python 3.11+ environment instead, install the library with:

```bash
python -m pip install 'chronozarr==0.5.2'
```

A notebook must use that environment as its kernel. Then run:

```python
import xarray as xr

ds = xr.open_dataset("my_store", engine="chronozarr")
```

A store is a Zarr v3 group, so xarray, zarr-python and GDAL 3.13 also read it without chronozarr. They return the stored values: plain xarray and CarbonPlan's zarr-layer 0.11.0 do not apply its band scale and offset or its mask ([GeoZarr review](https://github.com/chronozarr/chronozarr/blob/main/spec/review-2026-10-geozarr.md)). See [Analyze a store in Python](https://chronozarr.org/guides/analyze).

## JavaScript

```bash
npm install chronozarr@0.5.2
```

The npm package has a DOM-free reader, a MapLibre layer, a GeoLibre plugin and the production viewer for [self-hosting](https://chronozarr.org/guides/viewer-distribution). See the [JavaScript reader](https://chronozarr.org/reference/javascript).

## Versions

Python and npm package **0.5.2** read both draft store formats: **v0.3.0** for CRSs with an EPSG code and **v0.4.0** for other CRSs. Package versions and store specification versions are independent. Existing v0.3 stores need no conversion. Readers from package 0.4.1 and earlier cannot open v0.4 stores; upgrade the reader to 0.5.2. To use a v0.2 store, convert it with `chronozarr convert OLD_STORE NEW_STORE`. See the [0.5.2 release notes](https://chronozarr.org/reference/release-0.5.2).

Use `chronozarr --version` for the installed package version. `chronozarr info my_store` reports the store's format version.

## Documentation

| Topic | Page |
|---|---|
| What chronozarr is, and how it compares | [What is chronozarr?](https://chronozarr.org/what-is-chronozarr) |
| Every input type | [Convert your data](https://chronozarr.org/guides/convert) |
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
