"""Markdown tables for portability.md from constrained-run sidecars and host results.

Reads bench/s2-ingest/runs/*.json (container runs) and data/bench/results/*.jsonl (host runs
after SINCE). Correctness: SHA-256 of each month against the unshaped macOS baseline of the same
workload (reference), and for container runs also against the Linux baseline when one finished.
"""

import glob
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE.parents[1] / "data" / "bench" / "results"
SINCE = "2026-10-10T15:20:00"
GIB = 2**30


def load_jsonl(workload: str) -> list[dict]:
    path = RESULTS / f"{workload}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def reference_hashes(workload: str) -> dict[str, str]:
    """Month hashes most macOS baseline runs agree on (shaped or not)."""
    candidates = [
        json.dumps(r["month_sha256"], sort_keys=True)
        for r in load_jsonl(workload)
        if r["ok"]
        and r["config"] == "baseline"
        and r["machine"]["platform"].startswith("macOS")
        and not r.get("warm")
    ]
    if not candidates:
        return {}
    return json.loads(statistics.mode(candidates))


def correctness(record: dict, reference: dict[str, str]) -> str:
    if not record["ok"]:
        return "no output"
    shas = record["month_sha256"]
    if not reference:
        return "no reference"
    return (
        "exact"
        if shas == reference
        else "MISMATCH "
        + ",".join(
            m for m in sorted(set(shas) | set(reference)) if shas.get(m) != reference.get(m)
        )
    )


def med(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def span(values: list[float], digits: int = 1) -> str:
    if not values:
        return "n/a"
    lo, hi = min(values), max(values)
    mid = statistics.median(values)
    return f"{mid:.{digits}f} [{lo:.{digits}f}-{hi:.{digits}f}]"


def row(label: str, runs: list[dict], reference: dict[str, str]) -> str:
    ok = [r for r in runs if r["ok"]]
    failed = len(runs) - len(ok)
    if not ok:
        errors = {r.get("error", "")[:60] for r in runs}
        return f"| {label} | 0 ok / {failed} failed | | | | | | {'; '.join(sorted(errors))} |"
    s = ok[0]["settings"]
    summaries = [r["run_summary"] for r in ok if r["run_summary"]]
    settings = "baseline (no settings)"
    budget = "n/a"
    if s:
        settings = (
            f"req {s['requests']}/{s['max_requests']}, cpu_workers {s['cpu_workers']}, "
            f"cache {s['gdal_cache'] >> 20} MiB"
        )
        budget = f"{s['memory_budget'] / GIB:.2f}"
    rss = [r["peak_rss_bytes"] / GIB for r in ok]
    retries = [x["read_retries"] for x in summaries]
    throttled = [x["http_throttled"] for x in summaries]
    scenes_failed = [x["scenes_failed"] for x in summaries]
    finals = [x["final_request_limit"] for x in summaries]
    events = [len(x["limiter_events"]) for x in summaries]
    extra = ""
    if summaries:
        extra = (
            f"retries {retries}, 503s {throttled}, scenes failed {scenes_failed}, "
            f"final limit {finals}, limiter events {events}"
        )
    cor = {correctness(r, reference) for r in ok}
    mb = [r["gdal_network"]["downloaded_bytes"] / 1e6 for r in ok if r.get("gdal_network")]
    return (
        f"| {label} | {len(ok)} ok / {failed} failed | {span([r['wall_seconds'] for r in ok])} | "
        f"{span(rss, 2)} | {budget} | {settings} | {span(mb, 0)} | {extra} | "
        f"{', '.join(sorted(cor))} |"
    )


HEADER = (
    "| config | runs | wall s median [min-max] | peak RSS GiB | budget GiB | settings | "
    "MB read | retries / throttle / failures | correctness |\n"
    "|---|---|---|---|---|---|---|---|---|"
)


def host_tables() -> None:
    for workload in ("lab-ucayali-x2-5km", "lab-ucayali-x2"):
        reference = reference_hashes(workload)
        groups: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
        for r in load_jsonl(workload):
            if r["timestamp"] < SINCE or "container" in r:
                continue
            if r["ok"] and not r["machine"]["platform"].startswith("macOS"):
                continue
            lab = r.get("lab")
            label = "unshaped" if not lab else ", ".join(f"{k}={v:g}" for k, v in lab.items())
            if r["ok"] and "SLURM" in r["machine"]["resources"]["cpu_source"]:
                limit = r["machine"]["resources"]["memory_source"]
                label += f", SLURM env (cpus 2, {limit})"
            groups[label][r["config"]].append(r)
        for label, by_config in groups.items():
            print(f"\n#### host, {workload}, {label}\n\n{HEADER}")
            for config in sorted(by_config):
                print(row(config, by_config[config], reference))


def container_tables() -> None:
    groups: dict[str, list[dict]] = defaultdict(list)
    for path in sorted(glob.glob(str(HERE / "runs" / "*.json"))):
        d = json.loads(Path(path).read_text())
        tag = d["tag"].rsplit("-r", 1)[0]
        groups[tag].append(d)
    for tag, sidecars in groups.items():
        first = sidecars[0]
        c = first["container"]
        print(
            f"\n#### container {tag}: cpus {c['cpus']}, memory {c['memory']}, nofile {c['nofile']}"
        )
        records = []
        for d in sidecars:
            r = d["bench_record"]
            if r is None:
                r = {
                    "ok": False,
                    "error": f"exit {d['container']['exit_code']}"
                    f"{' OOM-killed' if d['container']['oom_killed'] else ''}",
                }
            records.append(r)
        reference = reference_hashes(first["workload"])
        print(HEADER)
        print(row(first["config"], records, reference))
        peaks = [
            int(d["container"]["cgroup"]["memory.peak"]) / GIB
            for d in sidecars
            if "memory.peak" in d["container"]["cgroup"]
        ]
        fds = [r.get("peak_open_fds") for r in records if r.get("peak_open_fds")]
        stats = [d["container"]["cgroup"].get("cpu.stat", {}) for d in sidecars]
        thr = [f"{s['nr_throttled']}/{s['nr_periods']}" for s in stats if s]
        cgcpu = [int(s["usage_usec"]) / 1e6 for s in stats if s]
        print(
            f"\ncgroup memory.peak GiB {[round(p, 2) for p in peaks]}; "
            f"throttled periods {thr}; cgroup CPU s {[round(x, 1) for x in cgcpu]}; "
            f"peak fds {fds}"
        )


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which in ("host", "all"):
        host_tables()
    if which in ("container", "all"):
        container_tables()
