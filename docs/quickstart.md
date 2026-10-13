# Quickstart

This page takes a folder of dated GeoTIFFs to an interactive viewer on your computer, and then to a link that a colleague can open. You need [uv](https://docs.astral.sh/uv/getting-started/installation/) and a web browser. You need no account, no cloud storage and no server.

## 1. Install

```bash
uv tool install --python 3.13 'chronozarr[geo]==0.4.0'
chronozarr --version
```

The first command puts the `chronozarr` command on your `PATH`. `--python 3.13` makes uv use Python 3.13 even when an older Python comes first on your computer. uv downloads Python 3.13 if it is not installed. The second command prints `chronozarr, version 0.4.0`. On macOS the first command after an install can take 10 to 25 seconds while the system checks the new files; later commands start in about a second.

The `geo` extra adds GeoTIFF support. To install with pip instead, use an environment with Python 3.11 or later:

```bash
python -m pip install 'chronozarr[geo]==0.4.0'
```

To use chronozarr from Python in a uv project, run `uv add 'chronozarr[geo]==0.4.0'` in the project. Then put `uv run` in front of each command on this page.

## 2. Get a dated raster series

Use one GeoTIFF for each timestep, with one date in each file name:

```text
rasters/
  ndvi_2024-01.tif
  ndvi_2024-02.tif
  ndvi_2024-03.tif
```

A date can be written `20240131`, `2024-01-31` or `2024-01`. All files must share one north-up grid: the same coordinate system, size, pixel size and position. They must also have the same bands, data type, scale, offset, units and nodata value.

If you have no files at hand, export twelve months of the public demo as GeoTIFFs:

```bash
chronozarr export-cog https://data.chronozarr.org/ucayali_santa_maria_v03 sample --level 2 --times 2024-01..2024-12
```

This downloads about 40 MB and writes twelve files, `sample/L2_2024-01-01.tif` to `sample/L2_2024-12-01.tif`. Each has four Sentinel-2 bands at 40 m. The rest of this page uses `sample`. Replace it with your own folder.

## 3. Convert

Check the files first. A dry run reads every file header, prints the plan and writes nothing:

```bash
chronozarr convert sample my_store --dry-run
```

```text
files:      12, dated from their names
  2024-01-01  sample/L2_2024-01-01.tif
  ...
  2024-12-01  sample/L2_2024-12-01.tif
grid:       EPSG:32718, 692 x 690 px, 40 m pixels (transform origin 485650, 9169880)
data:       4 bands (B02, B03, B04, B08), uint16
scaling:    B02 = stored * 0.0001 +0 [reflectance], B03 = ..., B08 = stored * 0.0001 +0 [reflectance]
validity:   nodata 0 sentinel, no mask
resampling: none needed, every source is on the target grid
raw size:   0.05 GB (4 MB per timestep; memory about 11 MB while staging)
output:     about 0.04 GB in 2 levels (zstd 5 ratio 0.70 on 3 sampled cells)
dry run: nothing was written
```

Check the dates, the grid, the bands and the scaling. They become the store.

If a file has no date, another grid or other band metadata, the dry run lists every problem by file, with a fix for each. See [Convert your data](convert.md) and [Troubleshooting](troubleshooting.md#conversion).

Then convert:

```bash
chronozarr convert sample my_store
```

```text
wrote my_store: 2 levels, 81 files, 40.2 MB, zstd level 5
read 12 timesteps (0 reused) in 0.2 s, encoded in 0.3 s, total 0.5 s
preview it: chronozarr preview my_store
```

`my_store` is a folder of static files. It holds your values unchanged, plus a coarser copy for zoomed-out views.

## 4. Look at it on your computer

```bash
chronozarr preview my_store
```

```text
serving my_store at http://127.0.0.1:56925/my_store (byte ranges and CORS, 127.0.0.1 only)
viewer: https://chronozarr.org/demo/ (loaded from the internet; the store itself is read from this machine).
open: https://chronozarr.org/demo/?store=http%3A%2F%2F127.0.0.1%3A56925%2Fmy_store
press Ctrl-C to stop
```

Your browser opens the viewer. The viewer page comes from chronozarr.org. The store is read from your computer and is not uploaded.

Try these in the viewer:

- Press Space to play the series. Press the Left and Right arrow keys to step.
- Choose a product, such as NDVI or False color.
- Click a pixel to see its values over time.

The [viewer guide](viewer.md) lists every control. Press Ctrl-C in the terminal to stop the preview. Use Chrome or Firefox: Safari can block a page on the internet from reading `127.0.0.1`.

## 5. Share a temporary link

`share` makes your store reachable through a temporary public address. It needs [`cloudflared`](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/), a free program from Cloudflare. It needs no Cloudflare account. On macOS, install it with `brew install cloudflared`.

```bash
chronozarr share my_store
```

The command prints its progress and then a viewer link. A new public address usually takes about a minute to work, so the link appears after one to two minutes. In the viewer, choose the date, product and view that you want to show. Click Copy link and send the link. The recipient needs only a web browser.

Before you send the link, know these limits:

- The link works only while the command runs. Ctrl-C stops sharing, and the link stops working.
- Your computer must stay on and connected. Every view that the recipient loads is sent from your computer, over your upload connection.
- Anyone who has the link can read the store while it runs.

[Share a temporary link](share.md) has the details and the troubleshooting steps.

## 6. Publish a permanent link

For a link that keeps working when your computer is off, upload the store to cloud storage that you control: Amazon S3, Cloudflare R2, Google Cloud Storage or Azure. `chronozarr publish` uploads it, checks it and prints the viewer link. You pay your provider for storage and downloads. chronozarr.org does not store your data. See [Publish a permanent link](publish.md).

## 7. Read the values in Python

```python
import xarray as xr

ds = xr.open_dataset("my_store", engine="chronozarr")
ds["data"].sel(band="B08").isel(time=0, x=100, y=80).item()
```

The dataset holds the values that you converted, with the scale applied. The `chronozarr` command from step 1 lives in its own environment, so install the package where your notebook runs: `pip install chronozarr` or `uv add chronozarr`. See [Analyze a store in Python](analyze.md).

## Next steps

| I want to | Page |
|---|---|
| Convert Zarr, NetCDF, COGs on S3 or PNG frames | [Convert your data](convert.md) |
| Build a Sentinel-2 series for my own area | [Sentinel-2 time series](sentinel2.md) |
| Add new timesteps later | [Append timesteps](append.md) |
| Show the viewer in my own web page | [Embed the viewer](embedding.md) |
| Work in Jupyter, on HPC or over SSH | [Notebooks and remote machines](notebooks.md) |
| Fix a problem | [Troubleshooting](troubleshooting.md) |
