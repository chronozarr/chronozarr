# chronozarr

[![CI](https://github.com/chronozarr/chronozarr/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/chronozarr/chronozarr/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/chronozarr)](https://pypi.org/project/chronozarr/)
[![npm](https://img.shields.io/npm/v/chronozarr)](https://www.npmjs.com/package/chronozarr)

chronozarr turns a georeferenced raster time series into an interactive dataset that anyone can open with a link. In a web browser, the series plays or scrubs like a video. You zoom and pan the map, and a click on a pixel shows its values over time. In Python, the same files return the exact numbers you stored. No tile server runs anywhere.

[Live demo](https://chronozarr.org/demo/): 117 monthly Sentinel-2 composites of the Ucayali River in Peru, read directly from a storage bucket. Documentation: [chronozarr.org](https://chronozarr.org).

## Quickstart

This quickstart uses the released **0.4.1** packages. The [candidate guides](docs/development.md) describe the local, unpublished **0.5.0rc1** Python / **0.5.0-rc.1** npm candidate; use the prepared checkout or local artifacts for new converter options, custom CRSs and the production viewer bundle. The [0.4.1 documentation](https://github.com/chronozarr/chronozarr/tree/v0.4.1/docs) stays with the release.

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then the `chronozarr` command:

```bash
uv tool install --python 3.13 'chronozarr[geo]==0.4.1'
```

chronozarr supports Python 3.11 and newer. `--python 3.13` makes uv use 3.13 even when an older Python comes first on your computer. With pip, in an environment with Python 3.11 or newer:

```bash
python -m pip install 'chronozarr[geo]==0.4.1'
```

Use a folder with one GeoTIFF per date, with the date in each file name, such as `ndvi_2024-01.tif`. All files share one grid. To try it without your own files, export twelve months of the public demo as GeoTIFFs:

```bash
chronozarr export-cog https://data.chronozarr.org/ucayali_santa_maria_v03 sample --level 2 --times 2024-01..2024-12
```

Then convert the folder and share it:

```bash
chronozarr convert sample my_store --dry-run
chronozarr convert sample my_store
chronozarr share my_store      # a temporary public link that any computer can open
chronozarr preview my_store    # or: private, opens on this computer only
```

The dry run checks every file and writes nothing. `convert` writes the store. `share` prints a temporary link that you or a colleague open in a browser. It needs [`cloudflared`](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/), works only while the command runs and your computer is on, and anyone with the link can read the store. `preview` opens the store in your browser through a local server. Its link opens only on this computer.

The [full quickstart](https://chronozarr.org/quickstart) has a sample dataset, the expected output of each command and the pip route.

## Share a store

| Link | Command | Lasts | Needs |
|---|---|---|---|
| Temporary | `chronozarr share my_store` | While the command runs | `cloudflared`, your computer on and online |
| Permanent | `chronozarr publish my_store --destination s3://BUCKET/PREFIX` | As long as you keep the files | Cloud storage that you control: S3, R2, Google Cloud Storage or Azure |

The recipient needs only a web browser. chronozarr.org serves the viewer page and stores no data. See [Share a temporary link](https://chronozarr.org/guides/share) and [Publish a permanent link](https://chronozarr.org/guides/publish).

## Read the values

```python
import xarray as xr

ds = xr.open_dataset("my_store", engine="chronozarr")
```

A store is a Zarr v3 group, so xarray, zarr-python and GDAL 3.13 also read it without chronozarr. They return the stored values: plain xarray and CarbonPlan's zarr-layer 0.11.0 do not apply its band scale and offset or its mask ([GeoZarr review](https://github.com/chronozarr/chronozarr/blob/main/spec/review-2026-10-geozarr.md)). See [Analyze a store in Python](https://chronozarr.org/guides/analyze).

## JavaScript

```bash
npm install chronozarr@0.4.1
```

The npm 0.4.1 package has a DOM-free reader, a MapLibre layer, a GeoLibre plugin and a module-based viewer for self-hosting. The [development viewer bundle](docs/viewer-distribution.md) is built from a checkout. See the [released JavaScript reader](https://github.com/chronozarr/chronozarr/blob/v0.4.1/js/README.md).

## Versions

The stable install examples above select Python and npm package 0.4.1, which implements the draft v0.3.0 format. This checkout prepares Python 0.5.0rc1 and npm 0.5.0-rc.1 locally; neither candidate has been published, tagged or deployed. The writer keeps EPSG stores at spec v0.3.0 and uses draft v0.4.0 for other CRSs. The candidate readers open both; a released 0.4.1 reader cannot open a v0.4.0 store. Use the [candidate installation](docs/development.md) for those stores. Existing v0.3 stores stay readable. To use a v0.2 store, convert it with `chronozarr convert OLD_STORE NEW_STORE`. See the [candidate release notes](docs/release-0.5.0rc1.md) and [released 0.4.1 notes](https://github.com/chronozarr/chronozarr/blob/v0.4.1/docs/release-0.4.1.md).

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

The pages are written in [`docs/`](https://github.com/chronozarr/chronozarr/tree/main/docs) for chronozarr.org. Candidate guides are labeled with their local version and publication status; use the [tagged 0.4.1 docs](https://github.com/chronozarr/chronozarr/tree/v0.4.1/docs) with that release. Changing these files does not deploy the site or its viewer. To contribute, see [CONTRIBUTING.md](https://github.com/chronozarr/chronozarr/blob/main/CONTRIBUTING.md).

## License

See [LICENSE](https://github.com/chronozarr/chronozarr/blob/main/LICENSE).
