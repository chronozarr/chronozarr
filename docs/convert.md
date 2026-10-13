# Convert your data

`convert` is the command for rasters on disk or in S3. Point it at a directory, a quoted glob or an `s3://` prefix of GeoTIFFs. Each file name holds one date: `20240131`, `2024-01-31` or `2024-01`.

```bash
chronozarr convert "scenes/*.tif" my_store --dry-run   # list files and dates, check every file, write nothing
chronozarr convert "scenes/*.tif" my_store
```

The dry run runs the same discovery and preflight checks as conversion, then prints each file with its date and the planned grid, bands, validity and size. It writes no store. A directory or `s3://` prefix is not searched recursively; use a glob such as `scenes/**/*.tif` for subdirectories. Listing an S3 prefix needs the `s3` extra (`uv add 'chronozarr[geo,s3]==0.4.0'`) and uses your AWS credentials.

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

Nothing is resampled or rescaled unless you ask. Files on another grid fail with a suggestion to pass `--resampling`.

The store takes each band's description (its name), scale, offset and units from the files, so a Sentinel-2 file with a scale of 0.0001 gives reflectance. These must be identical in every file. It also takes the nodata value that the files declare; files that declare none give a store with no nodata, so a stored 0 is data. `chronozarr convert --help` lists the manifest, Zarr and NetCDF sources and the validity rules.

`chronozarr encode "scenes/*.tif" my_store` is the in-memory route for GeoTIFFs on one grid. It reads uint8, uint16, int16 and float32 files by the same rules, holds the whole stack in memory and never resamples. It points you to `convert` for directories, S3 prefixes and manifests.

## Check the store

```bash
chronozarr validate my_store
chronozarr info my_store
```

`validate` checks the store against the [spec](../spec/CHRONOZARR.md). `info` prints the times, bands and levels.

## Read the values

Read the values in Python.

```python
import chronozarr

store = chronozarr.open_store("my_store")
store.read(t=42)              # (band, y, x), exact stored values
da = store.to_xarray(lod=0)   # xarray DataArray, loaded into memory
```

The input must be on an EPSG grid with north up. To write a store from an xarray `DataArray`, see [How it works](how-it-works.md#write-a-store-from-python).

## Next

- [Preview a store](preview.md) on your computer.
- [Share a local store instantly](share.md).
- [Publish to cloud storage](publish.md#chronozarr-publish).
