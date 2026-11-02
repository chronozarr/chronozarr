# Analyze a store in Python

A store keeps the values that you converted. Use xarray to select pixels, calculate indices and export results; the [Python API](python.md) lists the available functions.

Install the package where your notebook or script runs. A standalone CLI installed with `uv tool install` has its own environment and does not install the library here.

In a uv project:

```bash
uv add 'chronozarr==0.5.2'
```

Run your script or Python session with `uv run python`. To use an existing Python 3.11+ environment instead:

```bash
python -m pip install 'chronozarr==0.5.2'
```

A notebook must use the environment where you installed the package as its kernel.

## Open a store

```python
import xarray as xr

ds = xr.open_dataset("my_store", engine="chronozarr")
```

The path can be a local folder or the `https://` address of a published store. Use the store address. In a viewer link, it is the value after `store=`.

The dataset loads pixel values when you compute them. Remote reads download the cells needed for the selection; opening the dataset reads metadata first.

```text
<xarray.Dataset> Size: 92MB
Dimensions:     (time: 12, band: 4, y: 692, x: 690)
Coordinates:
  * time        (time) datetime64[ns] 96B 2024-01-01 2024-02-01 ... 2024-12-01
  * band        (band) <U3 48B 'B02' 'B03' 'B04' 'B08'
    band_units  (band) <U11 176B ...
  * y           (y) float64 6kB 9.17e+06 9.17e+06 ... 9.142e+06 9.142e+06
  * x           (x) float64 6kB 4.857e+05 4.857e+05 ... 5.132e+05 5.132e+05
Data variables:
    data        (time, band, y, x) float32 92MB ...
Attributes:
    crs:                      EPSG:32718
    transform:                [40.0, 0.0, 485650.0, 0.0, -40.0, 9169880.0]
    resolution:               40.0
    lod:                      0
    chronozarr_spec_version:  0.3.0
    nodata:                   0
```

This is the store from the [quickstart](quickstart.md), whose bands declare units. `band_units` exists only when the source declares units. A store without units has no `band_units` coordinate, and `ds.band_units` raises `AttributeError`. `chronozarr bands my_store` prints the units of each band, or `-` where there are none. `x` and `y` are the centres of the pixels, in the coordinate system named by `crs`.

## Physical values and stored values

By default the dataset holds physical values: float32, `stored * scale + offset` for each band, and NaN where a pixel is invalid. A Sentinel-2 band stored as 3828 with a scale of 0.0001 reads as 0.3828.

To read the stored integers, pass `physical=False`:

```python
raw = xr.open_dataset("my_store", engine="chronozarr", physical=False)
```

`chronozarr bands my_store` prints the scale, offset and units of each band. `chronozarr info my_store` prints the nodata value.

## Common tasks

To read a pixel's history, select the band by name first, then select the nearest spatial coordinates. `method="nearest"` applies to every selector in a call. Combining the text band name with `x` and `y` in that call raises `TypeError`:

```python
series = ds["data"].sel(band="B08").sel(x=496000, y=9160000, method="nearest")
series.to_series()
```

An index for every timestep, averaged over the map:

```python
nir = ds["data"].sel(band="B08")
red = ds["data"].sel(band="B04")
ndvi = (nir - red) / (nir + red)
ndvi.mean(dim=("y", "x")).to_series()
```

Use a coarser level for a quick look at a large store. Level 0 has the original resolution, and each subsequent level has half as many pixels along each axis. This store has levels 0 and 1. Run `chronozarr info my_store` to list the levels in yours; a `lod` beyond the last raises `IndexError`:

```python
overview = xr.open_dataset("my_store", engine="chronozarr", lod=1)
```

Mask and coverage: a store with a validity mask has a `mask` variable (1 is valid). At level 0, `coverage` records the input observation count for each pixel; 0 means that no observation was recorded there, including carried values. Coarser levels hold rounded means of those counts, so a zero overview value can also summarize a block containing an observed pixel. Use the mask for validity and level 0 for observation counts.

## Without chronozarr installed

Each level is a plain Zarr v3 group. Any Zarr reader returns the stored values:

```python
ds = xr.open_zarr("my_store", group="0", zarr_format=3, chunks=None)
```

This route does not apply scale, offset, nodata or the mask. A masked pixel can retain a finite source value, so use the `mask` variable to exclude invalid pixels before computing statistics. Source NaNs are replaced with a finite fill value during conversion; that replacement does not make the pixel valid. The store records the scale, offset and nodata in the root `zarr.json`. See [How it works](how-it-works.md#read-a-store-without-chronozarr).

## Export to GIS software

```bash
chronozarr export-cog my_store out_dir --times 2024-01..2024-06
```

This writes one Cloud Optimized GeoTIFF per timestep with the stored values, for QGIS, ArcGIS or GDAL. `--level` picks a coarser level. `--physical` writes float32 physical values instead.
