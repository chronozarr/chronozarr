# What is chronozarr?

chronozarr lets you share a georeferenced raster time series as an interactive map. A recipient can open a link, play or scrub through the dates, zoom in and click a pixel to see its values over time. The same static files keep the original pixel values for analysis in Python.

[Open the live demo](https://chronozarr.org/demo/). It shows 117 monthly Sentinel-2 composites of the Ucayali River in Peru, 2015 to 2026, read directly from a storage bucket.

## What you get

| You do | Result |
|---|---|
| Convert dated GeoTIFFs, a Zarr store or a NetCDF file | A store: a folder of static files in the open chronozarr layout |
| Preview the store | The viewer opens it from your computer |
| Share the store | A temporary link served from your computer, or a permanent link to your own cloud storage |
| Read the store in Python | Use xarray or `chronozarr.open_store` for physical values with scale, offset and validity applied, or request the raw stored values |

A store is a [Zarr v3](https://zarr.dev) group. It holds your values at full resolution and a pyramid of coarser levels for zoomed-out views. The browser downloads only the parts of the map that are on the screen, at the current date. [How it works](how-it-works.md) explains the layout.

## Terms used in these guides

| Term | Meaning |
|---|---|
| Raster | A grid of pixels, each holding one or more measurements |
| COG | Cloud Optimized GeoTIFF: a GeoTIFF arranged so a reader can fetch part of the image without downloading the whole file |
| CRS and EPSG | The coordinate reference system (CRS) defines where coordinates fall on Earth. An EPSG code, such as `EPSG:32718`, identifies a particular CRS |
| Nodata | Missing or invalid data, marked by a special value or a separate validity mask |
| Pyramid and LOD | A pyramid holds the full-resolution image and coarser copies. Level of detail (LOD) selects a level: level 0 is the finest, and each next level has pixels twice as wide and tall |

## Compared with other ways to share a time series

| Option | What the recipient can do | What it needs |
|---|---|---|
| GIF or MP4 | Watch the frames you rendered | A video player. The colours, the zoom and the dates are fixed when you render it. The pixel values are gone |
| A folder of GeoTIFFs | Analyze the values | GIS software or a separate viewer to animate the series; COGs support partial reads |
| Plain Zarr or NetCDF in a bucket | Analyze the values in a compatible client | A viewer and any pyramid for zoomed-out views must be set up separately |
| A tile server, such as TiTiler or GeoServer | Explore rendered tiles in a browser | A server that runs and is paid for while the link is in use. Most send rendered images, not values |
| chronozarr | Play, zoom, inspect pixels in a browser. Analyze the same values in Python | A one-time conversion. Static file hosting, or your own computer for a temporary link |

A chronozarr store is also a plain Zarr store. xarray, zarr-python and GDAL 3.13 read it without chronozarr installed. They return the stored values: plain xarray and zarr-layer 0.11.0 do not apply its band scale and offset or its mask ([GeoZarr review](../spec/review-2026-10-geozarr.md)).

## What chronozarr does not do

You host the data on your computer or in storage that you control; chronozarr.org serves the viewer page. Prepare composites and cloud masks before conversion. The [Sentinel-2 guide](sentinel2.md) provides a pipeline for those steps.

All timesteps share one north-up grid in one coordinate system, with or without an EPSG code. A store keeps that projection and supports uint8, uint16, int16 or float32 values. Conversion can resample inputs onto a grid when you request it, but the store does not use Web Mercator map tiles.

## Versions

There are two version numbers:

| Item | Version | Meaning |
|---|---|---|
| Python package and npm package `chronozarr` | 0.5.2 | The software that writes, checks, serves and reads stores |
| Store format specifications | v0.3.0 and v0.4.0, drafts | The layout of the files in a store. A store whose CRS has an EPSG code declares `0.3.0`. A store whose CRS has none declares `0.4.0` |

Use `chronozarr --version` to check the installed software. `chronozarr info my_store` reports the store's format version, which can be older than the software reading it.

A package release does not change the stores that you already wrote. Package 0.5.2 reads and writes both v0.3 and v0.4 stores. A reader from package 0.4.1 or earlier refuses a v0.4 store; upgrade the reader and keep the store. To use a v0.2 store, convert it with `chronozarr convert OLD_STORE NEW_STORE`. The [release notes](release-0.5.2.md) list the changes in 0.5.2.

## Next steps

- [Explore the live demo](viewer.md) to see what a recipient sees.
- [Quickstart](quickstart.md): make your first store and open it.
- [Performance and evidence](performance.md): measured speed, memory and correctness, with their conditions.
