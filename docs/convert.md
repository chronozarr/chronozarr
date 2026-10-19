# Convert your data

`chronozarr convert SOURCE OUT` writes a store from rasters that you already have. It reads one timestep at a time and encodes a few 512 by 512 cells at a time, so the files never have to fit in memory together. Since 0.4.1 the memory of the encode step stays nearly flat as the number of timesteps grows ([memory](performance.md#encoder-time-slabs)); in 0.4.0 it grew with them.

| Your data | Section |
|---|---|
| A folder, a glob or an `s3://` prefix of GeoTIFFs with a date in each name | [From dated GeoTIFFs](#from-dated-geotiffs) |
| BIL, GRIB2, HDF5 or any other raster that GDAL reads, or zips that hold one | [From other raster formats](#from-other-raster-formats) |
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

A date is read from a name only when the name holds exactly one. A name with no date, with several (`20240215_2024-03`), or two files with the same date are reported, never guessed. `--date-pattern` says where the date is, for example `--date-pattern "ndvi_%Y%m%d"`. `--write-manifest found.csv` saves discovered files and dates as a manifest that `convert` reads back; it is the one output allowed with `--dry-run`. The manifest is written even when the checks find problems, and the problems are reported after it. A file whose date was not found gets a row with an empty `datetime`, and the report says which rows; fill them in or delete them, then convert the manifest. When the files name their bands, the manifest has a `bands` column with those names to edit.

MODIS and HLS name a file by year and day of year. `MOD13A1.A2020001.h12v04.061.2020326023424_500m_16_days_NDVI.tif` is 1 January 2020, and `HLS.S30.T17SQV.2020061T155151.v2.0.B04.tif` is 1 March 2020 at 15:51:51. `%j` reads the three-digit day of year, 001 to 366, and goes with `%Y`, alone or with `%H%M%S`:

```bash
chronozarr convert "modis/*.tif" my_store --date-pattern "MOD13A1.A%Y%j" --dry-run
chronozarr convert "hls/*.tif" my_store --date-pattern "HLS.S30.T17SQV.%Y%jT%H%M%S" --dry-run
```

`%j` cannot be combined with `%m` or `%d`, and day 366 is refused in a year that has 365 days. Such a name has no date by the default rules, because seven digits can be anything. When one holds seven digits that read as a year and a day of year, the fix line under `no date in the file name` shows the pattern for that name, with the date it reads.

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

The store takes each band's description (its name), scale, offset and units from the files, so a Sentinel-2 file with a scale of 0.0001 gives reflectance. These must be identical in every file. A file whose bands have no description gives bands named `1`, `2` and so on. To name them, write the files and dates to a manifest with `--write-manifest found.csv`, add a `bands` column such as `ndvi`, and convert the manifest. A `bands` column replaces whatever the files say, so it is also the way to rename a band.

Some files carry a description that is the file's own name, such as the one band of a WorldClim file `wc2.1_10m_tmax_2020-01.tif`. When every file has one band and the descriptions are identical once the dates in them are left out, `convert` takes them for file labels and not for a band name. The band is named by the part they share (`wc2.1_10m_tmax`), or `1` when nothing is left, and the dry run prints a `bands:` line that says what it chose and how to change it. Descriptions that differ in anything but the date, such as `B04` and `B08` or `tmax` and `tmin`, are refused, and so are differing descriptions in files with more than one band. In those cases name the bands in a manifest. The store also takes the nodata value that the files declare. Files that declare none give a store with no nodata, so a stored 0 is data. `--nodata 0` sets one.

The dry run also reads three timesteps and checks their pixels. A `fill:` line names a value that the files do not declare as nodata but that looks like a fill: one far outside the other values, such as `-9999` in rainfall, or a common code such as `-3000`. The store keeps it as data, because it could be real. The line gives the option that marks it, such as `--nodata -9999`. A `range:` line gives the values of each band after scale and offset. A scale written as a divisor, such as MODIS NDVI with a scale of 10000, shows up there as a range near 1e8 before anything is written.

## From other raster formats

`convert` reads any raster that GDAL can open through rasterio, not only GeoTIFF. A folder, a quoted glob or an `s3://` prefix is searched for files with these extensions. Everything else is ignored, including the sidecars that GDAL reads for itself (`.hdr`, `.prj`, `.aux.xml`, `.stx`, `.xml`, `.txt`, `.csv`).

| Format | Extensions |
|---|---|
| GeoTIFF | `.tif`, `.tiff` |
| ESRI BIL and FLT, with their `.hdr` and `.prj` | `.bil`, `.flt` |
| ESRI ASCII grid, with its `.prj` | `.asc` |
| Erdas Imagine | `.img` |
| GRIB and GRIB2 | `.grib`, `.grib2`, `.grb`, `.grb2` |
| HDF5 and HDF-EOS5 | `.h5`, `.hdf5`, `.he5` |
| HDF4, only with a GDAL that has the driver | `.hdf` |
| JPEG 2000 | `.jp2` |
| GDAL virtual raster | `.vrt` |
| One raster in a zip, local files only | `.zip` |

A file with another extension goes in a [manifest](#from-a-manifest-of-urls), which takes any path that GDAL opens. NetCDF is not in the list: a NetCDF file keeps its own route, [From NetCDF or Zarr](#from-netcdf-or-zarr). The dates, the checks and the dry run are those of [dated GeoTIFFs](#from-dated-geotiffs); the first line of the plan says `dated rasters`. A date is read from the file name only, not from the folders above it. If the date is only in a folder name, as in `hrrr.20240115/conus/hrrr.t00z.wrfsfcf00.grib2`, run `--write-manifest found.csv`, fill in the `datetime` column and convert the manifest.

A BIL or FLT file needs its `.hdr`, which carries the grid and the nodata value, beside it, and a `.prj` for the CRS. An ASCII grid takes its CRS from a `.prj`.

### Zips

PRISM delivers one zip per day, each with a BIL file and its sidecars. Point `convert` at the folder of zips, or a quoted glob such as `"zips/*.zip"`:

```bash
chronozarr convert zips/ prism_ppt --dry-run
```

Each raster is read inside its zip, so nothing is unpacked. A zip must hold exactly one raster; its sidecars do not count. The date comes from the zip's name or, when that has none, from the name of the raster inside. A zip with no raster or several, or one that cannot be read, is reported with the other problems and never guessed at. For those, unzip the files and convert the folder. A zip at an `s3://` or `https://` address is not read.

### Files with several variables

An HDF5 or HDF-EOS5 file holds many arrays, which GDAL calls subdatasets, and no raster band of its own. Without `--variable`, `convert` stops once and lists them by group, with their shapes:

```text
Error: SMAP_L3_SM_P_20200601_R19240_001.h5 has no raster band: it holds 104 subdatasets. Choose one with --variable NAME ...:
  Soil_Moisture_Retrieval_Data_AM/ [406x964]
    EASE_column_index, EASE_row_index, albedo, ..., soil_moisture, ...
```

`--variable Soil_Moisture_Retrieval_Data_AM/soil_moisture` reads that array from every file. The group can be left out when no other group has the name, so `--variable soil_moisture` works here. A name that matches none, or several, is refused with the list. A single-band variable gives a band named after it, and `--variable` is refused for files that have no subdatasets.

HDF5 from NASA often has no geotransform that GDAL can read, and the dry run says so. A file that carries none needs `--crs` and `--bounds` with the grid of the product. SMAP L3 is on the 36 km EASE-Grid 2.0, which is EPSG:6933:

```bash
chronozarr convert smap/ smap_sm --variable soil_moisture --crs EPSG:6933 \
  --bounds=-17367530.445,-7314540.795,17367530.445,7314540.795
```

### GRIB and HDF4

GDAL gives GRIB values as 64-bit floats, which a store does not hold, so the dry run reports `sources are float64`. Write float32 files first with `gdal_translate -ot Float32`. A GRIB file with several messages is read as one band per message.

MODIS products on LP DAAC are HDF4. The GDAL inside the rasterio wheel has no HDF4 driver, so each file is reported as `an HDF4 file and this GDAL build has no HDF4 driver`. Write GeoTIFFs with a GDAL that has the driver (`gdalinfo --formats | grep HDF4` shows whether yours does; conda-forge's `libgdal-hdf4` adds it), and convert those. `gdalinfo FILE` lists the subdatasets to translate:

```bash
gdal_translate 'HDF4_EOS:EOS_GRID:"MOD13A3.A2020001.h09v05.061.2020328133217.hdf":MOD_Grid_monthly_1km_VI:1 km monthly NDVI' ndvi_2020001.tif
```

The MODIS names carry a year and day of year, so the GeoTIFFs convert with `--date-pattern`, as under [dated GeoTIFFs](#from-dated-geotiffs).

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

Reading a NetCDF4 file needs the `netcdf` extra, which adds h5netcdf and h5py. The quickstart install does not include it, so add it to the extras that you already use:

```bash
uv tool install --python 3.13 'chronozarr[geo,netcdf]==0.4.1'
```

In a uv project, run `uv add 'chronozarr[netcdf]==0.4.1'`. With pip, install `'chronozarr[netcdf]==0.4.1'` in your environment. Without the extra, `convert` stops with `reading a NetCDF4 file needs h5netcdf and h5py` and these commands. A Zarr store needs no extra. A classic NetCDF3 file, which starts with the bytes `CDF`, is not a NetCDF4 file: add `scipy` to the environment instead, for example `uv tool install --python 3.13 'chronozarr[geo]==0.4.1' --with scipy`.

`--variable` names the array to convert. Its dimensions must be time, y and x, with an optional band dimension. `--dims time=NAME,y=NAME,x=NAME[,band=NAME]` names them when they are not `time`, `y`, `x` or common aliases such as `lat` and `lon`. The coordinate system comes from a `crs`, `crs_wkt` or `spatial_ref` attribute, or from the CF grid mapping variable that the variable names in `grid_mapping`; reading a grid mapping needs pyproj. `--crs` gives it, as an EPSG code or WKT2 text, when the file does not declare it. A CRS with no EPSG code, such as Daymet's Lambert conformal conic, is stored as it is and is never reprojected. The CF `scale_factor`, `add_offset` and `units` of the variable apply to every band. `--mask-var` names a variable whose nonzero values mark valid pixels.

For an array that fits in memory, `chronozarr encode FILE my_store --variable NAME` is the shorter route.

### Climate model calendars

Climate models often write time on a calendar with no Gregorian equivalent: `360_day` (twelve months of 30 days), `noleap` or `365_day`, `all_leap` or `366_day`, and `julian`. CMIP6 files from HadGEM3 (`360_day`) and CESM2 (`noleap`) are examples. A store holds Gregorian timestamps ([specification](../spec/CHRONOZARR.md), section 2.2), so `convert` stops and names the choice:

```text
Error: the time coordinate 'time' uses the 360_day calendar, and a store holds Gregorian timestamps. Pass --calendar-to-gregorian date to keep each step's date and time of day as its Gregorian timestamp; the store then records the source calendar in its provenance. The time elapsed between steps is not kept: steps 30 days apart in the source are 28 to 31 days apart in the store (519 of 779 gaps change). Use it when the dates matter more than that.
```

`--calendar-to-gregorian date` keeps each step's year, month, day and time of day as the Gregorian timestamp, so a step stamped 16 January stays 16 January. It cannot keep the time between steps: months of 30 days become 28 to 31 days apart. The dry run prints a `calendar:` line with that change in numbers. Use it when the date, the month or the season is what matters. For rates per day over a span, compute from the source file, where the days are the model's own.

```bash
chronozarr convert tas_Amon_HadGEM3.nc my_store --variable tas --crs EPSG:4326 --calendar-to-gregorian date --dry-run
```

A date that Gregorian does not have, such as 30 February on a `360_day` calendar or 29 February in a `julian` century year, stops the conversion and is listed. `--calendar-to-gregorian date-or-month-end` moves each such date to the last day of its month instead. If a move puts two steps on the same day, as daily `360_day` data does on 28, 29 and 30 February, the conversion stops again: drop one step of each pair first, for example with xarray, and convert the result.

The store records what was done in its provenance: the source calendar, the option, the change in spacing and the dates that moved. `chronozarr info STORE` prints it as `notes:`, and `chronozarr stac` copies it into the STAC item. In Python it is `chronozarr.open_store("STORE").attrs.provenance["notes"]`. A `provenance=` that you pass to `chronozarr.convert` keeps its fields and gets the note added to its `notes`.

These calendars need cftime. The `netcdf` extra installs it with the NetCDF reader. A Zarr store needs no NetCDF reader, so cftime is also an extra of its own. Add it to the extras that you already use:

```bash
uv tool install --python 3.13 'chronozarr[geo,netcdf,cftime]==0.4.1'
```

In a uv project, run `uv add 'chronozarr[cftime]==0.4.1'`. With pip, install `'chronozarr[cftime]==0.4.1'` in your environment. Without the extra, `convert` stops with `the time coordinate 'time' uses the 360_day calendar, which needs cftime` and these commands.

`chronozarr encode` takes the same option for a file with dimensions `(time, band, y, x)`. `chronozarr append` does not convert calendars, because it cannot record the conversion in the store you append to; convert the whole series instead. The option has no effect on a Gregorian time axis. In Python, `convert` has `calendar_to_gregorian="date"`. `chronozarr.encode` of a `DataArray` whose time coordinate holds cftime dates stops and says so; map the dates yourself, for example with `da.convert_calendar("proleptic_gregorian", align_on="date")`, which drops a date that Gregorian lacks, and describe that in `provenance=`.

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
