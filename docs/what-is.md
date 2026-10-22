# What is chronozarr?

chronozarr turns a georeferenced raster time series into an interactive dataset that anyone can open with a link. In a web browser, the series plays or scrubs like a video. You zoom and pan the map. A click on a pixel shows its values over time. In Python, the same files return the exact numbers you stored.

[Open the live demo](https://chronozarr.org/demo/). It shows 117 monthly Sentinel-2 composites of the Ucayali River in Peru, 2015 to 2026, read directly from a storage bucket.

## What you get

| You do | Result |
|---|---|
| Convert dated GeoTIFFs, a Zarr store or a NetCDF file | A store: a folder of static files in the open chronozarr layout |
| Preview the store | The viewer opens it from your computer |
| Share the store | A temporary link served from your computer, or a permanent link to your own cloud storage |
| Read the store in Python | xarray or `chronozarr.open_store` returns the stored values, with scale, offset and nodata |

A store is a [Zarr v3](https://zarr.dev) group. It holds your values at full resolution and a pyramid of coarser levels for zoomed-out views. The browser downloads only the parts of the map that are on the screen, at the current date. [How it works](how-it-works.md) explains the layout.

## Compared with other ways to share a time series

| Option | What the recipient can do | What it needs |
|---|---|---|
| GIF or MP4 | Watch the frames you rendered | A video player. The colours, the zoom and the dates are fixed when you render it. The pixel values are gone |
| A folder of GeoTIFFs | Analyze the values | A download of every file, and GIS software to animate them |
| Plain Zarr or NetCDF in a bucket | Analyze the values in Python | Python. There is no viewer and no pyramid for zoomed-out views |
| A tile server, such as TiTiler or GeoServer | Explore rendered tiles in a browser | A server that runs and is paid for while the link is in use. Most send rendered images, not values |
| chronozarr | Play, zoom, inspect pixels in a browser. Analyze the same values in Python | A one-time conversion. Static file hosting, or your own computer for a temporary link |

A chronozarr store is also a plain Zarr store. xarray, zarr-python and GDAL 3.13 read it without chronozarr installed. They return the stored values: plain xarray and zarr-layer 0.11.0 do not apply its band scale and offset or its mask ([GeoZarr review](../spec/review-2026-10-geozarr.md)).

## What chronozarr does not do

- It does not host your data. The viewer page comes from chronozarr.org. The data comes from your computer or from storage that you control.
- It does not process imagery. Compositing, cloud masking and reprojection happen before conversion. The [Sentinel-2 guide](sentinel2.md) shows one pipeline that does these steps for one sensor.
- It does not make Web Mercator map tiles. A store keeps the projection of your data.
- It does not take every raster. All timesteps of a store share one north-up grid in one coordinate system, with or without an EPSG code. The values are uint8, uint16, int16 or float32.

## Versions

There are two version numbers:

| Item | Version | Meaning |
|---|---|---|
| Python package and npm package `chronozarr` | 0.4.1 | The software that writes, checks, serves and reads stores |
| Store format specification | v0.4.0, draft | The layout of the files in a store. A store whose CRS has an EPSG code declares `0.3.0`. A store whose CRS has none declares `0.4.0` |

A package release does not change the stores that you already wrote. Package 0.4.1 reads and writes v0.3 stores, and refuses a v0.4 store: see [Opening a store](troubleshooting.md#opening-a-store). Later releases read both. Because a store with an EPSG code is always written as v0.3, every release opens it. To use a v0.2 store, convert it with `chronozarr convert OLD_STORE NEW_STORE`. The [release notes](release-0.4.1.md) list the changes in 0.4.1.

## Next steps

- [Explore the live demo](viewer.md) to see what a recipient sees.
- [Quickstart](quickstart.md): make your first store and open it.
- [Performance and evidence](performance.md): measured speed, memory and correctness, with their conditions.
