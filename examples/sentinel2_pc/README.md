# Sentinel-2 ingest example

This example builds a chronozarr store of monthly Sentinel-2 median composites from Microsoft Planetary Computer for one area of interest (AOI). It can also write a static STAC catalog next to the store. The example lives outside the `chronozarr` package. The `planetary-computer` package signs the asset URLs, so you need network access but no account and no API key. Both steps work within a memory budget: the download composites a month in strips of the AOI when the whole month does not fit, and the encode step reads one 512 by 512 cell of every month at a time.

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

## Performance settings

The download phase needs no tuning. By default (`--performance auto`) it reads the CPUs and memory this process may use, including container quotas, SLURM allocations and rlimits. It runs 16 concurrent remote reads, the best or tied-best number in [the benchmark](#benchmark), or 4 per CPU on machines with fewer than 4 CPUs. It lowers that number when the host pushes back: by half when reads fail repeatedly, by a quarter when HTTP 429 or 5xx responses outnumber completed reads. It climbs back while throughput rises and never goes above `--max-requests`. With a higher `--max-requests` it also tries more than 16 and keeps a step only when throughput rises by more than 10 %. Before it downloads anything, it estimates the memory the largest month needs within a budget of half the available memory (or `--memory`). If the whole month does not fit, it composites each month in strips of the AOI (see [memory](#memory)); it stops with a breakdown only if not even a 512-row strip fits. Add `--diagnostics` to print each chosen setting with its reason. When the download finishes, the same flag prints which resource limited the run and the time spent in each stage.

| Flag | Default | Meaning |
|---|---|---|
| `--performance auto\|fixed` | `auto` | `fixed` keeps the request concurrency constant; with `--memory` as well, every setting is the same on every run |
| `--requests N` | 16, or 4 per CPU if fewer | concurrent remote reads; the start value in auto mode, the value itself in fixed mode |
| `--max-requests N` | 16 | ceiling on concurrent reads; raise it (e.g. 32) on a high-latency link, lower it for a shared or rate-limited one |
| `--cpu-workers N` | usable CPUs - 1 | threads for compositing |
| `--memory SIZE` | half of available memory | memory budget, e.g. `4GB`; months that need more are composited in strips, and the run stops before any download only if a 512-row strip does not fit |
| `--strip-rows N` | planned | rows per strip, a multiple of 512; by default the whole grid when a month fits, else the tallest strips that fit |
| `--keep-going` | off | continue after a scene cannot be read, see [read failures](#read-failures) |
| `--diagnostics` | off | print settings, reasons, bottleneck and stage timings |

```bash
# a laptop on a slow or shared connection
uv run python examples/sentinel2_pc/ingest.py --aoi ucayali_santa_maria --max-requests 8 --memory 4GB

# a far-away or high-latency link: let auto mode try up to 32 reads
uv run python examples/sentinel2_pc/ingest.py --aoi ucayali_santa_maria --max-requests 32

# repeatable settings, e.g. on an HPC node
uv run python examples/sentinel2_pc/ingest.py --aoi ucayali_santa_maria --performance fixed --requests 16 --diagnostics
```

There is no separate calibration step, so there are no stored calibration results to reuse. Adaptation runs during the download in 3-second steps.

Every setting changes speed and memory only. The monthly arrays are identical for any combination of settings and strip height (tested in `tests/test_ingest_mosaic.py`, measured in [the benchmark](#benchmark)). For scenes on the AOI's own UTM grid, the usual case, they equal the earlier implementations bit for bit. A scene from a neighbouring UTM zone is warped, and GDAL by default splits such a warp into chunks wherever the scene covers only part of the AOI, which shifts some pixels by its approximate transformer's error (at most 0.125 source pixels). The pipeline warps the whole strip as one chunk with a fixed resampling scale, so a strip gives the same pixels as the whole grid; where GDAL would have split, those pixels differ from the earlier output within that error (see [evidence](../../docs/evidence.md#spatial-strips)).

### Memory

The memory a month needs grows with scenes per month times AOI pixels: about 0.8 GB for a 10-scene month of the 2,765 by 2,759 pixel Ucayali AOI, plus about 0.9 GB for the process, the GDAL cache, the CPU workers' temporaries, the reads in flight and the finished strips held for carry-forward and writing. When the whole month fits the budget, it is composited at once, as one strip. Otherwise each month is composited in full-width strips of the AOI grid, with heights in multiples of 512 rows, the tallest that fit:

```
Compositing each month in 3 strips of 1024 rows: 2024-07 (20 scenes) needs 2.98 GiB for the whole grid, more than the budget of 1.46 GiB (set by user); a strip of 1024 rows needs 1.45 GiB
```

Strips cost bytes, not results: a 512 by 512 source block that a strip edge cuts is read once for each strip. On a 20-scene month, 1024-row strips read 18 % more than the whole grid and took the same time; 512-row strips read 55 % more. Only if a 512-row strip does not fit does the run stop, before any download, with a breakdown:

```
error: 2024-07 has 20 scenes and its smallest strip (2759 x 512 pixels) needs about 1.09 GiB, more than the memory budget of 0.98 GiB (set by user). Nothing was downloaded.
  strip buffers (20 scenes x 4 bands): 0.26 GiB
  ...
Options: if this much memory is free, pass --memory with at least 1.1GB ...
```

A strip always spans the AOI's full width, because a narrower window would change warped pixels, so a very wide AOI on a small budget needs to be split into narrower AOIs. Half of the available memory is a conservative default; `--memory` raises the budget when you know more is free.

### Read failures

Every read is retried up to six times over about a minute. A scene that still fails after that would make its month's median a different number from the month's composite, so by default the run stops at that month, writes nothing for it, keeps the months before it and starts no later ones. Running the same command again resumes there.

`--keep-going` continues instead:

- A month with some failed scenes is written, and its file lists the failed scene IDs in `scenes_failed`, and in `scenes_failed_windows` the grid rows and columns each is missing from (the strips where it had valid pixels).
- A month where every scene failed (in any strip) is not written. There is no time step for it, and the next run retries it. It is not filled from the month before, because a month made only of carried-forward pixels would look like an observation.
- The encode step refuses months with failed scenes unless `--keep-going` is given there too. It then names them in the store's `provenance.notes`. Coverage stays 0 wherever a pixel was carried forward, as everywhere else in the store.
- The encode step also checks the carry-forward chain. If a month filled its gaps from a different month than the one before it in the stack, for example because that month was re-run or added later, encoding stops and names the months to rebuild.

## Benchmark

`bench.py` measures the download phase on frozen scene lists and checks every output month against the earlier implementation bit for bit. Each measurement runs in a fresh process, and configurations are interleaved in random order. The results go to `data/bench/results/` with the machine, settings, requests, bytes, peak memory and per-month hashes.

```bash
uv run python examples/sentinel2_pc/bench.py freeze --workload ucayali-1m      # search once, store the scene list
uv run python examples/sentinel2_pc/bench.py compare --workloads ucayali-1m \
    --configs baseline,fixed-4,fixed-16,auto --reps 3                         # baseline = mosaic.py at 01185cd
uv run python examples/sentinel2_pc/bench.py summarize

# without load on the public host: mirror the files once (9.7 GB), then shape a local server
uv run python examples/sentinel2_pc/bench.py mirror --workload ucayali-1m --max-gb 10
uv run python examples/sentinel2_pc/bench.py lab-workload --workload ucayali-1m --as lab-ucayali-x2 --alias-copies 2
uv run python examples/sentinel2_pc/bench.py compare --workloads lab-ucayali-x2 \
    --configs baseline,fixed-16,auto --reps 3 --lab 50,0,0,12                 # 50 ms, no cap, no random 503, 503 above 12 in flight

# strips: a -rowsN suffix sets the rows per strip; --baseline-ref picks the pipeline "baseline" runs
uv run python examples/sentinel2_pc/bench.py compare --workloads lab-ucayali-x2 \
    --configs baseline,auto,auto-rows1024,auto-mem1500 --reps 3 --lab 0,0,0 --baseline-ref d7c2ce1
```

On one laptop and home connection (2026-10-10), the default settings were 1.9 to 4.7 times faster than the earlier implementation on five Planetary Computer workloads. Peak memory was 3 to 4 times lower on full-size AOIs, and the output arrays were bit-exact. Against a local server without shaping the speed-up was 7.9 times. The tables, method, failures and limits are in [evidence](../../docs/evidence.md#sentinel-2-ingest-performance). Only this machine has been measured so far.

## What the script does

The download phase searches the Planetary Computer STAC API for scenes with `eo:cloud_cover < 80`. For each calendar month it builds a median composite of B02, B03, B04 and B08 at 10 m, in the UTM zone of the AOI. The median skips a pixel when any band is 0 or when its SCL class is not 4, 5, 6, 7 or 11. SCL is the Scene Classification Layer. A pixel with no valid scene takes the value from the previous month's composite (`carry_forward` in `mosaic.py`). A pixel with no earlier valid month stays 0, which is nodata. A month whose file exists is skipped, so an interrupted download resumes; each file is written strip by strip to a temporary name and renamed when complete, so an interrupted run never leaves a partial month. Each month file also records how many scenes were searched, which scenes could not be read, and the month (with a SHA-256 of its bands) that filled its gaps. See [read failures](#read-failures).

Each scene's SCL band is read first. A scene with no valid pixel in the AOI reads no other band. When a scene is in the AOI's UTM zone and its pixel grid lines up with the AOI grid, which is the usual case, the bands are read as windows on the native grid, and only the 512 by 512 source blocks that contain a valid pixel are fetched. On that grid, bilinear resampling returns the source pixel, so the result is identical to the `reproject` that every other scene goes through. Reads of later strips and months start while earlier ones are composited and written, within the memory budget, and strips are finished in order because each strip's gaps are filled from the same strip of the month before.

The encode phase presents the monthly files as one lazy `(time, band, y, x)` uint16 array, read one 512 by 512 cell of every month at a time, and writes it with `chronozarr.encode` and the default options, which store true values. Each band has a name, a `common_name` (blue, green, red or nir) and a scale of 0.0001. The `coverage` plane is the number of valid scenes behind each pixel that month, saturated at 255, and 0 where the value is carried forward or missing. An `.npz` month saved before `scenes_searched` was recorded (before 2026-10-10) holds only the fraction k / n, so encoding refuses it by name. `--scenes-searched FILE` supplies n per month as JSON (`{"2024-01": 4}`, or the report of `audit_scenes.py` as it is) and rebuilds k = rint(fraction * n) only where float32(k / n) reproduces every stored value bit for bit; a month that fails, disagrees with a recorded n, or holds only 0 and 1 with n above 1 (which cannot confirm n) stops the encode. `--no-coverage` writes the store without a `coverage` variable instead. The two are mutually exclusive. The `provenance` record names the collection, the `composite` ("monthly median") and the `gap_fill` ("carry-forward"). It also has notes on the cloud mask and the baseline 04.00 offset correction. With `--stac`, the script also writes the static STAC Collection and Item that `chronozarr stac` writes.

```
<out-dir>/                      default: <repo>/data
  mosaics/<aoi>/YYYY-MM.tif     one file per month (YYYY-MM.npz before 2026-10-11)
  stores/<aoi>/chronozarr/      chronozarr store
  stores/<aoi>/stac/            with --stac: collection.json and <aoi>-sentinel-2-monthly/*.json
```

Each `YYYY-MM.tif` is a tiled GeoTIFF (512 by 512 tiles, zstd) on the AOI grid, readable by GDAL and QGIS. Bands 1 to 4 are B02, B03, B04 and B08 (uint16 DN, 0 = nodata) and band 5 (`valid_count`) is the number of valid scenes per pixel. The GDAL metadata holds `band_names`, `scenes_searched`, `scenes_failed`, `scenes_failed_windows`, `carried_from` and `bands_sha256` (the month's identity: a SHA-256 over the SHA-256 of each 512 by 512 cell, so it does not depend on the strip height), each as JSON. `mosaic.load_mosaic` reads either format; months written before 2026-10-11 are `.npz` files with `bands`, `coverage` (float32, the fraction of the month's scenes that are valid), `transform`, `epsg` and `band_names`, and are still read and resumed from. The file name gives the time coordinate, so `2024-01.tif` becomes 2024-01-01. All months of an AOI must share the same grid, CRS and bands, or the encode phase raises an error. A store is immutable. If `stores/<aoi>/chronozarr` exists, the script exits before it downloads anything, so delete the directory to encode again. To host the store, see [hosting](../../docs/hosting.md).
