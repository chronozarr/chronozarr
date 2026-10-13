"""Write the sidecar record of one constrained.sh run and, on failure, a results record.

Called by constrained.sh after the container exits. The sidecar holds the container limits,
exit code, OOM flag, cgroup counters and (when the bench finished) the bench's own record.
When the bench produced no record (OOM kill, crash), a failure record in the format of
`bench.py compare` is appended to data/bench/results/<workload>.jsonl so the run is not lost.
"""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RESULTS = REPO / "data" / "bench" / "results"
LAB_NAMES = ("latency_ms", "bandwidth_mbps", "fail_rate", "max_inflight")


def parse_bench_args(argv: list[str]) -> dict:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workload")
    parser.add_argument("--config")
    parser.add_argument("--lab")
    parser.add_argument("--rep", type=int, default=0)
    parser.add_argument("--lab-seed", type=int, default=0)
    parser.add_argument("--warm", action="store_true")
    parser.add_argument("--baseline-ref")
    known, _ = parser.parse_known_args(argv)
    lab = None
    if known.lab:
        parts = known.lab.split(",")
        lab = {n: float(v) for n, v in zip(LAB_NAMES, parts, strict=False) if v}
    return {
        "workload": known.workload,
        "config": known.config,
        "rep": known.rep,
        "lab": lab,
        "warm": known.warm,
    }


def parse_cgroup(stderr: str) -> dict:
    counters: dict[str, dict | int | str] = {}
    for line in stderr.splitlines():
        if not line.startswith("CGROUP "):
            continue
        _, name, *rest = line.split()
        if len(rest) == 1:
            counters.setdefault(name, rest[0])
        elif len(rest) == 2:
            counters.setdefault(name, {})[rest[0]] = rest[1]
        else:
            counters.setdefault(name, " ".join(rest))
    return counters


def last_json_line(text: str) -> dict | None:
    for line in reversed(text.strip().splitlines()):
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                return None
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    parser.add_argument("--exit-code", type=int, required=True)
    parser.add_argument("--oom", required=True)
    parser.add_argument("--cpus", required=True)
    parser.add_argument("--memory", required=True)
    parser.add_argument("--nofile", default="")
    parser.add_argument("--stdout", type=Path, required=True)
    parser.add_argument("--stderr", type=Path, required=True)
    parser.add_argument("--runs-dir", type=Path, required=True)
    parser.add_argument("bench_args", nargs="*")
    args = parser.parse_args()

    stdout = args.stdout.read_text()
    stderr = args.stderr.read_text()
    bench = parse_bench_args(args.bench_args)
    record = last_json_line(stdout) if args.exit_code == 0 else None
    now = datetime.now(UTC).isoformat(timespec="seconds")
    container = {
        "cpus": args.cpus,
        "memory": args.memory,
        "memory_swap": args.memory,
        "nofile": args.nofile or None,
        "exit_code": args.exit_code,
        "oom_killed": args.oom == "true",
        "cgroup": parse_cgroup(stderr),
    }
    stderr_tail = "\n".join(
        line for line in stderr.strip().splitlines() if not line.startswith("CGROUP ")
    )
    stderr_tail = "\n".join(stderr_tail.splitlines()[-25:])
    sidecar = {
        "tag": args.tag,
        "timestamp": now,
        "bench_args": args.bench_args,
        **bench,
        "container": container,
        "ok": record is not None,
        "stderr_tail": stderr_tail,
        "bench_record": record,
    }
    args.runs_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.replace(":", "").replace("-", "")
    path = args.runs_dir / f"{stamp}-{args.tag}.json"
    path.write_text(json.dumps(sidecar, indent=1) + "\n")

    if record is not None:
        # The bench wrote its own record into results; add the container limits to the sidecar
        # only (the results file stays in the bench's format).
        print(f"ok: {path}")
        return
    error = f"exit code {args.exit_code}" + (" (OOM killed)" if container["oom_killed"] else "")
    failure = {
        "ok": False,
        "workload": bench["workload"],
        "config": bench["config"],
        "rep": bench["rep"],
        "lab": bench["lab"],
        "warm": bench["warm"],
        "timestamp": now,
        "container": container,
        "error": f"{error}: {stderr_tail[-1500:]}",
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    with (RESULTS / f"{bench['workload']}.jsonl").open("a") as f:
        f.write(json.dumps(failure) + "\n")
    print(f"FAILED ({error}): {path}")


if __name__ == "__main__":
    main()
