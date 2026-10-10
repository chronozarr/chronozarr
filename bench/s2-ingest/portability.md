# Sentinel-2 ingest: behaviour under constrained resources

Measured 2026-10-10 on branch `perf/s2-ingest-adaptive` (working tree, git 01185cd plus uncommitted changes; the baseline is `mosaic.py` and `catalog.py` at 01185cd). No source file of the pipeline was edited. `bench.py` was not changed.

## Machines

- Host: Apple M3 Max, 16 CPUs, 128 GB, macOS (Darwin 25.6.0), Python 3.13.11, rasterio 1.5.0 (GDAL 3.12.1), numpy 2.4.4.
- Docker Desktop 29.4.0 VM: 16 CPUs, 7.65 GiB, Linux 6.12.76 linuxkit aarch64, cgroup v2. Container: python:3.13-slim (Python 3.13.16), project from `uv sync --frozen --extra ingest --extra dev`, rasterio 1.5.0 (GDAL 3.12.1), numpy 2.4.4. Mirror and results are bind-mounted from the host (virtiofs).
- Workload `lab-ucayali-x2`: 1 month, 20 scenes, 2765 x 2759 px window, 870 MB read by the new pipeline (1390 MB by the baseline), served by `labserver.py` on localhost. `month_bytes` for it is 1.53 GB (1.43 GiB). `lab-ucayali-x2-5km`: same scenes, 5 km box (bbox changed only; the workload sha is computed over the scene list, so it is the same sha), 86 MB read.
- Swap is off in every container (`--memory-swap` = `--memory`). One container ran at a time. Host and container runs did not overlap.

## Commands

```bash
# 1. derived workload
uv run python bench/s2-ingest/make_workload.py

# 2. image and runner
docker build -f bench/s2-ingest/Dockerfile -t chronozarr-s2-bench .
bench/s2-ingest/constrained.sh --cpus 2 --memory 3g --tag NAME -- --workload lab-ucayali-x2 --config auto --rep 0
bench/s2-ingest/constrained.sh --cpus 4 --memory 4g --nofile 64 --tag NAME -- --workload lab-ucayali-x2 --config auto --rep 0
# descriptor and RSS-timeline probe (same arguments as bench.py run); FDPROBE_NOFILE lowers the limit of the pipeline process only
bench/s2-ingest/constrained.sh --cpus 4 --memory 4g --env FDPROBE_NOFILE=64 --script /repo/bench/s2-ingest/fdprobe.py \
    --tag NAME -- --workload lab-ucayali-x2 --config fixed-16 --rep 0

# 3. host, network conditions (2 reps each; baseline, auto, fixed-16 interleaved in random order)
uv run python examples/sentinel2_pc/bench.py compare --workloads lab-ucayali-x2-5km --configs baseline,auto,fixed-16 --reps 2 --lab 300,0,0
#   same with --lab 0,2,0   --lab 0,0,0,4   --lab 0,0,0.05   and no --lab (unshaped reference)
# 3b. extra, full workload
uv run python examples/sentinel2_pc/bench.py compare --workloads lab-ucayali-x2 --configs baseline,auto,fixed-4,fixed-16 --reps 2 --lab 0,0,0,4
uv run python examples/sentinel2_pc/bench.py compare --workloads lab-ucayali-x2 --configs auto,fixed-4,fixed-16 --reps 2 --lab 0,0,0,2
uv run python examples/sentinel2_pc/bench.py compare --workloads lab-ucayali-x2 --configs auto,fixed-16 --reps 2 --lab 0,0,0.3

# 3c. SLURM variables on the host
SLURM_CPUS_PER_TASK=2 SLURM_MEM_PER_NODE=2048 uv run python examples/sentinel2_pc/bench.py run --workload lab-ucayali-x2-5km --config auto
SLURM_CPUS_PER_TASK=2 SLURM_MEM_PER_NODE=2048 uv run python examples/sentinel2_pc/bench.py run --workload lab-ucayali-x2 --config auto
SLURM_CPUS_PER_TASK=2 SLURM_MEM_PER_CPU=512  uv run python examples/sentinel2_pc/bench.py run --workload lab-ucayali-x2-5km --config auto

# tables below
uv run python bench/s2-ingest/analyze.py all
```

Files: `bench/s2-ingest/{Dockerfile,Dockerfile.dockerignore,constrained.sh,container_record.py,fdprobe.py,make_workload.py,analyze.py,show.py}`. One JSON sidecar per container run (limits, exit code, OOM flag, cgroup counters, bench record) is in `bench/s2-ingest/runs/` (80 files). Every run, including failures, is in `data/bench/results/lab-ucayali-x2*.jsonl`. A run that the bench could not finish (OOM kill) gets a failure record from `container_record.py` in the format of `bench.py compare`.

Totals since 15:00 UTC: 133 records, 121 ok, 12 failed (11 OOM kills, 1 container I killed by hand after 10 minutes, see finding 5).

Correctness: SHA-256 of each month's `bands` and `coverage` against the macOS baseline of the same workload (the hash most baseline runs agree on). Every one of the 121 successful runs, container (Linux aarch64) and host, new pipeline and baseline, matched it, so output is also identical across macOS and Linux.

"Peak RSS" is the pipeline process's maximum RSS (`ru_maxrss`); the lab server's memory is not in it. "cgroup peak" is the container's `memory.peak` (pipeline plus lab server plus page cache, so it saturates at the limit when the cache fills). "Budget" is `settings.memory_budget` as the run reported it.

## 1. Docker, 2 CPUs, 3 GB, no shaping

`detect_resources` saw: 2 CPUs (source "cgroup CPU quota"), memory limit 3168632832 B (the 3 GiB cgroup limit minus 50 MB in use at start; source "cgroup memory limit"), open files 1048576. Settings: requests 16, max_requests 16, cpu_workers 2, memory_budget 1.48 GiB, gdal_cache 94 MiB.

| config | runs | wall s median [min-max] | peak RSS GiB | budget GiB | cgroup peak GiB | CPU throttled periods | retries / failures | correct |
|---|---|---|---|---|---|---|---|---|
| auto | 3 ok | 10.0 [9.8-10.7] | 2.95 [2.81-2.97] | 1.48 | 3.00, 2.95, 3.00 | 89/122, 88/114, 88/115 | 0 / 0 | exact |
| fixed-4 | 3 ok | 7.0 [6.7-7.2] | 2.14 [2.12-2.17] | 1.46 | | | 0 / 0 | exact |

With 50 ms latency (`--lab 50,0,0`): auto 8 runs, 7 ok and 1 OOM-killed (exit 137); the 7 ok took 7.9 s median [7.8-8.2], peak RSS 2.88 GiB [2.74-2.97]. fixed-4, 2 runs, 13.6 s [13.6-13.7], RSS 2.06 GiB [2.03-2.08]. All exact.

Peak RSS is 2.0 times the budget and 98 % of the 3 GiB limit. In two of the three unshaped runs the cgroup hit its limit 55 times (`memory.events max`) and reclaimed page cache.

## 2. Docker, 1 CPU, 1500 MB (and where it starts to work)

All three configs were OOM-killed (exit 137, `OOMKilled` true) within 2 to 20 s. Auto logged `2024-07 needs 1.53 GB, over the 0.76 GB budget; reading it alone` and then `Killed`. The warning does not say that the month cannot fit in the container.

| memory limit | config | runs | result | peak RSS GiB | wall s | notes |
|---|---|---|---|---|---|---|
| 1500m | auto | 1 | OOM-killed | | | warning printed, then killed |
| 1500m | fixed-4 | 1 | OOM-killed | | | same warning |
| 1500m | baseline | 1 | OOM-killed | | | |
| 2g | auto | 1 | OOM-killed | | | budget 1.05 GiB, warning printed |
| 2500m | auto | 1 | OOM-killed | | | budget 1.28 GiB, warning printed |
| 3g | auto | 3 ok | complete | 2.69 [2.67-2.71] | 23.3 [21.5-24.6] | budget 1.48 GiB, no warning, cgroup peak 2.82-2.87 GiB, 202-233 of 232-264 periods throttled |
| 3g | fixed-4 | 3 ok | complete | 2.06 [2.06-2.13] | 17.7 [13.7-18.2] | |
| 3g | baseline | 3 | 3 OOM-killed | | | killed after 15 to 20 s |
| 4g | auto | 1 ok | complete | 2.70 | 25.8 | |

At 1 CPU the threshold for this month lies between 2.5 GiB (killed) and 3 GiB (ran), against a computed need of 1.43 GiB. The baseline was also killed at 4 CPUs with 4g and 6g (1 run each; its host RSS is 15.7 GiB). All completed runs are exact.

## 3. Docker, 4 CPUs, 4 GB, `--ulimit nofile=64:64`

Settings: cpus 4, cpu_workers 3, max_requests 8 (reason "open-file limit 64 allows about 8 descriptors per read"), memory_budget 1.98 GiB, gdal_cache 126 MiB.

| config | runs | wall s median [min-max] | peak RSS GiB | budget GiB | cgroup peak GiB | peak fds | retries / failures | correct |
|---|---|---|---|---|---|---|---|---|
| auto | 3 ok | 4.3 [4.2-4.4] | 2.55 [2.51-2.77] | 1.98 | 2.67, 2.95, 2.74 | not measured | 0 / 0 | exact |
| fixed-16 (runs as 8, capped) | 2 ok | 4.3 [4.2-4.4] | 2.52 [2.49-2.55] | 1.98 | 2.66, 2.74 | not measured | 1 retry in 1 run / 0 | exact |

Same container without the limit (`auto`, 3 runs): 4.7 s [4.5-4.7], RSS 3.13 GiB [3.11-3.21], requests 16. `fixed-4`, 3 runs: 5.6 s [5.6-5.8], RSS 2.14 GiB.

### File descriptors (extra runs, `fdprobe.py`)

Peak descriptors of the pipeline process, `fixed-16` (requests are capped to `open_files // 8`):

| nofile | requests used | runs | peak fds | wall s | read retries | notes |
|---|---|---|---|---|---|---|
| 1048576 | 16 | 2 | 120, 112 | 4.4, 4.5 | 0 | |
| 256 | 16 | 2 | 123, 117 | | 0 | |
| 128 | 16 | 2 + 3 (pipeline-only limit) | 115, 111; 113, 112, 122 | 4.4 to 5.0 | 0 | 95 % of the limit in one run |
| 96 | 12 | 2 | 96, 90 | 4.3, 4.4 | 0 | |
| 64, limit shared with the lab server | 8 | 6 | 61 to 64 | 4.3 to 6.3, and one 65.0 | 3 of 6 runs retried one read | |
| 48, shared | 6 | 3 | 48 | 64.7, 4.6, 125.2 | 0 | |
| 32, shared | 4 | 2 + 1 killed | 32 | 66.6, 125.9, killed at 10 min | 0 | extra bytes: 1114-1220 MB vs 870 MB |
| 64, pipeline only | 8 | 6 | 62 to 64 | 4.1 to 4.3 | 0 | exact, 870 to 879 MB |
| 48, pipeline only | 6 | 3 | 48 | 4.5 to 4.6 | 0 | 884-915 MB |
| 32, pipeline only | 4 | 3 | 32 | 6.4 to 7.4 | 0 | 1031-1052 MB |

"Shared" means the `ulimit` applied to the container, so the lab server it starts inherited it. "Pipeline only" lowers the limit of the pipeline process with `FDPROBE_NOFILE` and gives the server the container limit back. All runs are exact. The stalls of 60 s and multiples of 60 s occurred only in the shared runs. During the killed run `docker top` showed `labserver.py` at 89 % CPU after 8 minutes (CPU time 8:24), the signature of a server that cannot accept new connections. The pipeline-only runs did not stall.

## 4. Host (macOS), `lab-ucayali-x2-5km`, shaped network

Settings on the host in every case: requests 16, max_requests 16, cpu_workers 15, gdal_cache 512 MiB, memory_budget 24.7 to 25.8 GiB (50 % of 49 to 51 GiB "available memory", varies from run to run). 2 runs per row. All 30 runs are exact.

| condition | config | wall s median [min-max] | peak RSS GiB | MB read | 503 warnings | read retries | scenes failed | final limit | limiter events |
|---|---|---|---|---|---|---|---|---|---|
| unshaped | baseline | 1.0 [0.9-1.0] | 0.68 | 99 | | | | | |
| unshaped | auto | 0.3 [0.3-0.3] | 0.32 | 86 | 0 | 0 | 0 | 16 | 1 |
| unshaped | fixed-16 | 0.3 [0.3-0.3] | 0.32 | 86 | 0 | 0 | 0 | 16 | 1 |
| 300 ms | baseline | 27.6 [27.6-27.6] | 0.69 | 99 | | | | | |
| 300 ms | auto | 5.8 [5.8-5.8] | 0.30 | 86 | 0 | 0 | 0 | 16, 16 | 2, 2 |
| 300 ms | fixed-16 | 5.8 [5.7-5.8] | 0.29 | 86 | 0 | 0 | 0 | 16 | 1 |
| 2 MB/s | baseline | 50.2 [50.2-50.3] | 0.69 | 99 | | | | | |
| 2 MB/s | auto | 43.2 [43.2-43.2] | 0.31 | 86 | 0 | 0 | 0 | 16 | 2 |
| 2 MB/s | fixed-16 | 43.2 [43.2-43.2] | 0.30 | 86 | 0 | 0 | 0 | 16 | 1 |
| max 4 in flight | baseline | 1.7 [1.6-1.8] | 0.69 | 102 | | | | | |
| max 4 in flight | auto | 4.4 [3.4-5.5] | 0.27 | 87 | 21, 31 | 0 | 0 | 16, 16 | 2, 2 |
| max 4 in flight | fixed-16 | 4.0 [3.5-4.5] | 0.27 | 89 | 18, 31 | 0 | 0 | 16, 16 | 1, 1 |
| fail rate 0.05 | baseline | 8.9 [7.9-9.9] | 0.68 | 105 | | | | | |
| fail rate 0.05 | auto | 3.5 [2.3-4.6] | 0.29 | 91 | 6, 10 | 0 | 0 | 16, 16 | 1, 2 |
| fail rate 0.05 | fixed-16 | 3.0 [2.2-3.8] | 0.29 | 89 | 11, 12 | 0 | 0 | 16 | 1 |

Auto did not leave 16 in any of these: the runs last 0.3 to 43 s, and a back-off needs an epoch (3 s and at least `limit` completed reads) in which 503s outnumber completed reads, or two failed reads within 30 s. The 2 MB/s case is bandwidth bound (86 MB / 2 MB/s = 43 s), so concurrency does nothing; the new pipeline wins 14 % on bytes alone. Under the 4-in-flight cap the baseline (4 sequential readers) was faster than both new configs (n = 2 each: 1.6 and 1.8 s against 3.4 to 5.5 s).

### Extra: full workload `lab-ucayali-x2` (longer runs, so the limiter has several epochs), host, 2 runs per row, all exact

| condition | config | wall s median [min-max] | peak RSS GiB | MB read | 503 warnings | retries | final limit |
|---|---|---|---|---|---|---|---|
| max 4 in flight | baseline | 23.5 [23.1-23.8] | 15.67 | 1390 | | | |
| max 4 in flight | auto | 8.7 [8.0-9.4] | 3.83 | 1594 | 20, 28 | 0 | 16, 16 |
| max 4 in flight | fixed-4 | 8.6 [8.5-8.7] | 3.41 | 1696 | 3, 2 | 0 | 4 |
| max 4 in flight | fixed-16 | 8.3 [6.4-10.1] | 3.69 | 1575 | 17, 20 | 0 | 16 |
| max 2 in flight | auto | 9.2 [8.9-9.6] | 3.59 | 1337 | 28, 30 | 0 | 16, 16 |
| max 2 in flight | fixed-4 | 10.8 [9.4-12.1] | 3.22 | 1368 | 4, 6 | 0 | 4 |
| max 2 in flight | fixed-16 | 12.1 [11.8-12.5] | 3.57 | 1346 | 36, 28 | 0 | 16 |
| fail rate 0.3 | auto | 69.2 [65.1-73.4] | 3.32 | 1229 | 74, 81 | 0, 1 | 12, 16 |
| fail rate 0.3 | fixed-16 | 70.6 [63.6-77.6] | 3.36 | 1251 | 87, 89 | 1, 1 | 16 |

Unshaped, the same workload reads 870 MB. With a 4-request cap the new pipeline reads 1.58 to 1.70 GB, more than the baseline (1.39 GB), and with a 2-request cap 1.34 to 1.37 GB: aborted and retried range requests are fetched again. The cause was not isolated. At fail rate 0.3 auto backed off from 16 to 12 at about 3.3 s in both runs (reason "host throttled"), probed up again, and in one run settled at 12 ("no gain above 12; settle"); wall time is no different from fixed-16 (65 to 74 s against 64 to 78 s; the run is dominated by GDAL's retry delays, CPU 1 %).

## 5. Host with SLURM variables

`SLURM_CPUS_PER_TASK=2 SLURM_MEM_PER_NODE=2048` (macOS has 16 CPUs and no cgroup):

| workload | config | runs | cpus (source) | memory limit (source) | cpu_workers | budget | gdal_cache | wall s | peak RSS GiB | warning |
|---|---|---|---|---|---|---|---|---|---|---|
| 5km | auto | 2 | 2 (SLURM_CPUS_PER_TASK) | 2 GiB (SLURM_MEM_PER_NODE) | 2 | 1.00 GiB | 64 MiB | 0.33, 0.33 | 0.30, 0.29 | none |
| 5km | fixed-16 | 1 | same | same | 2 | 1.00 GiB | 64 MiB | 0.33 | 0.30 | none |
| full | auto | 1 | same | same | 2 | 1.00 GiB | 64 MiB | 3.3 | 3.26 | `needs 1.53 GB, over the 1.07 GB budget; reading it alone` |
| 5km | auto, `SLURM_MEM_PER_CPU=512` | 1 | 2 | 1 GiB (SLURM_MEM_PER_CPU) | 2 | 0.50 GiB | 64 MiB | 0.34 | 0.28 | none |

Settings follow the variables. With the 2 GiB limit the full workload peaked at 3.26 GiB, 1.6 times the limit; under a real SLURM cgroup at 2 GiB it would be killed, as the 2g and 2500m containers were. The process used 178 % of 2 CPUs because SLURM variables are not enforced on the host.

## 6. Does auto stay within what it is given?

| run | CPU allocation | process CPU s / wall s | cgroup CPU s / wall (incl. lab server) | memory limit | budget | peak RSS | RSS / budget | RSS / limit |
|---|---|---|---|---|---|---|---|---|
| Docker 2 CPU 3g auto (3 runs) | 2 | 1.5 CPUs | 20.4-22.1 s / 9.8-10.7 s | 3.0 GiB | 1.48 | 2.81-2.97 | 1.9-2.0 | 0.94-0.99 |
| Docker 1 CPU 3g auto (3 runs) | 1 | 0.77 | 23.1-26.2 s / 21.5-24.6 s | 3.0 GiB | 1.48 | 2.67-2.71 | 1.8 | 0.89-0.90 |
| Docker 4 CPU 4g nofile 64 auto (3 runs) | 4 | 2.4 | 13.9-14.1 s / 4.2-4.4 s | 4.0 GiB | 1.98 | 2.51-2.77 | 1.3-1.4 | 0.63-0.69 |
| Docker 4 CPU 4g auto (3 runs) | 4 | | | 4.0 GiB | 1.98 | 3.11-3.21 | 1.6 | 0.78-0.80 |
| Host SLURM 2 GiB, full workload | 2 | 1.78 (5km run) | | 2.0 GiB | 1.00 | 3.26 | 3.3 | 1.63 |
| Host, no limit | 16 | | | 50.6 GiB available | 25-28 | 3.4-3.8 | 0.14 | 0.07 |

CPU: in a container the CPU count and cpu_workers follow the quota (1, 2 and 4 CPUs gave cpu_workers 1, 2, 3), the process stays inside the allocation (the kernel enforces it; 73 to 89 % of periods were throttled at 1 and 2 CPUs), and the diagnostics report "CPU" as the bottleneck there. The CPU the bench reports excludes the lab server, which used 5 to 8 s of the cgroup's budget in these runs, so the report understates CPU saturation by that amount; a real remote host does not run in the container, but TLS decryption would take CPU that this server does not.

Memory: the budget does not bound the process. A month that fits the budget (needs 1.43 GiB, budget 1.48 GiB) still peaks at 1.8 to 2.0 times the budget, because the RSS timeline of a 4 GiB run (`rss-c4m4g-auto-r0`: 0.04 GiB at 1 s, 1.48 at 2 s, 2.2 at 3.5 s, 2.5 to 2.7 GiB from 4 to 5.5 s, then 1.5 GiB at the end of the month) rises through the read phase to well above `month_bytes` and stays at 1.5 GiB after the month is done. RSS also scales with concurrent reads: at 4 CPUs/4g, 8 reads gave 2.5 GiB, 16 reads 3.1 to 3.3 GiB, 4 reads 2.1 GiB (1 to 3 runs each).

## Findings

1. **Memory budget does not bound peak RSS; auto can be OOM-killed at limits the budget calls fine.** `examples/sentinel2_pc/mosaic.py:490-496` (`month_bytes`) and `:692-695` (admission). Evidence: Docker 2 CPU/3g, budget 1.48 GiB, need 1.43 GiB, no warning, peak RSS 2.81-2.97 GiB against 3.0 GiB; with 50 ms latency 1 of 8 runs was OOM-killed (cgroup peak 3.00 GiB, `memory.events` max 181, oom_kill 1). At 1 CPU, 2g and 2500m were killed, 3g ran (peak 2.7 GiB). A 20-scene month of this size needs about 2.7 to 3.0 GiB of RSS, about 1.9 times `month_bytes`. A limit that auto accepts without a warning therefore fails. Reproduce: `bench/s2-ingest/constrained.sh --cpus 2 --memory 3g --tag x -- --workload lab-ucayali-x2 --config auto --lab 50,0,0` (repeat; about 1 in 8). Suggested: calibrate `month_bytes` (or add the read-phase overhead) so that the over-budget warning fires when peak RSS would exceed the limit, not 0.5 times it.
2. **The over-budget warning does not say the run will fail.** `mosaic.py:695-702`. With the month alone larger than the detected limit (1500m: need 1.53 GB, limit 1.57 GB, killed 2 to 10 s after the warning in 3 of 3 configs), the pipeline logs "reading it alone" and continues. Fail fast with the limit, the need and a suggested `--memory`/smaller AOI, or at least say that the limit is `Resources.memory_limit` and will be exceeded.
3. **`--requests` start value (16) ignores CPU count; at 1 and 2 CPUs fixed-4 is faster and smaller.** `performance.py:33-34` and `plan_settings` (`:243-258`). Docker, no shaping, 3 runs each: 2 CPUs auto 10.0 s [9.8-10.7], RSS 2.95 GiB vs fixed-4 7.0 s [6.7-7.2], 2.14 GiB (-30 % wall, -27 % RSS); 1 CPU/3g auto 23.3 s [21.5-24.6], 2.69 GiB vs fixed-4 17.7 s [13.7-18.2], 2.06 GiB. At 4 CPUs auto (16) is faster: 4.7 s vs 5.6 s. With 50 ms latency at 2 CPUs auto won (7.9 s vs 13.6 s, 1 and 2 successful runs). The lab server shares the container CPU and a real link adds latency, so this is zero-latency evidence; the limiter cannot see CPU-bound reads because it measures useful bytes per second and these runs are throttled by the CPU quota, not the host.
4. **`open_files // 8` leaves no descriptor headroom.** `performance.py:244-245`. Peak descriptors equal the limit at 64 (62-64, 8 reads), 48 (48, 6 reads) and 32 (32, 4 reads), and 111-122 of 128 with 16 reads; unlimited, 16 reads use 112-120 (about 7 per read). Pipeline-only limits ran clean (15 of 15 exact, no retries). But when the limit is a container `ulimit` shared with other processes, the same limit exhausted the lab server and gave read retries (3 of 6 runs at 64) and stalls of 60 to 125 s; the pipeline's diagnostics then read "neither saturated" or "network or the host limits throughput". A real cause is a descriptor limit shared with another process in the same limit, for example an HPC job that also holds many files. Not isolated: whether `EMFILE` itself is the read failure (rasterio reports "Read failed. See previous exception for details.").
5. **`labserver.py` spins at 100 % CPU when it hits the descriptor limit.** `examples/sentinel2_pc/labserver.py:162` (`ThreadingHTTPServer`; accept loop). At `nofile=32` shared with the pipeline, one run did not finish in 10 minutes; `docker top` showed `labserver.py` at 89 % of a CPU. This is a harness problem, not the pipeline. I killed that container by hand (it is the one failure that is not an OOM kill).
6. **Wrong reason text for cpu_workers at 1 or 2 CPUs.** `performance.py:237` always prints "usable CPUs less one for the scheduler", but `:234` uses all CPUs when `cpus <= 2` (1 CPU gives 1, 2 CPUs give 2, 4 CPUs give 3). Seen in the 1 and 2 CPU runs: `cpu_workers=2: usable CPUs less one for the scheduler`.
7. **`--performance fixed` is not repeatable in memory.** `performance.py:227` says fixed settings are a function of the resources, and README line 46 says two runs use the same settings. `memory_budget` is 50 % of the currently available memory and changed between runs on one host (24.70 and 24.91 GiB in `fixed-16` runs 10 s apart). Only the budget changes, but the docstring is wrong.
8. **On a hard concurrent-request cap and a short run, the new default is slower than the baseline.** Host, 5 km box, cap of 4 in flight: baseline 1.6 and 1.8 s; auto 3.4 and 5.5 s; fixed-16 3.5 and 4.5 s (n = 2 each). 18 to 31 HTTP 503s per run (each costing GDAL's 1 s retry delay) never exceeded completed reads in a 3 s epoch, so auto stayed at 16 (limiter events: start and "at ceiling"). The full workload (about 9 s) under the same cap is 2.7 times faster than the baseline, so this is a short-run effect. Bytes read under a cap are 1.5 to 1.9 times the unshaped bytes (1.58 to 1.70 GB against 0.87 GB, more than the baseline's 1.39 GB).
9. **Image build.** `uv.lock` has no linux/aarch64 wheel for `numcodecs 0.14.1` (only x86_64), so on an aarch64 Linux host (Docker Desktop on Apple silicon, Graviton) `uv sync --frozen` builds it from the locked sdist and needs a C compiler; the image installs `build-essential` for that. `docker build` first stalled for 6 minutes at "load metadata"; `docker pull` of both base images first fixed it.
10. **Container memory limit reads memory in use.** `performance.py:156-162` subtracts `memory.current` from `memory.max`. `memory.current` includes page cache, so a job that has already read large files starts with a smaller limit. Here it removed 50 MB (3168632832 against 3221225472). Not tested with a large page cache.

Nothing in these runs produced a wrong output: no scene was dropped (`scenes_failed` 0 in every completed new-pipeline run), and no run mismatched the reference.

## Not tested

- Real network: no packet loss, TLS, DNS or Planetary Computer SAS-token refresh; the server is on loopback inside or next to the process, and in containers it shares the CPU and memory limit.
- x86_64 Linux, real SLURM or HPC cgroups, cgroup v1, `--cpuset-cpus` (affinity path), `memory.high`, and a Docker memory limit below the VM's 7.65 GiB other than the values listed.
- Multi-month workloads under memory pressure: every month here fits alone, so admission of overlapping months (`mosaic.py:693`) was not exercised below the budget. The 3-month 5 km workload was not run in containers.
- `--max-requests`, `--cpu-workers`, `--memory` overrides, `auto-capped`, and the warp path (`ucayali-warp-1m`) under limits.
- Slow link in a container: shaping was applied in containers only with 50 ms latency (2 CPUs, 3g); the 2 MB/s, 300 ms, throttled and failing cases ran on the host only.
- `ingest.py` end to end (encode phase, store write); only the download phase through `bench.py`.
- Why the retried-range bytes rise under a request cap, and whether `EMFILE` is the failure behind the shared-limit read errors.

## Changes made after these findings (2026-10-10)

Findings 1, 2, 3, 4, 5, 6 and 7 led to code changes; finding 8 and 10 did not.

- **Memory (1, 2).** Band reads now fill the month's stack in place instead of a separate plane per read; the BOA offset and mask are applied in blocks of 256 rows instead of whole-scene int32 copies; valid counts are int32. Month admission now counts the process (512 MiB), the GDAL cache, one grid-sized buffer per concurrent read and the finished months held for carry-forward and writing, not only the month buffers (`mosaic.fixed_bytes`, `mosaic.month_bytes`). The base term was set from measurement: the estimate for the 20-scene month is 2.41 GB, and peak RSS was 2.39 to 2.43 GB in 3 runs (2 CPUs, 3g, 50 ms latency: 2 runs; 1 CPU, 2500m: 1 run), against 2.8 to 3.0 GB before. The warning for a month over the budget now gives the estimate and says that the process may be killed. At 1 CPU and 2g the run is still killed (1 run): 2.4 GB plus the in-container lab server do not fit, and the warning says so beforehand.
- **Start concurrency (3).** Auto mode starts at 4 reads per CPU, at most 16: 4 at 1 CPU, 8 at 2. The 3 runs above at 1 and 2 CPUs started at 4 and 8.
- **Descriptors (4).** The ceiling keeps 32 descriptors for everything else: `(nofile - 32) // 8`, so 4 at a limit of 64.
- **Lab server (5).** `LabServer.get_request` pauses 50 ms after a failed accept, so running out of descriptors no longer spins at 100 % CPU.
- **Reason text (6)** names all CPUs when there are 2 or fewer.
- **Fixed mode (7).** The docstring and README now say that fixed mode holds the request concurrency constant, and that the memory budget follows available memory unless `--memory` is given.

After the changes, all 261 completed records in `data/bench/results/` (copied to `results/`) are bit-exact against the baseline.

## Memory preflight, failure semantics and a multi-month run (2026-10-10, later)

Two behaviour changes followed. Auto mode estimates the largest month's memory before any download and stops with a breakdown when it exceeds the budget; `--memory` is the explicit override. A scene that fails after every attempt now stops the run at its month by default (see the example README, "Read failures").

Regression runs on the final code, `lab-ucayali-x2` (20 scenes, one month), 1 run each:

| Case | Result | Peak RSS | Budget |
|---|---|---|---|
| Docker 2 CPUs, 3g, auto | stopped before any download: needs about 2.4 GiB, budget 1.46 GiB | | 1.46 GiB |
| Docker 2 CPUs, 3g, `--memory 2600MB` | completed, bit-exact, 8 reads | 2.38 GiB | 2.54 GiB |
| Docker 1 CPU, 3g, `--memory 2600MB` | completed, bit-exact, 4 reads | 2.27 GiB | 2.54 GiB |
| Docker 4 CPUs, 4g, nofile 64, `--memory 3000MB` | completed, bit-exact, ceiling 4 reads | 1.93 GiB | 2.93 GiB |
| Host, 5 km box, 300 ms | 5.7 s, no failed scenes | | |
| Host, 5 km box, 503 above 4 in flight | 4.5 s, 24 retried 503s, no failed scenes | | |
| Host, 5 km box, 5 % random 503 | 3.6 s, 12 retried 503s, no failed scenes | | |

Multi-month run, `lab-ucayali-4mo` (`make_multimonth.py`: 4 months of 10 scenes on the full Ucayali grid, served from the mirror under aliases), 1 run each:

| Case | Result | Peak RSS | Budget |
|---|---|---|---|
| Host, baseline (old code) | 43.6 s | 10.1 GiB | |
| Docker 2 CPUs, 3g, auto | stopped before any download: needs 1.74 GiB, budget 1.46 GiB | | 1.46 GiB |
| Docker 2 CPUs, 3g, `--memory 2600MB` | 18.5 s, bit-exact, up to 2 months at once | 2.08 GiB | 2.54 GiB |
| Docker 4 CPUs, 6g, auto | 9.2 s, bit-exact, up to 2 months at once | 2.73 GiB | 2.98 GiB |

The estimate for two overlapping 10-scene months is 2.49 GiB; the measured peak was 2.08 and 2.73 GiB, so the estimate is within about 10 % and every run stayed under its budget. Half of the available memory is conservative: in a 3 GB container auto mode refuses work that fits, and `--memory` is needed.

`constrained.sh` failures with exit code 2 from my own mistyped invocations (4 records without a workload) were removed from the results; they never reached the pipeline.
