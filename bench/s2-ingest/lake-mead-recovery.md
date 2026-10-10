# Lake Mead recovery

Findings from 2026-10-10 on the 127 Lake Mead monthly mosaics in `data/mosaics/lake_mead` (written 2026-04-19), and what it takes to replace them. The machine-readable list is `lake-mead-recovery.json`. Nothing under `data/` was modified and no ingest was run; the checks below used STAC metadata, 23 product-metadata XML files, 4 full-AOI SCL reads and a few 256 x 256 B04 windows.

## Findings

### 1. The +1000 offset is real and the stored months do not remove it

STAC `s2:processing_baseline` agrees with `MTD_MSIL2A.xml` for all 23 scenes checked (one or two per baseline and era). `BOA_QUANTIFICATION_VALUE` is 10000 everywhere.

| Scene (acquisition date) | Baseline | `BOA_ADD_OFFSET` |
|---|---|---|
| 2015-08-10, 2016-04-03, 2017-01-31, 2018-01-01, 2019-01-01, 2019-03-02, 2020-01-06, 2021-01-05 | 02.12 | absent |
| 2015-12-18, 2016-01-07, 2017-01-01, 2019-06-10, 2021-06-24, 2021-12-11, 2022-01-05 | 03.00 | absent |
| 2021-12-01 (processed 2023-01-29), 2022-01-25, 2022-02-04 | 04.00 | -1000 (13 bands) |
| 2019-03-17 (processed 2022-11-18) | 05.00 | -1000 |
| 2022-02-24, 2023-03-26, 2024-07-23, 2026-02-08 | 05.10, 05.09, 05.11, 05.12 | -1000 |

The rule is by processing baseline, not by acquisition date. ESA reprocessed some old acquisitions at baseline 04.00 or later, and Planetary Computer serves them: 4 scenes of 2019-03 (05.00) and 4 of 2021-12 (04.00) carry the offset. Baselines in the fresh search: 02.12 and 03.00 up to 2021-11, 04.00 from 2021-12, 05.xx from 2022-02.

`catalog.py` is correct. `SceneRef.boa_offset` returns -1000 for `processing_baseline >= 4.0`, `_processing_baseline` raises when the property is missing, and `mosaic.apply_scene_mask` adds the offset to non-zero DN, clips to 1..65535 and keeps 0 as nodata. I found no defect in the current logic. The dedup (one scene per date and tile, greatest `item_id`, that is the latest processing) keeps the reprocessed twin; its own baseline then drives the offset.

Pixel check. B04, window rows 512:768, cols 640:896 of the AOI grid (a bright desert target, 65,536 px). Each row is the per-pixel median of SCL-valid scenes read from the COGs, compared with the stored mosaic. "Raw" is no offset, "corrected" is what `mosaic.py` now stores.

| Month | Scenes read | Baselines | Stored median B04 | Fresh raw | Fresh corrected | Stored = raw | Stored = corrected |
|---|---|---|---|---|---|---|---|
| 2021-06 | 12 | 02.12, 03.00 | 2765 | 2765 | 2765 | 100 % | 100 % |
| 2022-06 | 12 | 04.00, 05.10 | 3882 | 3882 | 2882 | 100 % | 0 % |
| 2022-01 | 9 | 03.00, 04.00 | 2338 | 2338 | 2222 | 100 % | 23 % |
| 2019-03 | 7 | 02.12, 05.00 | 2053 | 2053 | 2042 | 100 % | 89 % |
| 2021-12 | 10 | 03.00, 04.00 | 2281 | 2281 | 2200 | 100 % | 20 % |
| 2025-06 | 10 | 05.11 | 3812 | 3828 | 2828 | 1 % | 0 % |

2025-06 is a carry-forward copy of an older month (coverage 0 in the window), so it matches no fresh median, but it sits 981 DN above the corrected value: the carried source is offset too. Corrected 2022-06 (2882) is within 5 % of 2021-06 (2765), as seasonal reflectance should be.

Timeline. The first pipeline commit in this repo (`4adef88`, 2026-09-29) had no offset handling; `f079e72` (2026-09-30) added it. The mosaics date from 2026-04-19, so they predate the fix by five months. `7c2962b` (2026-10-01) added `--boa-offset-from` to the water-mask builder as a workaround for these files.

### 2. Missing scenes

The audit (`audit-lake_mead.json`, 2,318 scenes) is reproduced by a fresh search with the exact `search_scenes_by_month` query: 2,599 items, 2,318 after dedup, and every month's count equals the audit's `n`.

- 674 scenes with valid pixels are missing in 34 months from 2023-06.
- The first audit write-up split these into 24 empty and 10 partial months (corrected in `docs/evidence.md`). In fact, in all 33 months from 2023-07 to 2026-03 no scene contributed to the stored month. In the nine months the evidence section calls partial (2023-08, 2023-12, 2024-01, 2024-04, 2024-06, 2025-03, 2025-08, 2025-11, 2026-01) every scene that has a valid pixel is missing and the rest have none. `water-1.months.csv` agrees: all 33 are "skipped: no valid pixel".
- Only 2023-06 is a true partial month: 20 of 24 scenes missing, 4 contribute at most, and 2.28 million pixels of shortfall are unattributed.

### 3. Carry-forward chains

A month's gaps (pixels with no valid scene) take the previous written month's composite, itself possibly carried (`mosaic.py`, gap fill from `previous_month`). A month whose own scenes are fine therefore inherits damage in its gap pixels. Pixel-level propagation, from the stored mosaics:

`contaminated[m] = own_damage[m] | (carried[m] & contaminated[m-1])`, with `carried` = coverage 0 and a non-zero value.

The 4 offset scenes of 2019-03 (05.00) have valid pixels over 94 % of the AOI (SCL read for those 4 scenes), so 2019-03 is wholly suspect. Its damage then reaches carried pixels in every later month through 2021-11: 50,520 px (0.77 %) in 2019-04, 3,112 px in 2021-01, 696 px (0.01 %) in 2021-11. Those 32 months are `carry_forward_chain` in the manifest. The bound is pixel-exact only to the extent that "a baseline-05.00 scene was valid here" means the median changed, so it is an upper bound.

From 2021-12 every month is defective on its own account (offset), so the chain adds nothing there. Result: the affected months form one run, 2019-03 to 2026-03.

### 4. The water-1 store is partly compensated, not correct

`data/stores/lake_mead/water-1` (built 2026-10-01) has 94 months written and 33 skipped. Its notes say 1000 DN was subtracted from months >= 2022-02. That is valid for months made only of baseline >= 04.00 scenes, but it leaves 2019-03, 2021-12 and 2022-01 (mixed baselines, where the median is not a simple shift), keeps the chain pixels of 2019-04 to 2021-11, and 2023-06 stands on at most 4 of 24 scenes. Everything from 2023-07 is missing from the store. It must be rebuilt from recovered mosaics, without `--boa-offset-from`.

## Manifest summary

| | |
|---|---|
| Months on disk | 127 |
| Unaffected (2015-08 to 2019-02) | 42 |
| To rebuild (2019-03 to 2026-03, contiguous) | 85 |
| `boa_offset_uncorrected` | 53 (2019-03, 2021-12, 2022-01 to 2026-03) |
| `carry_forward_only` | 33 (2023-07 to 2026-03) |
| `missing_scenes` | 1 (2023-06; 20 scenes) |
| `carry_forward_chain` only | 32 (2019-04 to 2021-11) |
| Scenes in the rebuild months | 1,738 |
| Estimated transfer | 29.7 to 40.6 GB |

Transfer method. Assets are whole-tile COGs, about 1 GB of B02 + B03 + B04 + B08 per scene (HEAD of 4 scenes: 858 to 1,108 MB; STAC has no `file:size`), but the pipeline reads only the 22 x 28 km window. Bytes per scene = fraction of the AOI inside the scene's footprint x C. C is calibrated on the `lakemead-1m` benchmark (2024-01, 16 scenes, summed footprint fraction 7.94): 35.8 MB with SCL-first block skipping (284.1 MB downloaded) and 48.9 MB without it (388.3 MB, the baseline pipeline). The low figure is the new pipeline in a cloudy month; a clear month skips fewer blocks, so expect the figure between the two. A rebuilt month needs all of its scenes, not only the missing ones, because a median cannot be patched. At the 12 to 18 MB/s measured on the home link, expect roughly 40 to 60 minutes of transfer.

Smaller alternative: skip the 32 chain months (11.3 to 15.5 GB). Rebuild 2019-03 on its own, then 2021-12 to 2026-03. That leaves up to 0.8 % wrong pixels in 2019-04 to 2021-11. I do not recommend it: the saving is a third of the transfer, and the damaged pixels would stay undocumented in the files.

## Rebuild procedure (not run)

Build in a new directory outside `data/`. Existing months are skipped by the pipeline, so the directory must hold only the 42 unchanged months.

```bash
set -euo pipefail
R=$HOME/projects/chronozarr-recovery            # any empty directory outside data/
mkdir -p "$R/mosaics/lake_mead"

# 1. seed the unchanged months (copies; the source is read only)
uv run python - <<'EOF' | while read -r m; do cp -n "data/mosaics/lake_mead/$m.npz" "$R/mosaics/lake_mead/"; done
import json
for r in json.load(open("bench/s2-ingest/lake-mead-recovery.json"))["months"]:
    if r["action"] == "none":
        print(r["month"])
EOF

# 2. download 2019-03 to 2026-03; 2019-02 is only the carry-forward source
uv sync --extra ingest
uv run python examples/sentinel2_pc/ingest.py --aoi lake_mead \
    --start 2019-02-01 --end 2026-03-31 --out-dir "$R" --diagnostics

# 3. check: audit the new files against the scenes (SCL only), expect 0 missing
uv run python examples/sentinel2_pc/audit_scenes.py --aoi lake_mead --out-dir "$R" \
    --report "$R/audit-lake_mead.json"

# 4. water-mask store from the corrected mosaics: no --boa-offset-from
uv run python examples/water_masks/build_water_stack.py --aoi lake_mead \
    --mosaic-root "$R/mosaics" --out-root "$R/stores" --reports-dir "$R/reports"
```

Notes.

- `--start 2019-02-01` is deliberate. Only months inside the search range count as carry-forward sources, so starting at 2019-03 would leave the first month with no previous month and zero where the old files carried values.
- `--end 2026-03-31` keeps the range of the old files; the default end (2026-04-01) adds a one-day 2026-04.
- `ingest.py` also encodes `$R/stores/lake_mead/chronozarr` (an imagery store that does not exist today). Interrupt after the download if it is not wanted; with `--keep-going` absent the run stops at the first unreadable scene and can be re-run to resume.
- `check_water_stack.py` has no root option (it reads `data/` only), so it cannot check the store in `$R`. Run it as `check_water_stack.py --aoi lake_mead --month 2023-06` after the approved swap, without `--boa-offset-from`.
- After step 2, compare the new files with the stored ones: months 2019-03 onward should differ (offset), and 2015-08 to 2019-02 are the copied originals. A B04 median of June 2022 near 2,880 (stored: 3,882) confirms the correction. Step 3 should report no missing scene; the first rebuilt month records the SHA-256 of the seeded 2019-02 bands in `carried_from`.

## Do not do without approval

- Overwrite, move or delete anything under `data/mosaics/lake_mead/` or `data/stores/lake_mead/`. Swap the recovered files in only after Jake has compared them.
- Pass `--overwrite` to `build_water_stack.py` with the default roots; that deletes `data/stores/lake_mead/water-1`.
- Pass `--boa-offset-from` to the water-mask builder for recovered mosaics (it would subtract 1000 a second time). `examples/water_masks/README.md` and the builder's docstring still say to use it for Lake Mead; update them with the swap.
- Upload or `publish --update` any Lake Mead store. No reference to Lake Mead exists in `docs/`, `deploy/`, `site/` or `js/`, and three guessed paths on `data.chronozarr.org` return 404, but the R2 bucket cannot be listed from here. Confirm nothing from `water-1` was uploaded.
- Use the current `water-1` store, or the stored 2019-03 onward mosaics, for any result or figure.

## Limits

- Chain contamination is counted per pixel from the stored files and the SCL of the four 2019-03 offset scenes. It assumes a pixel is damaged whenever one of those scenes was valid there.
- The transfer range rests on one calibration month. Re-measure after the first rebuilt year.
- The pixel check used one window of B04. The XML check covers 23 of 2,318 scenes; the baseline rule is the same for every scene.
