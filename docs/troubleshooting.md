# Troubleshooting

Find the step where the problem appears. Each row gives the message or symptom, its cause and the fix.

## Installation

| Message or symptom | Cause and fix |
|---|---|
| `No solution found when resolving dependencies` and `Python>=3.11` | uv chose an older Python that comes first on your computer. Add `--python 3.13` to `uv tool install` or `uv init` |
| `chronozarr: command not found` after `uv tool install` | uv's tool folder is not on your `PATH`. Run `uv tool update-shell`, then open a new terminal |
| `this command needs rasterio` | The `geo` extra is missing. Install it the way you installed chronozarr: `uv tool install --python 3.13 'chronozarr[geo]'`, listing every extra you use at once, such as `'chronozarr[geo,publish]'`; `uv add 'chronozarr[geo]'` in a uv project; or pip with `'chronozarr[geo]'` |
| `Connection refused` for chronozarr.org or data.chronozarr.org, or a security block page, on a university or company network or a VPN | The network's DNS filter blocks the domain. Try another network, or ask your IT team to allow chronozarr.org |
| `chronozarr --version` prints an older version | An older install comes first on your `PATH`, or a pinned environment is active. Check `which chronozarr` |
| The first command after an install takes 10 to 25 seconds | On macOS the system checks new program files once. Later commands start in about a second |

## Conversion

`chronozarr convert SOURCE OUT --dry-run` reports every problem in every file at once, with a fix for each, and writes nothing. Run it first.

| Message | Cause and fix |
|---|---|
| `no date in the file name` | Put one date in each name, as `20240131`, `2024-01-31` or `2024-01`. Or pass `--date-pattern`, such as `--date-pattern "ndvi_%Y%m%d"`, or list the files in a [manifest](convert.md#from-a-manifest-of-urls) |
| `several dates` in one name | Pass `--date-pattern` to say which part is the date |
| A month name, such as `scene_Jan2024.tif` | `--date-pattern` reads numbers only. Write a CSV manifest with the columns `uri,datetime`, one row per file, and convert the manifest |
| `Got unexpected extra arguments` | The shell expanded an unquoted glob into many files. Quote it: `"scenes/*.tif"` |
| The bands are named `1`, `2` | The GeoTIFFs have no band descriptions. Add a `bands` column to a manifest, as in [Convert your data](convert.md#from-dated-geotiffs) |
| Two files `share the datetime` | Each timestep needs one file. Remove or rename the duplicate |
| A file on another grid or CRS | All timesteps of a store share one grid. Fix the files, or pass `--resampling` to warp every file to the grid of the first |
| Different scale, offset, units or band names | A store holds one value of each per band. Correct the metadata, for example with `gdal_edit.py -scale -offset -units` |
| A float file with undeclared NaN | Pass `--nodata nan` to treat NaN as invalid |
| A palette PNG is refused | Expand it to RGB first: `gdal_translate -expand rgba` |
| The output folder exists | A store is never overwritten. Choose a new folder |
| `listing an S3 prefix needs boto3` | Install the `s3` extra. The message gives the command |

`convert --help` lists every source type, option and validity rule. [Convert your data](convert.md) explains them.

## The viewer

| Symptom | Cause and fix |
|---|---|
| Only Single band is offered | The viewer does not know which band is red, green, blue or near infrared. Set the roles with `chronozarr bands STORE --band-role NAME=ROLE`. See [Band roles](python.md#band-roles-and-the-first-view) |
| `Store on this computer not reachable` | The viewer page is on the internet and the store on your computer. Check that `preview` still runs, allow the page to reach your local network when Chrome asks, or use Chrome or Firefox instead of Safari |
| `Shared link not available` | The temporary link from `chronozarr share` has stopped. Ask the sender for a new one |
| True color looks white or black | The bands may lack their scale. A Sentinel-2 band stored as 0 to 10000 needs a scale of 0.0001. Check with `chronozarr bands STORE` |
| A single band looks flat | Its display range does not fit the data. Click Auto, or set the minimum and maximum |
| Hatched or flat areas in some months | No observation covered those pixels, and the value was carried over from an earlier month. Click Gaps to show them |
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
