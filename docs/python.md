# Python API

The `chronozarr` package writes a store, reads it and opens it in xarray. The [README](../README.md) has a first run. This page lists the functions. The docstring of `chronozarr.encode` lists every option of `encode`.

## Write a store

```python
import chronozarr

chronozarr.encode(da, "my_store", crs="EPSG:32631")
```

`da` is an xarray `DataArray` with dims `(time, band, y, x)`. Its dtype is uint8, uint16, int16 or float32. Its `x` and `y` coordinates are the projected pixel centres of an EPSG grid with north up.

The output directory must not exist, or it must be empty. The store is unsharded unless you pass `shard=True`.

## Read a store

```python
store = chronozarr.open_store("my_store")   # a local path or an http(s) URL
frame = store.read(t=0, lod=0)              # (band, y, x), the stored values
cell = store.read_cell(t=0, row=0, col=0, lod=0)
mask = store.read_mask(t=0, lod=0)          # None when the store has no mask
coverage = store.read_coverage(t=0, lod=0)  # None when the store has no coverage plane
physical = store.physical(t=0, lod=0)       # float32, scale and offset applied
da = store.to_xarray(lod=0, times=[0], physical=False)
```

`t` is the index of a timestep. `lod` is the pyramid level, and level 0 has the original resolution. `row` and `col` select a cell of the level.

`read`, `read_cell` and `to_xarray` return the stored values in the stored dtype. With `physical=True`, `to_xarray` returns float32 values, as `physical()` does. Each physical value is `stored * scale + offset` for its band. Invalid pixels are NaN.

`to_xarray` loads the requested level into memory.

## Open a store in xarray

```python
import xarray as xr

ds = xr.open_dataset("my_store", engine="chronozarr")
raw = xr.open_dataset("my_store", engine="chronozarr", physical=False)
```

The backend reads lazily. It returns physical values unless you pass `physical=False`. The `lod` argument selects the level, and the default is 0.

Install `chronozarr[dask]` to pass `chunks=` and get dask arrays.

A plain Zarr reader opens a level without chronozarr and returns the stored values. See [Read a store without chronozarr](../README.md#read-a-store-without-chronozarr).

## Show a store in a notebook

```python
chronozarr.view("my_store")
```

Install `chronozarr[notebook]`. For a local store, `view` starts a server on `127.0.0.1` that answers byte-range requests. It then shows the hosted viewer in an iframe. This works in local Jupyter and VS Code sessions. The browser must reach `127.0.0.1` on the machine that runs the kernel.

`chronozarr.player` shows a viewer that Python can control. See [examples/anywidget](../examples/anywidget/README.md).

`chronozarr.add_chronozarr` adds a store as a layer on a leafmap or geemap MapLibre map. Install the `leafmap` or `geemap` extra. See [examples/leafmap](../examples/leafmap/README.md).
