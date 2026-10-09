# Sentinel-2 ingest example

This example builds a chronozarr store of monthly Sentinel-2 composites from Microsoft Planetary Computer. It shows how to produce the input for `chronozarr.encode` from a public archive. The example lives outside the `chronozarr` package.

For one area of interest (AOI) from `aois.yaml`, the script downloads Sentinel-2 L2A scenes and builds a cloud-masked median composite for each calendar month. It then encodes the months as a store. It can also write a static STAC catalog next to the store.

## What you need

- A checkout of this repository, with the `ingest` extra installed.
- Network access to Planetary Computer. The `planetary-computer` package signs the asset URLs, so you need no account and no API key.
- Memory for the whole stack. The encode step holds every month in memory, at 2 bytes per month, band and pixel. One month of the Ucayali AOI (4 bands of 2,765 by 2,759 pixels) takes 0.06 GB.

## Steps

Run the commands from the repository root.

1. Install the extra.

   ```bash
   uv sync --extra ingest
   ```

2. Pick an AOI. The names are the keys under `aois:` in `aois.yaml`.

   | Name | Area |
   |------|------|
   | `nile_delta` | Nile Delta (Kafr El Sheikh, Egypt) |
   | `rondonia_brazil` | Rondônia deforestation (Brazil) |
   | `lake_mead` | Lake Mead (NV/AZ, USA) |
   | `mekong_delta` | Mekong Delta (Vietnam) |
   | `ucayali_santa_maria` | Ucayali River, Santa María reach (Peru) |

3. Run one of these commands. Each one uses `ucayali_santa_maria` as the example AOI. The last two read the monthly files from an earlier download. Use them when those files exist and the store does not.

   Download and encode the full archive, from 2015-07 to 2026-04:

   ```bash
   uv run python examples/sentinel2_pc/ingest.py --aoi ucayali_santa_maria
   ```

   Download and encode one month into a scratch directory:

   ```bash
   uv run python examples/sentinel2_pc/ingest.py --aoi ucayali_santa_maria \
       --start 2024-01-01 --end 2024-01-31 --out-dir /tmp/ingest_smoke
   ```

   Encode the mosaics that are already on disk:

   ```bash
   uv run python examples/sentinel2_pc/ingest.py --aoi ucayali_santa_maria --skip-download
   ```

   Encode the mosaics and write the STAC catalog beside the store:

   ```bash
   uv run python examples/sentinel2_pc/ingest.py --aoi ucayali_santa_maria --skip-download --stac
   ```

4. Check the store.

   ```bash
   uv run chronozarr doctor data/stores/ucayali_santa_maria/chronozarr
   ```

Use `--start` and `--end` to change the date range. Use `--out-dir` to change the root of `mosaics/` and `stores/`.

## What the script does

### Download

The script searches the Planetary Computer STAC API for scenes with `eo:cloud_cover < 80`. For each calendar month it builds a median composite of B02, B03, B04 and B08 at 10 m, in the UTM zone of the AOI.

The median skips a pixel when any band is 0 or when its SCL class is not 4, 5, 6, 7 or 11. SCL is the Scene Classification Layer. A pixel with no valid scene takes the value from the previous month's composite (`carry_forward` in `mosaic.py`). A pixel with no earlier valid month stays 0, which is nodata.

A month whose `.npz` file exists is skipped, so an interrupted download resumes.

### Encode

The script stacks the monthly `.npz` files into a `(time, band, y, x)` uint16 array. It writes the array with `chronozarr.encode` and the default options, which store true values. The store also records band metadata, a `coverage` plane and provenance.

Each band has a name, a `common_name` (blue, green, red or nir) and a scale of 0.0001. Reflectance is the stored value times 0.0001.

The `coverage` plane is a uint8 plane for each pixel and month. It is 1 where at least one scene was valid and 0 where the value is carried forward or missing. The monthly files hold the valid fraction of scenes, so the plane is a flag with no scene count. Use it to tell observed pixels from carried-forward pixels.

The `provenance` record names the Planetary Computer collection, the `composite` ("monthly median") and the `gap_fill` ("carry-forward"). It also holds notes on the cloud mask and the baseline 04.00 offset correction.

### STAC catalog

With `--stac`, the script writes a static STAC Collection and Item next to the store. The output is the same as `chronozarr stac`. It holds the extent, the Zarr asset, the bands, the datacube extension and the provenance.

## What you get

```
<out-dir>/                      default: <repo>/data
  mosaics/<aoi>/YYYY-MM.npz     one file per month
  stores/<aoi>/chronozarr/      chronozarr store
  stores/<aoi>/stac/            with --stac: collection.json and <aoi>-sentinel-2-monthly/*.json
```

Each `YYYY-MM.npz` file holds these arrays.

| Key | dtype | Shape | Content |
|-----|-------|-------|---------|
| `bands` | uint16 | (4, H, W) | Monthly median reflectance, 0 = nodata |
| `coverage` | float32 | (H, W) | Fraction of the month's scenes that are valid at each pixel |
| `transform` | float64 | (6,) | Affine coefficients (a, b, c, d, e, f) |
| `epsg` | int | scalar | EPSG code of the UTM grid |
| `band_names` | str | (4,) | `B02 B03 B04 B08` |

The file name gives the time coordinate: `2024-01.npz` becomes 2024-01-01. To host the store, see [hosting](../../docs/hosting.md).

## Limits

- All months of an AOI must share the same grid, CRS and bands. The encode step raises an error if they differ.
- A store is immutable. If `stores/<aoi>/chronozarr` exists, the script exits before it downloads anything. Delete the directory to encode again.
- The encode step holds the whole stack in memory.
