# From rasters to an interactive map you can share

[![CI](https://github.com/chronozarr/chronozarr/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/chronozarr/chronozarr/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/chronozarr)](https://pypi.org/project/chronozarr/)
[![npm](https://img.shields.io/npm/v/chronozarr)](https://www.npmjs.com/package/chronozarr)

chronozarr turns dated GeoTIFFs into a time-series viewer. Explore it on your computer, send a temporary link from your laptop, or publish it to cloud storage. Anyone with the link plays or scrubs through the series in a web browser like a video. A click on a pixel shows its values over time. The same files open in Python with the exact values you wrote.

[Live demo](https://chronozarr.org/demo/): 117 monthly Sentinel-2 composites (2015 to 2026) of the Ucayali River in Peru, read directly from a bucket.

## Quickstart

Four commands take a folder of dated GeoTIFFs to a link.

1. Install. It needs Python 3.11 or later.

   ```bash
   python -m pip install --upgrade "chronozarr[geo]==0.4.0"
   ```

2. Convert your rasters. Each file name holds one date: `20240131`, `2024-01-31` or `2024-01`.

   ```bash
   chronozarr convert "rasters/*.tif" my_store
   ```

3. Explore on your computer.

   ```bash
   chronozarr preview my_store
   ```

4. Share a link.

   ```bash
   chronozarr share my_store
   ```

`share` needs [`cloudflared`](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/). It needs no bucket and no Cloudflare account. Keep the terminal open while others use the link. Anyone with the link can read the store while it runs.

To check the files first, add `--dry-run` to the `convert` command. It reports every problem per file and writes nothing. See [Convert your data](docs/convert.md).

## Choose how to share

| I want to | Use |
|-----------|-----|
| Look at a store on my computer | `chronozarr preview`. See [Preview a store](docs/preview.md) |
| Send someone a temporary link | `chronozarr share`. See [Share a local store instantly](docs/share.md) |
| Make a link that lasts | `chronozarr publish`. See [Publish a store](docs/publish.md#chronozarr-publish) |
| Show a store in my own web page | [Embed the viewer](docs/embedding.md) |
| Keep the data private | [Private stores](docs/private.md) |
| Work in Jupyter or VS Code | [Explore a store in a notebook](docs/notebooks.md) |

## What you can do with a store

| Task | How |
|------|-----|
| Convert a folder, glob or S3 prefix of dated GeoTIFFs | `chronozarr convert`, with date discovery and a dry run. See [Convert your data](docs/convert.md) |
| Check a store, or a host | `chronozarr validate` and `chronozarr doctor` |
| Add new timesteps | `chronozarr append`, then `chronozarr publish --update`. See [Append timesteps](docs/append.md) |
| Inspect a pixel, switch to NDVI, NDWI or true color | The viewer computes products from the stored bands. See [Band roles](docs/python.md#band-roles-and-the-first-view) |
| Send the exact view | `Copy link` in the viewer, or `chronozarr link`. The link holds time, product, zoom and centre |
| Analyze in Python | `xr.open_dataset("my_store", engine="chronozarr")`. See [Python API](docs/python.md) |
| Open in GDAL or QGIS | `chronozarr export-cog`. See [How it works](docs/how-it-works.md#read-a-store-without-chronozarr) |

## JavaScript

```bash
npm install chronozarr@0.4.0
```

The package has four parts:

- `chronozarr` is a DOM-free reader. It returns one cell of one timestep as a typed array.
- `chronozarr/maplibre` is a MapLibre custom layer that draws a store.
- `chronozarr/geolibre` is a GeoLibre plugin that adds a store as a MapLibre layer.
- `chronozarr-viewer` copies the full viewer into a folder that you can host next to your store. See [docs/viewer-distribution.md](docs/viewer-distribution.md).

There is no build step and no runtime dependency. Examples are in [js/README.md](js/README.md).

## Documentation

| Topic | Document |
|-------|----------|
| How a store is laid out, and the first store from Python | [How it works](docs/how-it-works.md) |
| Every command | [Command line](docs/cli.md) |
| Python functions | [docs/python.md](docs/python.md) |
| Your own data, end to end | [examples/bring_your_data](examples/bring_your_data/README.md) |
| Georeferenced PNG frames | [docs/png-frames.md](docs/png-frames.md) |
| Hosting and the doctor checklist | [docs/hosting.md](docs/hosting.md) |
| Embedding the viewer in a page | [docs/embedding.md](docs/embedding.md) |
| Drawing a store on a MapLibre map | [js/maplibre/README.md](js/maplibre/README.md) |
| More examples: Sentinel-2 ingest, water masks, notebooks, SWOT, NISAR | [examples](examples/) |
| Format rules | [spec/CHRONOZARR.md](spec/CHRONOZARR.md) |
| Comparison with PMTiles, Mapbox raster-array and zarr-layer | [docs/format-comparison.md](docs/format-comparison.md) |
| Measurements and tested reader versions | [docs/evidence.md](docs/evidence.md) |

## Status

The released Python and npm packages are 0.4.0. The spec remains v0.3.0, a draft: package upgrades do not change existing v0.3 stores, and readers open v0.3 stores. To use a v0.2 store, convert it:

```bash
chronozarr convert OLD_STORE NEW_STORE
```

## License

See [LICENSE](LICENSE).
