# How it works

A chronozarr store is a Zarr v3 group arranged so that a browser can read it over plain HTTP.

The map is divided into square cells, 512 by 512 pixels by default. Each chunk holds one cell at one timestep, with all of its bands. To show a view, the browser downloads only the chunks for the visible cells at the current timestep.

The store also holds a pyramid. Level 0 holds your values unchanged. Each coarser level is the mean of 2 by 2 blocks of the level below it, at half the resolution. When you zoom out, the viewer switches to a coarser level, so a view of the whole area still needs only a few chunks.

```text
my_store/
  zarr.json                root metadata
  0/                       level 0, the original resolution
    data/                  (time, band, y, x)
    time/ band/ y/ x/      coordinates
    mask/ coverage/        optional
  1/ 2/ ...                coarser levels
```

The chunks hold the stored numbers in their original type: uint8, uint16, int16 or float32. A scale and offset per band give physical units. An optional mask marks invalid pixels. An optional coverage plane counts the valid observations behind each pixel. The viewer draws these numbers on the GPU and computes products such as true color or NDVI there. It also downloads the timesteps around the current one in the background, so stepping through time usually needs no new download.

The layout follows the Zarr conventions for pyramids (`multiscales`), coordinate systems (`proj`) and georeferencing (`spatial`), all at v0.1. This is why other tools can read a store without chronozarr installed: xarray, GDAL 3.13, and CarbonPlan's zarr-layer for MapLibre.

To publish a store, upload it to a static host that sends CORS headers. A sharded store also needs byte-range requests. The demo store is on Cloudflare R2, and there is no server code to run.

## Write a store from Python

1. Save this script as `quickstart.py`. It writes a store of three synthetic timesteps and reads one back. The pixels are 10 m in UTM zone 31N.

   ```python
   import numpy as np
   import xarray as xr
   import chronozarr

   values = np.arange(3 * 64 * 64, dtype=np.uint16).reshape(3, 1, 64, 64)
   da = xr.DataArray(
       values,
       dims=("time", "band", "y", "x"),
       coords={
           "time": np.array(["2024-01-01", "2024-02-01", "2024-03-01"], dtype="datetime64[ns]"),
           "band": ["example"],
           "y": 5000000 - (np.arange(64) + 0.5) * 10,
           "x": 500000 + (np.arange(64) + 0.5) * 10,
       },
   )
   chronozarr.encode(da, "synthetic_store", crs="EPSG:32631", nodata=None)

   store = chronozarr.open_store("synthetic_store")
   print((store.read(t=1) == values[1]).all())
   ```

2. Run the script. It prints `True`: level 0 returns the values that you wrote.

   ```bash
   python quickstart.py
   ```

3. Check the store.

   ```bash
   chronozarr validate synthetic_store
   chronozarr info synthetic_store
   ```

## Read a store without chronozarr

xarray reads each level as a plain Zarr group:

```python
import xarray as xr

ds = xr.open_zarr("my_store", group="0", zarr_format=3, chunks=None)
```

GDAL 3.13 reads the CRS, the georeferencing and the values. It attaches the coarser levels as overviews when you open the data array as `ZARR:"my_store":/0/data`. A single time-slice subdataset shows no overviews. To get Cloud Optimized GeoTIFFs for older GDAL or QGIS, run `chronozarr export-cog my_store out_dir`.
