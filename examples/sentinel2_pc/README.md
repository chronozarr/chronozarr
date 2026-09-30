# Sentinel-2 ingest example

Builds a chronozarr store from Sentinel-2 L2A scenes on Microsoft Planetary Computer. It is an
example of producing input for `chronozarr.encode`; it is not part of the `chronozarr` package.

## What it does

`ingest.py` runs two phases for one AOI from `aois.yaml`:

1. **Download.** STAC search (`eo:cloud_cover < 80`), then for each calendar month a median
   composite of B02, B03, B04, B08 at 10 m in the AOI's UTM zone. Pixels whose SCL class is not
   4, 5, 6, 7 or 11, or where any band is 0, are excluded from the median. Pixels with no valid
   scene are filled from the previous month's composite (`carry_forward` in `mosaic.py`); a
   pixel with no earlier valid month stays 0 (nodata). Months that already have an `.npz` are
   skipped, so an interrupted download resumes.
2. **Encode.** Stacks the monthly `.npz` files into a `(time, band, y, x)` uint16 array (the
   whole stack is held in memory) and writes it with `chronozarr.encode` using default options.

Planetary Computer asset URLs are signed by `planetary-computer` without an API key.

## Layout

```
<out-dir>/                      default: <repo>/data
  mosaics/<aoi>/YYYY-MM.npz     one file per month
  stores/<aoi>/chronozarr/      chronozarr v0.1 store
```

Each `YYYY-MM.npz` holds:

| key          | dtype   | shape        | content                                        |
| ------------ | ------- | ------------ | ---------------------------------------------- |
| `bands`      | uint16  | (4, H, W)    | monthly median reflectance, 0 = nodata         |
| `coverage`   | float32 | (H, W)       | fraction of the month's scenes valid per pixel |
| `transform`  | float64 | (6,)         | affine coefficients (a, b, c, d, e, f)         |
| `epsg`       | int     | scalar       | EPSG code of the UTM grid                      |
| `band_names` | str     | (4,)         | `B02 B03 B04 B08`                              |

The file name gives the time coordinate (`2024-01.npz` becomes 2024-01-01). All months of an
AOI must share the same grid, CRS and bands; the encode phase raises if they do not.

## Commands

Run from the repository root.

```bash
uv sync --extra ingest

# full archive (2015-07 to 2026-04) for one AOI, download + encode
uv run python examples/sentinel2_pc/ingest.py --aoi sahara_tamanrasset

# one month into a scratch directory
uv run python examples/sentinel2_pc/ingest.py --aoi sahara_tamanrasset \
    --start 2024-01-01 --end 2024-01-31 --out-dir /tmp/ingest_smoke

# encode existing mosaics only
uv run python examples/sentinel2_pc/ingest.py --aoi sahara_tamanrasset --skip-download

# check the result
uv run chronozarr validate data/stores/sahara_tamanrasset/chronozarr
```

AOI names are the keys under `aois:` in `aois.yaml`. A chronozarr store is immutable: if
`stores/<aoi>/chronozarr` already exists the script exits before downloading; delete the
directory to re-encode.
