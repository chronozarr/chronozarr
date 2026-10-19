# Analyze a store in Python

A store holds the values you converted, unchanged. This page reads them with xarray for analysis. The [Python API](python.md) lists every function.

Install the package where your notebook or script runs:

```bash
uv add chronozarr        # in a uv project
pip install chronozarr   # in a pip environment
```

## Open a store

```python
import xarray as xr

ds = xr.open_dataset("my_store", engine="chronozarr")
```

The path can be a local folder or the `https://` address of a published store. Use the store address. In a viewer link, it is the value after `store=`.

The dataset is lazy. Nothing is read until you compute a value. For a remote store, each read downloads only the cells it needs.

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

A pixel's values over time. Select the band by name first, then the pixel by its nearest coordinates. `method="nearest"` applies to every selector in the call, and a band name is text, so a call that gives it both a band and `x` and `y` raises `TypeError`:

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

A coarser level, for a quick look at a large store. Level 0 has the original resolution. Each level halves it. This store has levels 0 and 1; `chronozarr info my_store` lists the levels of yours, and `lod` beyond the last raises `IndexError`:

```python
overview = xr.open_dataset("my_store", engine="chronozarr", lod=1)
```

Mask and coverage: a store with a validity mask has a `mask` variable (1 is valid). A store with a coverage plane has a `coverage` variable: the number of observations behind each pixel. A coverage of 0 means that no observation is behind the pixel: its value was filled from another timestep, or it was never observed.

## Without chronozarr installed

Each level is a plain Zarr v3 group. Any Zarr reader returns the stored values:

```python
ds = xr.open_zarr("my_store", group="0", zarr_format=3, chunks=None)
```

This route does not apply scale, offset, nodata or the mask. A store with a mask holds 0 where the mask is 0, also for float data whose source had NaN, so apply the `mask` variable before you compute statistics. The store records the scale, offset and nodata in the root `zarr.json`. See [How it works](how-it-works.md#read-a-store-without-chronozarr).

## Export to GIS software

```bash
chronozarr export-cog my_store out_dir --times 2024-01..2024-06
```

This writes one Cloud Optimized GeoTIFF per timestep with the stored values, for QGIS, ArcGIS or GDAL. `--level` picks a coarser level. `--physical` writes float32 physical values instead.
