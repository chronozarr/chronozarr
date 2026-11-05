# Quickstart

Convert a raster time series and see it play in your browser. The sample uses a year of Sentinel-2 images of the Ucayali River in Peru.

## Choose your input

| What you have | Where to start |
|---|---|
| No files yet | [Start with the sample](#1-install) |
| A folder of GeoTIFFs, one per date | [Use your own GeoTIFFs](#use-your-own-geotiffs) |
| NetCDF or Zarr | [Convert NetCDF or Zarr](convert.md#from-netcdf-or-zarr) |
| An Earth Engine export | [Convert Earth Engine exports](convert.md#from-earth-engine-exports) |
| COG URLs, PNGs or another format | [See all supported inputs](convert.md) |

## 1. Install

**Package version — 0.5.2.** See the [release notes](release-0.5.2.md) or [install from source](development.md).

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run these commands in a terminal.

```bash
uv tool install --python 3.13 'chronozarr[geo]==0.5.2'
chronozarr --version
```

uv installs the `chronozarr` command on your `PATH` and uses Python 3.13, downloading it if needed. This avoids selecting an older Python already on your computer. The second command prints `chronozarr, version 0.5.2`. On macOS the first command after an install can take 10 to 25 seconds while the system checks the new files; later commands start in about a second.

chronozarr supports Python 3.11 and newer; this page uses 3.13. The `geo` extra adds GeoTIFF support. NetCDF files need the `netcdf` extra as well; see the [NetCDF guide](convert.md#from-netcdf-or-zarr). To install with pip instead, use an environment with Python 3.11 or newer, such as a virtual environment or a conda environment:

```bash
python -m pip install 'chronozarr[geo]==0.5.2'
```

If you already use a uv project, run `uv add 'chronozarr[geo]==0.5.2'` in the project. Then put `uv run` in front of each `chronozarr` command on this page.

## 2. Get a dated raster series

Export twelve months of the public demo as GeoTIFFs.

```bash
chronozarr export-cog https://data.chronozarr.org/ucayali_santa_maria_v03 sample --level 2 --times 2024-01..2024-12
```

This downloads about 40 MB and writes twelve files, `sample/L2_2024-01-01.tif` to `sample/L2_2024-12-01.tif`. The sample has four bands at 40 m resolution.

### Use your own GeoTIFFs

[Install chronozarr](#1-install), then use one GeoTIFF per date. Put the date in each file name.

```text
rasters/
  ndvi_2024-01.tif
  ndvi_2024-02.tif
  ndvi_2024-03.tif
```

A date can be written `20240131`, `2024-01-31` or `2024-01`. All files must share a north-up grid with the same coordinate system, size, pixel size and position. They must also have the same bands, data type, scale, offset, units and nodata value. The [overview defines common terms](what-is.md#terms-used-in-these-guides).

Replace `sample` with your own folder in step 3.

## 3. Convert

Check the files first. A dry run reads every file header, prints the plan and writes nothing:

```bash
chronozarr convert sample my_store --dry-run
```

```text
source:     dated GeoTIFFs, 12 timesteps (2024-01-01 .. 2024-12-01)
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
time:       read about 0.03 s per timestep (0.3 s sequential, less with --read-ahead), encode about 0.3 s
dry run: nothing was written
```

Check that the dates, grid, bands and scaling match your inputs before converting.

If a file has no date, another grid or other band metadata, the dry run lists every problem by file, with a fix for each. See the [conversion guide](convert.md) and [troubleshooting guide](troubleshooting.md#conversion).

Then convert:

```bash
chronozarr convert sample my_store
```

```text
wrote my_store: 2 levels, 81 files, 40.2 MB, zstd level 5
read 12 timesteps (0 reused) in 0.2 s, encoded in 0.3 s, total 0.5 s
...
```

`my_store` is a folder of static files. It holds your values unchanged, plus a coarser copy for zoomed-out views.

## 4. Look at it on your computer

Run `preview` to open your map in a browser. The viewer page loads from chronozarr.org and reads the store directly from your computer. Your data stays on your computer.

```bash
chronozarr preview my_store
```

If the browser stays closed, copy the address after `open:` in the terminal and open it yourself. Use Chrome or Firefox. If Chrome asks for local network access, allow it so the viewer can read your store.

Press Space to play the series or use the arrow keys to step through the dates. Click a pixel to see its values over time. With the sample, you can choose False color or NDVI in the product menu.

Press Ctrl-C in the terminal to stop the preview. See the [viewer controls](viewer.md#controls) and [preview troubleshooting](troubleshooting.md#preview).

## Next steps

### Share a temporary link

Install [`cloudflared`](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/) to give your store a temporary public address. On macOS, use `brew install cloudflared`.

Run `share` after stopping the preview.

```bash
chronozarr share my_store
```

The command opens the viewer when the link is ready, usually after about half a minute. Choose the date, product and map view you want to show. Click Copy link and send the link.

Anyone with the link can read the store while the command runs and your computer is on and online. Press Ctrl-C to stop sharing. See the [sharing guide](share.md) for details and troubleshooting.

### Publish a permanent link

For a link that keeps working when your computer is off, use storage in your own Amazon S3, Cloudflare R2, Google Cloud Storage or Azure account. `chronozarr publish` uploads the store, checks it and prints the viewer link. Storage, request and transfer charges depend on your provider and plan. chronozarr.org does not store your data. See [Publish a permanent link](publish.md).

### Read the values in Python

The `chronozarr` command from step 1 lives in its own environment. Install the library in the environment used by your script or notebook before running this code.

Stay in the folder where you ran `convert`, which contains `my_store`. If that folder is not already a uv project, create one there:

```bash
uv init --bare --python 3.13
```

Then install the library in that project:

```bash
uv add 'chronozarr==0.5.2'
```

Use `uv run python` to start Python in the same folder, or `uv run python your_script.py` to run a script. The `"my_store"` path below is relative to that folder. To use an existing Python 3.11+ environment instead, run `python -m pip install 'chronozarr==0.5.2'` there. A notebook must use that environment as its kernel.

```python
import xarray as xr

ds = xr.open_dataset("my_store", engine="chronozarr")
ds["data"].sel(band="B08").isel(time=0, x=100, y=80).item()
```

The dataset holds the values that you converted, with the scale applied. See [Analyze a store in Python](analyze.md).

### More guides

| I want to | Page |
|---|---|
| Develop from a local checkout | [Source installation](development.md) |
| Build a Sentinel-2 series for my own area | [Sentinel-2 time series](sentinel2.md) |
| Add new timesteps later | [Append timesteps](append.md) |
| Show the viewer in my own web page | [Embed the viewer](embedding.md) |
| Work in Jupyter, on HPC or over SSH | [Notebooks and remote machines](notebooks.md) |
| Fix a problem | [Troubleshooting guide](troubleshooting.md) |
