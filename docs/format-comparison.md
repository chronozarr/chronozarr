# chronozarr compared with other formats

This page compares chronozarr with other ways to serve raster data to a web map. Each tool answers a different question, so each section ends with the case for choosing that tool instead.

Statements about chronozarr come from [spec/CHRONOZARR.md](../spec/CHRONOZARR.md), the tests in this repository and the measurements in [evidence.md](evidence.md). Statements about the other tools come from their documentation and source. The zarr-layer, topozarr, ndpyramid, titiler-multidim and GeoZarr statements, and those about the other GeoZarr readers, were read on 2026-10-19. The PMTiles, Mapbox raster-array and COG + TiTiler statements were last read on 2026-09-30. [evidence.md](evidence.md#comparison-notes) lists the versions and what was read.

Comparisons that were measured come from the [GeoZarr review](../spec/review-2026-10-geozarr.md): one Apple M3 Max with other work running, on 2026-10-18 and 2026-10-19. They show how the tools behaved on that machine and link, not a ranking.

## Summary

| Tool | What it stores | Time axis | Values | Server needed |
|---|---|---|---|---|
| chronozarr | A Zarr v3 pyramid | A time dimension; the reader prefetches timesteps | Stored values, with per-band scale and offset and a validity mask | No |
| PMTiles | An archive of z/x/y tiles | None | Tile pixels | No |
| Mapbox raster-array | MRT raster tiles | Bands | Numeric | Mapbox Tiling Service |
| zarr-layer, written by topozarr or ndpyramid | A Zarr pyramid | A selector on a time dimension | 8 and 16 bit integers as they are, other types as float32; CF scale and offset | No |
| COG + TiTiler | Cloud Optimized GeoTIFFs | One file per timestep | Exact values in any GDAL dtype | Yes |
| titiler-multidim | Zarr or NetCDF | A `sel` on any dimension, time included | Whatever the array holds | Yes |
| GeoZarr | Conventions for Zarr | The dimensions of the dataset | Whatever the array holds | No |

The time axis is not a difference in kind between chronozarr and zarr-layer: both select a timestep on a time dimension. The measured difference is in the client. On a fast link, chronozarr's viewer, its MapLibre layer and zarr-layer each drew 239 or 240 of the 240 steps of a 60 s scrub. With the link limited to 12 MB/s and 30 ms, the viewer drew 240 of 240 by dropping to coarser levels, chronozarr's MapLibre layer drew 128, and zarr-layer drew 45. See [zarr-layer](#zarr-layer-topozarr-and-ndpyramid) and the [review](../spec/review-2026-10-geozarr.md).

## chronozarr

chronozarr stores a Zarr v3 pyramid of `(time, band, y, x)` arrays. Each level is one group. The default is one object per chunk, and sharding is an option.

Time is a dimension of the array. Any timestep of any level is one data chunk read. The values are the exact stored values in `uint8`, `uint16`, `int16` or `float32`. They are lossless. Each band has a `scale` and an `offset` in the root metadata, and a store can carry a `mask` array. chronozarr's readers apply both. A reader that does not know chronozarr returns the stored values.

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

## zarr-layer, topozarr and ndpyramid

CarbonPlan's zarr-layer renders Zarr stores in MapLibre or Mapbox GL. Its current release is 0.11.0 (2026-10-02). It reads Zarr v2 and v3. A store that carries the `multiscales`, `proj` and `spatial` conventions needs no configuration, and legacy ndpyramid pyramids still open. It picks the level that matches the screen resolution and loads the chunks in view.

A selector on a time dimension picks the timestep, by value or by index. `queryData` takes a selector with several values, such as `{ time: [0, 1, 2] }`, and returns the series for a point, a line or a polygon. In the usual layout each timestep is its own chunk, so there is no compression across time.

A custom fragment shader does band math. The README shows NDVI from `B08` and `B04`. In a layer with a custom shader, bands of 8 and 16 bit integer types go to the GPU in integer textures, and the shader applies scale, offset and the fill value. Other types are uploaded as float32, 32-bit integers included. The fill-value comparison of a 32-bit integer is exact only up to 2^24. Decoded chunks are kept in a cache bounded by entry count and by bytes, 256 MiB per store by default.

zarr-layer reprojects a native CRS on the GPU. The CRS comes from `proj:code`, `proj:wkt2`, `proj:projjson` or an option. On 2026-10-19 zarr-layer 0.11.0 placed a Lambert conformal conic store, a copy of it described by PROJJSON and a sinusoidal store exactly, none with an EPSG code (27 of 27 checked pixels each). It drew a store with longitudes from 165 to 195 across 180° without a seam ([review](../spec/review-2026-10-geozarr.md)).

Two tools write the stores for it. topozarr 0.2.0 (2026-10-13) is CarbonPlan's current writer. It coarsens an xarray dataset, by 2 per level unless told otherwise, in the dataset's own grid and CRS and writes `multiscales`, `proj` and `spatial` metadata. It does not reproject: its documentation says to reproject first. ndpyramid's last release is 0.4.0 (2024-11-22). That predates the multiscales convention, whose repository was created on 2025-10-15 and tagged v0.1 on 2026-06-12. ndpyramid writes Zarr pyramids that are reprojected to a tile grid or coarsened in place, and it was built for CarbonPlan's `@carbonplan/maps`.

zarr-layer reads chronozarr stores. It takes a band's scale and offset only from the CF `scale_factor` and `add_offset` attributes of an array, and the nodata value from the array's `fill_value`. chronozarr keeps the scale and offset in its own `chronozarr` block and the validity in a `mask` array. In the review's test of 2026-10-19, zarr-layer 0.11.0 ignored both. It hid valid zeros where the array `fill_value` is 0 and drew masked pixels. The Sentinel-1 and NISAR public stores keep their dB scale only in the `chronozarr` block, so zarr-layer shows their stored integers.

On a fast link zarr-layer matched the viewer on the same stores. It used the pyramid, reprojected native UTM with exact values, uploaded `uint16` as it is, ran true color and NDVI in a custom shader and returned 117 of 117 pixel values equal to zarr-python's. It took 89 lines of page code to reach that parity on time selection, scrubbing, band math and the pixel series, against 33 for chronozarr's MapLibre layer. Warm timestep switches took one display frame in both, with no request ([review](../spec/review-2026-10-geozarr.md)).

Choose zarr-layer instead when you want the established MapLibre Zarr layer, float data and selectors over arbitrary dimensions. Choose it also when your data already comes out of topozarr or ndpyramid, or when the store must stay a plain GeoZarr store that other readers open. The v0.3 fixture opens in zarr-layer without metadata overrides. The tested versions are in [evidence.md](evidence.md#reader-checks).

## COG + TiTiler

COG + TiTiler serves single-timestep Cloud Optimized GeoTIFFs, with internal tiles and overviews. A tile server renders them to image tiles. A time series is one file per timestep, or STAC mosaics selected on the server.

The values are exact in any GDAL dtype, and lossless compression is available. Point queries and band math run on the server. Any CRS works, because the server reprojects to a tile matrix. A running service, such as a container or a serverless function, must sit in front of the files.

titiler-multidim (0.10.2, 2026-10-14) is a TiTiler application for Zarr and NetCDF, built on `titiler.xarray`. A request picks a variable and a slice of any dimension with `sel`, for example `sel=time=nearest::2020-01-06`, and the server renders tiles and point values. A time series can stay in one Zarr store, with no COG per date. It needs a running service too.

Choose COG + TiTiler instead when a server is acceptable and one of these holds:

- Your data are already COGs or in a STAC catalog.
- Your data are a Zarr or NetCDF time series, and titiler-multidim serves them.
- You need server-side band math.
- Many clients read the COGs directly, for example GDAL, QGIS or rasterio.
- You serve single-date imagery.

## GeoZarr

GeoZarr is no longer one document. The GeoZarr working group develops conventions, and its README says the specification will be produced once a set of mature conventions forms a coherent suite. The conventions are `multiscales` for pyramids, `proj` for the CRS and `spatial` for the transform between array indexes and coordinates. Each README classes its convention as Pilot and pre-stable: breaking changes are expected until v1, which the READMEs anticipate before the end of 2026. The geozarr-spec repository has no GitHub release.

The time axis is whatever the dimensions of the dataset are, with no temporal encoding. How time and bands are described is open. On 2026-10-19 these issues were open: geozarr-spec #136 "Non-spatial dimensions (time etc.)" (2026-05-18), #131 "How to represent layers in Zarr", about a band dimension against one array per band (2026-04-21), and zarr-conventions/spatial #33 "How should non-spatial coordinates be handled?" (2026-05-28). A per-band scale and offset was deferred to a later phase (#52, closed 2025-04-08). The values are whatever the array holds. Any CRS that the conventions can describe works, and the Zarr is static.

chronozarr v0.3 pins the multiscales, proj and spatial v0.1 conventions and follows the same `proj` and `spatial` attributes. On top of them it adds four things that change what the numbers mean: a per-band scale and offset, the validity rule (a `nodata` value or a `mask` array), the meaning of an overview level (the mean of the valid pixels in each block) and a version guard that refuses v0.2 stores. It also adds a time-series profile, `coverage` and a browser reader.

In the review's tests, every variant of a v0.3 store returned the same values through plain Zarr and xarray, including stores with the whole `chronozarr` block removed. What a generic reader loses is the meaning, not the values: plain xarray returns stored values with neither scale nor mask, and zarr-layer ignores both (see [above](#zarr-layer-topozarr-and-ndpyramid)). Upstream has only proposals for the scale, the validity rule and the overview meaning ([review](../spec/review-2026-10-geozarr.md)). The nearest is `missing_value`, a nodata sentinel convention that was classed Proposal on 2026-10-19.

Choose GeoZarr instead when you publish general-purpose geospatial arrays for analysis tools and want the community convention as it settles.

### Other GeoZarr readers

These readers were read from their repositories on 2026-10-19.

- **OpenLayers** 10.11.0 (2026-10-04) has the source `ol/source/GeoZarr`. It reads `multiscales`, `proj` and `spatial` stores. For a `(time, band, y, x)` array, its `variable` and `dimensions` options pick the timestep and the bands, by index or label. It draws stored values: applying CF scale and offset is an open issue (openlayers/openlayers#17649, 2026-09-30).
- **deck.gl-raster** 0.8.1 (2026-09-24) has a `deck.gl-zarr` layer and a `geozarr` metadata parser for the `spatial`, `multiscales` and `proj` conventions. The layer picks the level and reprojects each chunk to Web Mercator. The application supplies the callbacks that read and draw a tile, so time selection and band math are its own work.
- **GDAL** 3.13 (3.13.0, 2026-05-08) reads the `multiscales` convention of Zarr v3 as overviews, and reads `spatial` and `proj`. [evidence.md](evidence.md#reader-checks) has the checks on a chronozarr fixture.
- **Icechunk** 2.3.0 (2026-10-06) is a transactional storage engine for Zarr, not a layout convention. It keeps snapshots and manifests in its own binary format next to the chunks, so a reader needs an Icechunk client. zarr-layer accepts an Icechunk store through its `store` option, and topozarr can write to one. A chronozarr store is plain objects and needs no engine. It has no transactions.

## What chronozarr costs

A wide view downloads a lot of data. The values are lossless and unquantized. The measured size of a wide overview is in [evidence.md](evidence.md#playback).

The viewer downloads more than zarr-layer does. In the review, on a local store, the viewer fetched 187 to 226 MB in the 1.5 s after the first frame. A 60 s scrub fetched 1,450 MB where zarr-layer fetched 544 MB. The review names the speculative fetching after the first frame as the measured cost of the viewer's slow-link playback ([review](../spec/review-2026-10-geozarr.md)).

Scrubbing uses client memory. It depends on worker decoding, cached chunks and time-window prefetch.

On 2026-10-19 every chronozarr reader refused an ndpyramid store, an ESA EOPF Sentinel-2 product and a plain GeoZarr store at the first check, with the message "missing chronozarr" ([review](../spec/review-2026-10-geozarr.md)). The Python reader now opens them from the multiscales layout and reports what it assumed ([evidence.md](evidence.md#stores-written-by-other-tools-2026-10-19)). The JavaScript reader and the viewer still open only chronozarr stores. zarr-layer and GDAL open a chronozarr store, but zarr-layer ignores its scale and mask, as described above.

Two readers have been checked against the v0.3 fixture: GDAL and zarr-layer. The versions, and what the checks did not establish, are in [evidence.md](evidence.md#reader-checks).

chronozarr has no server-side rendering. Products such as true color, NDVI and water are band math in the fragment shader of the client. Server-side rendering, mosaicking of many sources and arbitrary expressions belong with COG + TiTiler or titiler-multidim.

## What the other tools cost

- TiTiler and titiler-multidim need a server that you run and scale.
- PMTiles, Mapbox raster-array and the reprojected pyramids of ndpyramid resample every timestep to Web Mercator before storage. This biases block statistics and discards native pixels. topozarr and zarr-layer keep the native grid.
- The common zarr-layer layout puts one timestep in each chunk. Each timestep then needs a fresh chunk fetch, and timesteps share no state. chronozarr also stores one timestep per chunk. Its viewer prefetches nearby timesteps and lowers the level when the link is slow. On a link limited to 12 MB/s and 30 ms, zarr-layer drew 45 of the 240 steps of a 60 s scrub, and the viewer drew all 240.
- Mapbox raster-array depends on the tiling service and renderer of one vendor.
