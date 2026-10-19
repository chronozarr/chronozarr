# Convert your data

`chronozarr convert SOURCE OUT` writes a store from rasters that you already have. It reads one timestep at a time and encodes a few 512 by 512 cells at a time, so the files never have to fit in memory together. Since 0.4.1 the memory of the encode step stays nearly flat as the number of timesteps grows ([memory](performance.md#encoder-time-slabs)); in 0.4.0 it grew with them.

| Your data | Section |
|---|---|
| A folder, a glob or an `s3://` prefix of GeoTIFFs with a date in each name | [From dated GeoTIFFs](#from-dated-geotiffs) |
| COGs at URLs, with dates in a table | [From a manifest of URLs](#from-a-manifest-of-urls) |
| A NetCDF file or a Zarr store with a time axis | [From NetCDF or Zarr](#from-netcdf-or-zarr) |
| An xarray `DataArray` in Python | [From xarray](#from-xarray) |
| Rendered PNG frames | [PNG frames](png-frames.md) |

Every route checks all inputs before it writes anything. Nothing is resampled or rescaled unless you ask.

## From dated GeoTIFFs

Point `convert` at a directory, a quoted glob or an `s3://` prefix. Each file name holds one date: `20240131`, `2024-01-31` or `2024-01`.

```bash
chronozarr convert "scenes/*.tif" my_store --dry-run   # check every file, write nothing
chronozarr convert "scenes/*.tif" my_store
```

The dry run runs the same discovery and checks as conversion. It prints each file with its date, the grid, the bands, the scaling, the validity rule and the expected size. The pixel size is in the unit of the CRS: `m`, `degree` or `US survey foot`. A geographic grid whose longitudes run outside -180 to 180, such as 0 to 360, gets a `longitude:` line. The store keeps those values, so a lookup in xarray must use the same convention. A directory or `s3://` prefix is not searched recursively; use a glob such as `"scenes/**/*.tif"` for subdirectories. Listing an S3 prefix needs the `s3` extra (`uv tool install --python 3.13 'chronozarr[geo,s3]==0.4.1'`) and uses your AWS credentials.

A date is read from a name only when the name holds exactly one. A name with no date, with several (`20240215_2024-03`), or two files with the same date are reported, never guessed. `--date-pattern` says where the date is, for example `--date-pattern "ndvi_%Y%m%d"`. `--write-manifest found.csv` saves discovered files and dates as a manifest that `convert` reads back; it is the one output allowed with `--dry-run`.

Before it reads any pixels in bulk, `convert` checks every file for its date, grid and CRS, bands, dtype, scale, offset, units and nodata. It reports all problems at once, grouped by file, each with a suggested fix, and writes nothing:

```text
2 problem(s) in 2 of 24 source file(s); nothing was written:

scenes/ndvi_2024-03.tif
  - has scales [0.5, 1.0]; scenes/ndvi_2024-01.tif has [1.0, 1.0]. A store holds one value per band for every timestep, so the sources must agree
    fix: if the metadata is wrong, correct it (gdal_edit.py -scale -offset -units); ...

scenes/readme_copy.tif
  - no date in the file name
    fix: name the date YYYYMMDD, YYYY-MM-DD or YYYY-MM, or pass --date-pattern ...
```

Files on another grid fail with a suggestion to pass `--resampling`, which warps them to the grid of the first file.

The store takes each band's description (its name), scale, offset and units from the files, so a Sentinel-2 file with a scale of 0.0001 gives reflectance. These must be identical in every file. A file whose bands have no description gives bands named `1`, `2` and so on. To name them, write the files and dates to a manifest with `--write-manifest found.csv`, add a `bands` column such as `ndvi`, and convert the manifest. The store also takes the nodata value that the files declare. Files that declare none give a store with no nodata, so a stored 0 is data. `--nodata 0` sets one.

The dry run also reads three timesteps and checks their pixels. A `fill:` line names a value that the files do not declare as nodata but that looks like a fill: one far outside the other values, such as `-9999` in rainfall, or a common code such as `-3000`. The store keeps it as data, because it could be real. The line gives the option that marks it, such as `--nodata -9999`. A `range:` line gives the values of each band after scale and offset. A scale written as a divisor, such as MODIS NDVI with a scale of 10000, shows up there as a range near 1e8 before anything is written.

## From a manifest of URLs

A manifest lists the files and their dates. Use it when the dates are not in the file names, or when the files are at URLs.

```text
uri,datetime
https://example.org/cogs/scene_a.tif,2024-01-15
https://example.org/cogs/scene_b.tif,2024-02-14
s3://my-bucket/cogs/scene_c.tif,2024-03-16T10:30:00Z
```

```bash
chronozarr convert manifest.csv my_store
```

A CSV manifest has the columns `uri` and `datetime`, and an optional `bands` column with names separated by `;`. A JSON manifest is a list of `{"uri": ..., "datetime": ...}` objects. Relative paths are relative to the manifest. The target grid is the grid of the first file, unless you give `--crs`, `--transform` and `--shape`. Files on another grid need `--resampling`.

## From NetCDF or Zarr

```bash
chronozarr convert model.nc my_store --variable tas
chronozarr convert data.zarr my_store --variable ndvi --crs EPSG:32618
```

`--variable` names the array to convert. Its dimensions must be time, y and x, with an optional band dimension. `--dims time=NAME,y=NAME,x=NAME[,band=NAME]` names them when they are not `time`, `y`, `x` or common aliases such as `lat` and `lon`. `--crs` gives the coordinate system when the file does not declare it. The CF `scale_factor`, `add_offset` and `units` of the variable apply to every band. `--mask-var` names a variable whose nonzero values mark valid pixels.

For an array that fits in memory, `chronozarr encode FILE my_store --variable NAME` is the shorter route.

## From xarray

In a script or notebook, write a `DataArray` with dimensions `(time, band, y, x)`:

```python
import chronozarr

chronozarr.encode(da, "my_store", crs="EPSG:32618")
```

The `x` and `y` coordinates are the projected pixel centres. See [How it works](how-it-works.md#write-a-store-from-python) for a complete script, and the [Python API](python.md#write-a-store) for the options.

## Band roles

The viewer offers True color, False color, NDVI, NDWI and Water when it knows which band is red, green, blue and near infrared. It recognizes the names `red`, `green`, `blue` and `nir`, and the Sentinel-2 names `B02`, `B03`, `B04` and `B08`. It offers the Dual-pol composite for bands named VV and VH, or HH and HV. For other names, set the roles:

```bash
chronozarr bands my_store                              # list bands, roles and products
chronozarr bands my_store --band-role b4=red,b8=nir    # set roles
```

`convert` and `encode` take `--band-role` too. A store without roles still opens, with the Single band product. See [Band roles and the first view](python.md#band-roles-and-the-first-view).

## Check the store

```bash
chronozarr validate my_store
chronozarr info my_store
```

`validate` checks the store against the [specification](../spec/CHRONOZARR.md). `info` prints the times, bands and levels.

## Next

- [Share a temporary link](share.md).
- [Preview a store](preview.md) on your computer only.
- [Publish a permanent link](publish.md).
- [Analyze a store in Python](analyze.md).
