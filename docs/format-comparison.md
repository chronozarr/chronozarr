# chronozarr compared with other formats

This page compares chronozarr with five other ways to serve raster data to a web map. Each tool answers a different question, so each section ends with the case for choosing that tool instead.

Statements about chronozarr come from [spec/CHRONOZARR.md](../spec/CHRONOZARR.md), the tests in this repository and the measurements in [evidence.md](evidence.md). Statements about the other tools come from their documentation and source. [evidence.md](evidence.md#comparison-notes) gives the date.

## Summary

| Tool | What it stores | Time axis | Values | Server needed |
|---|---|---|---|---|
| chronozarr | A Zarr v3 pyramid | Native | Exact stored values | No |
| PMTiles | An archive of z/x/y tiles | None | Tile pixels | No |
| Mapbox raster-array | MRT raster tiles | Bands | Numeric | Mapbox Tiling Service |
| ndpyramid + zarr-layer | A Zarr pyramid | zarr-layer selectors | Any Zarr dtype | No |
| COG + TiTiler | Cloud Optimized GeoTIFFs | One file per timestep | Exact values in any GDAL dtype | Yes |
| GeoZarr | Conventions for Zarr | The dimensions of the dataset | Whatever the array holds | No |

## chronozarr

chronozarr stores a Zarr v3 pyramid of `(time, band, y, x)` arrays. Each level is one group. The default is one object per chunk, and sharding is an option.

The time axis is native. Any timestep of any level is one data chunk read. The values are the exact stored values in `uint8`, `uint16`, `int16` or `float32`. They are lossless, and each band has a `scale` and an `offset`.

The store keeps its native projected CRS, such as UTM. A static bucket with byte ranges and CORS serves it, and no server runs.

## PMTiles

PMTiles stores one archive of z/x/y tiles, vector or image, addressed by byte range. It has no time axis, so a time series is one archive per timestep. The values are whatever the tile image encodes. That is rendered 8-bit pixels, or values packed into color channels at the precision of the packing.

The tile scheme is Web Mercator in practice. A static bucket with byte ranges serves the archive, and no server runs.

Choose PMTiles instead when you want finished map tiles for one moment, such as basemaps, vector layers or rendered imagery. It also has broad client support across MapLibre, Leaflet and OpenLayers. Choose it when you need neither raw values nor a time scrubber.

## Mapbox raster-array

Mapbox raster-array stores multi-band numeric raster tiles in the MRT format. The Mapbox Tiling Service produces them. Bands can carry time steps or variables. The client decodes the numeric values.

The tiles use Web Mercator. The format is tied to the Mapbox tiling service and renderer. The decoder code is public in mapbox-gl-js (`src/data/mrt`). The dependency is on that service and renderer.

Choose Mapbox raster-array instead when all of these hold:

- You already build on Mapbox GL JS and the Mapbox Tiling Service.
- You want managed tiling and hosting.
- You can accept data resampled to Web Mercator.

## ndpyramid and zarr-layer

ndpyramid writes Zarr pyramids that are reprojected to a tile grid or coarsened in place. CarbonPlan's zarr-layer renders them in MapLibre or Mapbox GL.

zarr-layer picks a time through selectors on a time dimension. Each timestep is normally its own chunk, so there is no compression across time. Any Zarr dtype works, and float32 is typical.

zarr-layer reads `proj:` and `spatial:` attributes. It supports arbitrary CRS through proj4 reprojection, and its README lists the WGS84 UTM zones as built in. The Zarr is static and served over HTTP.

Choose it instead when you want the established MapLibre Zarr layer, float data and selectors over arbitrary dimensions. Choose it also when your data already comes out of ndpyramid. The v0.3 fixture opens in zarr-layer without metadata overrides. The tested version is in [evidence.md](evidence.md#reader-checks).

## COG + TiTiler

COG + TiTiler serves single-timestep Cloud Optimized GeoTIFFs, with internal tiles and overviews. A tile server renders them to image tiles. A time series is one file per timestep, or STAC mosaics selected on the server.

The values are exact in any GDAL dtype, and lossless compression is available. Point queries and band math run on the server. Any CRS works, because the server reprojects to a tile matrix. A running service, such as a container or a serverless function, must sit in front of the files.

Choose COG + TiTiler instead when a server is acceptable and one of these holds:

- Your data are already COGs or in a STAC catalog.
- You need server-side band math.
- Many clients read the COGs directly, for example GDAL, QGIS or rasterio.
- You serve single-date imagery.

## GeoZarr

GeoZarr is a set of conventions for geospatial Zarr: CRS, affine transform and multiscales. The time axis is whatever the dimensions of the dataset are, with no temporal encoding. The values are whatever the array holds. Any CRS that the conventions can describe works, and the Zarr is static.

chronozarr v0.3 pins the multiscales, proj and spatial v0.1 conventions. Choose GeoZarr instead when you publish general-purpose geospatial arrays for analysis tools and want the community convention as it settles.

chronozarr follows the same `proj` and `spatial` attributes. It adds the time-series profile, band objects, `mask`, `coverage` and a browser reader on top.

## What chronozarr costs

A wide view downloads a lot of data. The values are lossless and unquantized. The measured size of a wide overview is in [evidence.md](evidence.md#playback).

Scrubbing uses client memory. It depends on worker decoding, cached chunks and time-window prefetch.

Two readers have been checked against the v0.3 fixture: GDAL and CarbonPlan's zarr-layer. The versions, and what the checks did not establish, are in [evidence.md](evidence.md#reader-checks).

chronozarr has no server-side rendering. Products such as true color, NDVI and water are band math in the fragment shader of the client. Server-side rendering, mosaicking of many sources and arbitrary expressions belong with COG + TiTiler.

## What the other tools cost

- TiTiler needs a server that you run and scale.
- PMTiles, Mapbox raster-array and the reprojected pyramids of ndpyramid resample every timestep to Web Mercator before storage. This biases block statistics and discards native pixels.
- The common zarr-layer layout puts one timestep in each chunk. Each timestep then needs a fresh chunk fetch, and timesteps share no state. chronozarr also stores one timestep per chunk. Its reader hides that cost with time-window prefetch.
- Mapbox raster-array depends on the tiling service and renderer of one vendor.
