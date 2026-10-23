# Troubleshooting

**Package version — 0.5.1.** Follow the [installation guide](quickstart.md#1-install) before using this reference. The [release notes](release-0.5.1.md) describe compatibility and changes since 0.4.1.

Find the step where the problem appears. Each row gives the message or symptom, its cause and the fix.

## Installation

| Message or symptom | Cause and fix |
|---|---|
| `No solution found when resolving dependencies` and `Python>=3.11` | uv chose an older Python that comes first on your computer. Add `--python 3.13` to `uv tool install` or `uv init` |
| `chronozarr: command not found` after `uv tool install` | uv's tool folder is not on your `PATH`. Run `uv tool update-shell`, then open a new terminal |
| `this command needs rasterio` | The `geo` extra is missing. Include it with the extras you use in the [development checkout installation](development.md). Use the local-checkout dependency for a Python project or notebook |
| `Connection refused` for chronozarr.org or data.chronozarr.org, or a security block page, on a university or company network or a VPN | The network's DNS filter blocks the domain. Try another network, or ask your IT team to allow chronozarr.org |
| `chronozarr --version` prints an older version | An older install comes first on your `PATH`, or a pinned environment is active. Check `which chronozarr` |
| The first command after an install takes 10 to 25 seconds | On macOS the system checks new program files once. Later commands start in about a second |

## Conversion

`chronozarr convert SOURCE OUT --dry-run` reports every problem in every file at once, with a fix for each, and writes nothing. Run it first.

| Message | Cause and fix |
|---|---|
| `no date in the file name` | Put one date in each name, as `20240131`, `2024-01-31` or `2024-01`. Or pass `--date-pattern`, such as `--date-pattern "ndvi_%Y%m%d"` (`%Y%j` reads a year and day of year, as in MODIS and HLS names: `--date-pattern "MOD13A1.A%Y%j"`), or list the files in a [manifest](convert.md#from-a-manifest-of-urls) |
| `no date in the file name` for one multi-band file, and the `fix:` names `--bands-are-dates` | The bands of the file are dates, as in an Earth Engine `toBands()` export. Pass `--bands-are-dates`. See [From Earth Engine exports](convert.md#from-earth-engine-exports) |
| `has the same date` for several files, and the `fix:` names `--mosaic-tiles` | The files of each date are tiles of one image, as Earth Engine splits a large export into `name-0000000000-0000000000.tif` files. Pass `--mosaic-tiles`; do not leave files out, or the store holds part of the area. See [Exports split into tiles](convert.md#exports-split-into-tiles) |
| `has the same date` and `covers the same pixels` | The files are copies, or two images of one area. Keep one, or read a time of day from the names with `--date-pattern` and `%H%M%S` |
| `several dates` in one name | Pass `--date-pattern` to say which part is the date |
| A month name, such as `scene_Jan2024.tif` | `--date-pattern` reads numbers only. Write a CSV manifest with the columns `uri,datetime`, one row per file, and convert the manifest |
| `Got unexpected extra arguments` | The shell expanded an unquoted glob into many files. Quote it: `"scenes/*.tif"` |
| The bands are named `1`, `2` | The GeoTIFFs have no band descriptions. Add a `bands` column to a manifest, as in [Convert your data](convert.md#from-dated-geotiffs) |
| Two files `share the datetime` | Each timestep needs one file. Remove or rename the duplicate |
| A file on another grid or CRS | All timesteps of a store share one grid. Fix the files, or pass `--resampling` to warp every file to the grid of the first |
| Different scale, offset, units or band names | A store holds one value of each per band. Correct the metadata, for example with `gdal_edit.py -scale -offset -units`. For a scale or offset, `--scale` and `--offset` give one value for every band instead |
| A float file with undeclared NaN | Pass `--nodata nan` to treat NaN as invalid |
| `-9999 (48 % of the valid pixels in the 3 sampled timesteps) lies far outside the other values`, and the command stops | The files declare no nodata, and a value that looks like a fill holds a large share of the pixels. If it marks missing pixels, pass `--nodata -9999`. If it is data, pass `--nodata none`. `--nodata` takes one value: with two flag values the message says which stays data and how to mark both |
| A `fill:` line names a value such as `-9999` | The files declare no nodata, and the value looks like a fill but holds a small share of the pixels. Pass `--nodata -9999` to mark it invalid. Leave it out if the value is data |
| `500m 16 days NDVI declares scale 10000 and would run -2e+07 to 1e+08`, and the command stops | The file's scale is a divisor, as in MODIS and other HDF-EOS products, and the store multiplies. Pass `--scale 0.0001`. If 10000 is right, pass `--scale 10000` |
| A `range:` line shows values far outside the physical range, and the command did not stop | The scale is applied as stored * scale + offset. Pass `--scale` and `--offset` with the right values, or correct the files, for example with `gdal_edit.py -scale -offset` |
| A palette PNG is refused | Expand it to RGB first: `gdal_translate -expand rgba` |
| The output folder exists | A store is never overwritten. Choose a new folder |
| `reading a NetCDF4 file needs h5netcdf and h5py`. Older releases print xarray's `found the following matches with the input file in xarray's IO backends`, or `No module named 'h5py'` | Include `netcdf` in the [installation guide](quickstart.md#1-install). A classic NetCDF3 file (it starts with `CDF`) needs `scipy` instead: add `--with scipy` to the checkout's tool install, or add scipy to the analysis environment. See [From NetCDF or Zarr](convert.md#from-netcdf-or-zarr) |
| `the time coordinate 'time' uses the 360_day calendar, which needs cftime` (also `noleap`, `all_leap`, `julian`). Older releases print xarray's `unable to decode time units ... installing cftime` | Include `cftime` or `netcdf` in the [installation guide](quickstart.md#1-install). Package 0.4.1 has no `cftime` extra or calendar-mapping option. See [Climate model calendars](convert.md#climate-model-calendars) |
| `uses the 360_day calendar, and a store holds Gregorian timestamps`. Older releases print `the time coordinate 'time' is object, not datetimes` | The time axis is on a calendar with no Gregorian equivalent. Pass `--calendar-to-gregorian date` to keep each step's date and time of day as its Gregorian timestamp. The message says what that does to the spacing between steps, and the store records it. See [Climate model calendars](convert.md#climate-model-calendars) |
| `2 of 780 dates that Gregorian does not have` | The calendar has days Gregorian lacks, such as 30 February on a `360_day` calendar. `--calendar-to-gregorian date-or-month-end` moves each to the last day of its month and records which |
| `both land on` or `on or before the previous step`, with `date-or-month-end` | Moving the dates put two steps on one day, as daily `360_day` data does on 28, 29 and 30 February. Drop one step of each pair first, for example with xarray, then convert the result |
| `listing an S3 prefix needs boto3` | Install the `s3` extra. The message gives the command |

`convert --help` lists every source type, option and validity rule. [Convert your data](convert.md) explains them.

## Opening a store

A store whose CRS has no EPSG code, such as a Lambert conformal conic or a sinusoidal grid, declares `spec_version` `0.4.0`. A store whose CRS has an EPSG code declares `0.3.0`; package 0.3 and later open supported v0.3 layouts. Releases up to 0.4.1 refuse a `0.4.0` store, and their message names the wrong fix.

| Message | Cause and fix |
|---|---|
| `chronozarr.spec_version: unsupported or missing version '0.4.0'; expected 0.3.0; use chronozarr convert before opening with a v0.3 reader` | The installed chronozarr is 0.4.1 or older and cannot read this custom-CRS store. Install 0.5.1 with the [installation guide](quickstart.md#1-install), then check that `chronozarr --version` reports `0.5.1`. Keep the existing store; it needs no conversion |
| `not a valid chronozarr store: unsupported spec_version 0.4.0; use chronozarr convert to convert this store to v0.3` in the viewer or the JavaScript package | Install the npm 0.5.1 [viewer bundle](viewer-distribution.md). The hosted viewer changes when the site is deployed; a Python package install does not update it. An npm 0.4.1 viewer needs a package upgrade to read this store |
| `unsupported or missing version '0.2.0'` | The store is v0.2. `chronozarr convert OLD NEW` writes a v0.3 store from it |
| `unsupported or missing version` with a version later than 0.4.0 | The store was written by a newer chronozarr. Upgrade chronozarr |
| `cannot read the CRS ... Pass an authority code such as EPSG:32631 or WKT2 text` | `encode` was given a CRS it could not parse. Pass `EPSG:xxxxx`, or WKT2 text. WKT1, PROJJSON and PROJ strings need pyproj to be converted: install it, or convert the CRS to WKT2 yourself |
| `crs: the input is ..., the store is ...` from `append` | The new data is in another CRS than the store. Append data on the store's grid. A code and a WKT2 can be compared only when pyproj is installed |

## The viewer

| Symptom | Cause and fix |
|---|---|
| Only Single band is offered | The viewer does not know which band is red, green, blue or near infrared, or which two are VV and VH or HH and HV. Set the roles with `chronozarr bands STORE --band-role NAME=ROLE`. See [Band roles](python.md#band-roles-and-the-first-view) |
| `Store on this computer not reachable` | The viewer page is on the internet and the store on your computer. Check that `preview` still runs, allow the page to reach your local network when Chrome asks, or use Chrome or Firefox instead of Safari |
| `Shared link not available` | The temporary link from `chronozarr share` has stopped. Ask the sender for a new one |
| True color looks white or black | The bands may lack their scale. A Sentinel-2 band stored as 0 to 10000 needs a scale of 0.0001. Check with `chronozarr bands STORE` |
| A single band looks flat | Its display range does not fit the data. Click Auto, or set the minimum and maximum |
| Hatched or flat areas in some months | Gaps hatches zero coverage. At level 0 this records an observation gap; at overview levels the rounded count can also become zero. Inspect the native level and validity mask before interpreting the area |
| The map stays empty and an error names CORS | The host does not send `Access-Control-Allow-Origin`. Run `chronozarr doctor URL`. See [Publish a permanent link](publish.md) |
| `Open` reports that the store cannot be reached | Check the address. It must be the store, the folder that holds the root `zarr.json`, and start with `https://` |

## Preview

| Symptom | Cause and fix |
|---|---|
| No browser opens | Open the address after `open:` in the terminal yourself. `--no-open` prints it without trying |
| Chrome asks to let the page reach devices on your local network | The viewer page is on chronozarr.org and the store is on your computer. Allow it |
| Safari shows nothing | Safari can block an https page from reading `http://127.0.0.1`. Use Chrome or Firefox, or a [self-hosted viewer](viewer-distribution.md) with `--viewer-dir` |
| `--port N` fails | Another program listens on that port. Leave out `--port`, or choose another number |

## Share

| Symptom | Cause and fix |
|---|---|
| `cloudflared` not found | Install it from [Cloudflare's download page](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/). On macOS: `brew install cloudflared` |
| `cloudflared exited ...` | cloudflared could not reach Cloudflare. Check the network connection or firewall, and run the command again |
| `the tunnel did not serve this store within 90 seconds` | The new address did not start working in time. Run the command again |
| The recipient sees nothing | Sharing stopped, or your computer slept or went offline. The link works only while `share` runs |
| Scrubbing is slow for the recipient | Every view travels over your upload connection. For many viewers, [publish](publish.md) the store |

## Publish

| Symptom | Cause and fix |
|---|---|
| `access denied` | The credentials cannot write to the bucket. The message names the permission they need |
| The objects are stored, but `doctor` gets 403 at the public URL | The bucket is not public. Publishing never changes public access. Make the bucket readable, or put a CDN in front of it and pass its address as `--public-url` |
| `doctor` gets 404 for the root `zarr.json` | The public URL does not point at the store. It must be the folder that holds the root `zarr.json` |
| `CORS` fails | Rerun with `--apply-cors`, or add the rule from the [hosting recipes](hosting-providers.md) |
| The prefix already holds other objects | Each version of a store needs a new prefix. See [Immutable prefixes](hosting-requirements.md#immutable-prefixes-and-upload-order) |
| The upload stopped | Run the same command again. It skips the objects already stored |

## Notebooks

| Symptom | Cause and fix |
|---|---|
| The viewer frame stays empty | Run `chronozarr.diagnose_view("my_store")`. It prints what the browser requested and what to try next |
| The kernel runs on another machine | The browser must reach the kernel's machine. See [Notebooks and remote machines](notebooks.md#remote-notebooks) |

## Report a problem

Open an issue at [github.com/chronozarr/chronozarr/issues](https://github.com/chronozarr/chronozarr/issues). Include the command, its full output, `chronozarr --version`, and the output of `chronozarr doctor` for a hosted store.
