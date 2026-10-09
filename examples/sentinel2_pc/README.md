# Sentinel-2 ingest example

This example builds a chronozarr store of monthly Sentinel-2 median composites from Microsoft Planetary Computer for one area of interest (AOI). It can also write a static STAC catalog next to the store. The example lives outside the `chronozarr` package. The `planetary-computer` package signs the asset URLs, so you need network access but no account and no API key. The encode step holds every month in memory at 2 bytes per month, band and pixel. One month of the Ucayali AOI (4 bands of 2,765 by 2,759 pixels) takes 0.06 GB.

## Run it

Run the commands from the repository root. The AOIs are the keys under `aois:` in `aois.yaml`: `nile_delta`, `rondonia_brazil`, `lake_mead`, `mekong_delta` and `ucayali_santa_maria`.

1. Install the extra.

   ```bash
   uv sync --extra ingest
   ```

2. Run one of these commands. The last two read the monthly files from an earlier download. Use them when those files exist and the store does not.

   ```bash
   # full archive (2015-07 to 2026-04) for one AOI, download + encode
   uv run python examples/sentinel2_pc/ingest.py --aoi ucayali_santa_maria

   # one month into a scratch directory
   uv run python examples/sentinel2_pc/ingest.py --aoi ucayali_santa_maria \
       --start 2024-01-01 --end 2024-01-31 --out-dir /tmp/ingest_smoke

   # encode existing mosaics only
   uv run python examples/sentinel2_pc/ingest.py --aoi ucayali_santa_maria --skip-download

   # encode existing mosaics and write the STAC catalog beside the store
   uv run python examples/sentinel2_pc/ingest.py --aoi ucayali_santa_maria --skip-download --stac
   ```

3. Check the store.

   ```bash
   uv run chronozarr doctor data/stores/ucayali_santa_maria/chronozarr
   ```

`--start` and `--end` change the date range, and `--out-dir` changes the root of `mosaics/` and `stores/`.

## What the script does

The download phase searches the Planetary Computer STAC API for scenes with `eo:cloud_cover < 80`. For each calendar month it builds a median composite of B02, B03, B04 and B08 at 10 m, in the UTM zone of the AOI. The median skips a pixel when any band is 0 or when its SCL class is not 4, 5, 6, 7 or 11. SCL is the Scene Classification Layer. A pixel with no valid scene takes the value from the previous month's composite (`carry_forward` in `mosaic.py`). A pixel with no earlier valid month stays 0, which is nodata. A month whose `.npz` file exists is skipped, so an interrupted download resumes.

The encode phase stacks the monthly files into a `(time, band, y, x)` uint16 array. It writes the array with `chronozarr.encode` and the default options, which store true values. Each band has a name, a `common_name` (blue, green, red or nir) and a scale of 0.0001. The `coverage` plane is 1 where at least one scene was valid and 0 where the value is carried forward or missing. It is a flag with no scene count. The `provenance` record names the collection, the `composite` ("monthly median") and the `gap_fill` ("carry-forward"). It also has notes on the cloud mask and the baseline 04.00 offset correction. With `--stac`, the script also writes the static STAC Collection and Item that `chronozarr stac` writes.

```
<out-dir>/                      default: <repo>/data
  mosaics/<aoi>/YYYY-MM.npz     one file per month
  stores/<aoi>/chronozarr/      chronozarr store
  stores/<aoi>/stac/            with --stac: collection.json and <aoi>-sentinel-2-monthly/*.json
```

Each `YYYY-MM.npz` file holds `bands` (uint16, shape (4, H, W), 0 = nodata) and `coverage` (float32, the fraction of the month's scenes that are valid). It also holds `transform` (6 affine coefficients), `epsg` and `band_names` (`B02 B03 B04 B08`). The file name gives the time coordinate, so `2024-01.npz` becomes 2024-01-01. All months of an AOI must share the same grid, CRS and bands, or the encode phase raises an error. A store is immutable. If `stores/<aoi>/chronozarr` exists, the script exits before it downloads anything, so delete the directory to encode again. To host the store, see [hosting](../../docs/hosting.md).
