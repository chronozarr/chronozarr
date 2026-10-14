# Examples: SWOT, NISAR, model output and other data

A store holds any raster time series on one north-up grid in an EPSG coordinate system. This page lists the worked examples in the repository and the route for each kind of data. Each example has a README with its commands, run from a checkout of the repository.

## Worked examples

| Example | Data | What you need | README |
|---|---|---|---|
| Sentinel-2 monthly composites | Planetary Computer, public | Nothing beyond the repository. See [Sentinel-2 for your area](sentinel2.md) | [sentinel2_pc](../examples/sentinel2_pc/README.md) |
| SWOT water surface elevation | Two SWOT L2 HR Raster 100 m NetCDF files, Roanoke region, about 60 MB each | A free NASA Earthdata login to download them from PO.DAAC | [swot_raster](../examples/swot_raster/README.md) |
| NISAR backscatter | A year of NISAR L2 GCOV HH and HV over one box, read from the ASF DAAC granules by HTTP range requests | A free NASA Earthdata login | [nisar](../examples/nisar/README.md) |
| SWOT intensity | Geocoded SWOT amplitude of Wax Lake | Inputs from a private project. The README names the public source granules | [swot_intensity](../examples/swot_intensity/README.md) |
| Water masks | NDWI and water fraction derived from the Sentinel-2 composites | The output of the Sentinel-2 example | [water_masks](../examples/water_masks/README.md) |
| PNG frames | 36 monthly true-color PNG frames with world files | The output of the Sentinel-2 example | [png_frames](../examples/png_frames/README.md) |
| Your own GeoTIFFs, end to end | Any dated GeoTIFFs on one grid | Your files | [Your own data, end to end](../examples/bring_your_data/README.md) |
| Notebook player | The public PNG demo store | `chronozarr[notebook]` and a local Jupyter or VS Code notebook | [anywidget](../examples/anywidget/README.md) |
| leafmap map | The public demo store | `chronozarr[leafmap]` | [leafmap](../examples/leafmap/README.md) |

## Routes by kind of data

| Data | Route |
|---|---|
| Dated GeoTIFFs or COGs on disk or in S3 | `chronozarr convert` on the directory, a quoted glob or the `s3://` prefix. See [Convert your data](convert.md) |
| COGs at URLs, with dates in a table | `chronozarr convert` on a CSV or JSON manifest of URLs and dates. See [Convert your data](convert.md#from-a-manifest-of-urls) |
| A NetCDF file or a Zarr store with a time axis | `chronozarr convert FILE OUT --variable NAME`. See [Convert your data](convert.md#from-netcdf-or-zarr) |
| An xarray `DataArray` in a script or notebook | `chronozarr.encode(da, "my_store", crs="EPSG:32618")`. See [Python API](python.md#write-a-store) |
| Rendered PNG frames from another tool | `chronozarr convert` on a manifest of frames with world files. See [PNG frames](png-frames.md) |
| Sentinel-2 imagery that you have not downloaded | The Sentinel-2 pipeline. See [Sentinel-2 for your area](sentinel2.md) |

## Model output

Model output on a regular grid converts like any other NetCDF or Zarr variable. A grid in latitude and longitude uses `EPSG:4326`:

```bash
chronozarr convert model.nc model_store --variable tas --crs EPSG:4326
```

`--crs` is needed only when the file does not declare its coordinate system. `--dims time=NAME,y=NAME,x=NAME` names the axes when they are not `time`, `y` and `x` or the usual aliases such as `lat` and `lon`. `convert` reverses rows that run from south to north, so the store is north-up. A store cannot hold a curvilinear or rotated grid. Regrid such output to a regular grid first.
