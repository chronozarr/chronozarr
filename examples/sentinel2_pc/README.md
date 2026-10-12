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

## Performance settings

The download phase needs no tuning. By default (`--performance auto`) it reads the CPUs and memory this process may use, including container quotas, SLURM allocations and rlimits. It runs 16 concurrent remote reads, the best or tied-best number in [the benchmark](#benchmark), or 4 per CPU on machines with fewer than 4 CPUs. It lowers that number when the host pushes back: by half when reads fail repeatedly, by a quarter when HTTP 429 or 5xx responses outnumber completed reads. It climbs back while throughput rises and never goes above `--max-requests`. With a higher `--max-requests` it also tries more than 16 and keeps a step only when throughput rises by more than 10 %. Before it downloads anything, it estimates the memory the largest month needs and stops with a breakdown if that is more than the budget, which is half of the available memory unless `--memory` is given. Add `--diagnostics` to print each chosen setting with its reason. When the download finishes, the same flag prints which resource limited the run and the time spent in each stage.

| Flag | Default | Meaning |
|---|---|---|
| `--performance auto\|fixed` | `auto` | `fixed` keeps the request concurrency constant; with `--memory` as well, every setting is the same on every run |
| `--requests N` | 16, or 4 per CPU if fewer | concurrent remote reads; the start value in auto mode, the value itself in fixed mode |
| `--max-requests N` | 16 | ceiling on concurrent reads; raise it (e.g. 32) on a high-latency link, lower it for a shared or rate-limited one |
| `--cpu-workers N` | usable CPUs - 1 | threads for compositing |
| `--memory SIZE` | half of available memory | memory budget, e.g. `4GB`; a month that needs more stops the run before any download, so this is also the override when more memory is free |
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

Every setting changes speed and memory only. The arrays in the `.npz` files are identical for any combination of settings and equal bit for bit to those of the earlier sequential implementation (tested in `tests/test_ingest_mosaic.py`, measured in [the benchmark](#benchmark)). The files themselves differ in bytes because they are compressed at zlib level 1 instead of 6.

### Memory

A month is composited whole, so the memory a run needs grows with scenes per month times AOI pixels: about 0.8 GB for a 10-scene month of the 2,765 by 2,759 pixel Ucayali AOI, plus about 0.9 GB for the process, the GDAL cache, 16 reads in flight and the finished months held for carry-forward and writing. In Docker the estimate was within about 10 % of the measured peak, and every run stayed under its budget (`bench/s2-ingest/portability.md`). If the largest month does not fit the budget, the run stops at once:

```
error: 2024-07 has 10 scenes on a 2759 x 2765 grid and needs about 1.74 GiB, more than the memory budget of 1.46 GiB (50% of 2.9 GiB (cgroup memory limit)). Nothing was downloaded.
  month buffers (10 scenes x 4 bands): 0.75 GiB
  ...
Options: if this much memory is free, pass --memory with at least 1.8GB ...
```

Half of the available memory is a conservative default. When you know that more is free, `--memory` raises the budget explicitly. A smaller AOI, a shorter date range or a lower `--max-requests` lowers the need.

### Read failures

Every read is retried up to six times over about a minute. A scene that still fails after that would make its month's median a different number from the month's composite, so by default the run stops at that month, writes nothing for it, keeps the months before it and starts no later ones. Running the same command again resumes there.

`--keep-going` continues instead:

- A month with some failed scenes is written, and its file lists the failed scene IDs in `scenes_failed`.
- A month where every scene failed is not written. There is no time step for it, and the next run retries it. It is not filled from the month before, because a month made only of carried-forward pixels would look like an observation.
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
```

On one laptop and home connection (2026-10-10), the default settings were 1.9 to 4.7 times faster than the earlier implementation on five Planetary Computer workloads. Peak memory was 3 to 4 times lower on full-size AOIs, and the output arrays were bit-exact. Against a local server without shaping the speed-up was 7.9 times. The tables, method, failures and limits are in [evidence](../../docs/evidence.md#sentinel-2-ingest-performance). Only this machine has been measured so far.

## What the script does

The download phase searches the Planetary Computer STAC API for scenes with `eo:cloud_cover < 80`. For each calendar month it builds a median composite of B02, B03, B04 and B08 at 10 m, in the UTM zone of the AOI. The median skips a pixel when any band is 0 or when its SCL class is not 4, 5, 6, 7 or 11. SCL is the Scene Classification Layer. A pixel with no valid scene takes the value from the previous month's composite (`carry_forward` in `mosaic.py`). A pixel with no earlier valid month stays 0, which is nodata. A month whose `.npz` file exists is skipped, so an interrupted download resumes; each file is written to a temporary name and renamed, so an interrupted write never leaves a partial month. Each month file also records how many scenes were searched, which scenes could not be read, and the month (with a SHA-256 of its bands) that filled its gaps. See [read failures](#read-failures).

Each scene's SCL band is read first. A scene with no valid pixel in the AOI reads no other band. When a scene is in the AOI's UTM zone and its pixel grid lines up with the AOI grid, which is the usual case, the bands are read as windows on the native grid, and only the 512 by 512 source blocks that contain a valid pixel are fetched. On that grid, bilinear resampling returns the source pixel, so the result is identical to the `reproject` that every other scene goes through. Reads of later months start while earlier months are composited and written, within the memory budget, and months are finished in calendar order because each month's gaps are filled from the month before.

The encode phase stacks the monthly files into a `(time, band, y, x)` uint16 array. It writes the array with `chronozarr.encode` and the default options, which store true values. Each band has a name, a `common_name` (blue, green, red or nir) and a scale of 0.0001. The `coverage` plane is 1 where at least one scene was valid and 0 where the value is carried forward or missing. It is a flag with no scene count. The `provenance` record names the collection, the `composite` ("monthly median") and the `gap_fill` ("carry-forward"). It also has notes on the cloud mask and the baseline 04.00 offset correction. With `--stac`, the script also writes the static STAC Collection and Item that `chronozarr stac` writes.

```
<out-dir>/                      default: <repo>/data
  mosaics/<aoi>/YYYY-MM.npz     one file per month
  stores/<aoi>/chronozarr/      chronozarr store
  stores/<aoi>/stac/            with --stac: collection.json and <aoi>-sentinel-2-monthly/*.json
```

Each `YYYY-MM.npz` file holds `bands` (uint16, shape (4, H, W), 0 = nodata) and `coverage` (float32, the fraction of the month's scenes that are valid). It also holds `transform` (6 affine coefficients), `epsg` and `band_names` (`B02 B03 B04 B08`). The file name gives the time coordinate, so `2024-01.npz` becomes 2024-01-01. All months of an AOI must share the same grid, CRS and bands, or the encode phase raises an error. A store is immutable. If `stores/<aoi>/chronozarr` exists, the script exits before it downloads anything, so delete the directory to encode again. To host the store, see [hosting](../../docs/hosting.md).
