# Sentinel-2 ingest benchmark data

Evidence for the download pipeline in `examples/sentinel2_pc/` (2026-10-10, one Apple M3 Max laptop and a Docker VM on it). The method, tables and conclusions are in [docs/evidence.md](../../docs/evidence.md#sentinel-2-ingest-performance); constrained runs are in [portability.md](portability.md).

- `workloads/`: frozen scene lists, one JSON per workload, with the SHA-256 that `bench.py` checks.
- `results/`: one JSON line per run, as written by `examples/sentinel2_pc/bench.py run`. `summary.md` is `bench.py summarize` over them. `lab-ucayali-x2.pre-cache-fix.jsonl` holds the throttled runs before GDAL's cached failed opens were cleared on retry, including the 3 runs whose output was not bit-exact.
- `runs/`: one JSON per Docker run (limits, exit code, cgroup memory peak).
- `audit-ucayali_santa_maria.json`, `audit-lake_mead.json`: per-month output of `examples/sentinel2_pc/audit_scenes.py` (no missing scenes in Ucayali; 674 in 34 Lake Mead months, see docs/evidence.md).
- `make_workload.py`, `make_multimonth.py`: derive the 5 km and 4-month lab workloads from the mirror.
- `Dockerfile`, `constrained.sh` and the helper scripts: rerun the constrained cases.

`bench.py` writes to `data/bench/results/` (not committed); copy the files here to keep them.
