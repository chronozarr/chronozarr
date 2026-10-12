"""Print one line per constrained run (sidecars in runs/) for quick inspection."""

import glob
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
pattern = sys.argv[1] if len(sys.argv) > 1 else "*"
for path in sorted(glob.glob(str(HERE / "runs" / f"{pattern}.json"))):
    d = json.loads(Path(path).read_text())
    c = d["container"]
    cg = c["cgroup"]
    r = d["bench_record"]
    head = f"{Path(path).name[:40]:40} exit={c['exit_code']} oom={c['oom_killed']}"
    if r is None:
        print(head, "|", d["stderr_tail"][-200:].replace("\n", " / "))
        continue
    s, rs = r["settings"], r["run_summary"]
    stat = cg.get("cpu.stat", {})
    print(
        head,
        f"wall={r['wall_seconds']} cpu={r['cpu_seconds']}",
        f"rss={r['peak_rss_bytes'] / 2**30:.2f}GiB fds={r.get('peak_open_fds')}",
        f"cgpeak={int(cg['memory.peak']) / 2**30:.2f} max_ev={cg['memory.events']['max']}",
        f"thr={stat.get('nr_throttled')}/{stat.get('nr_periods')}",
        f"cgcpu={int(stat.get('usage_usec', 0)) / 1e6:.1f}",
    )
    if s:
        print(
            "   ",
            {k: s[k] for k in ("requests", "max_requests", "cpu_workers")},
            f"budget={s['memory_budget'] / 2**30:.2f}GiB cache={s['gdal_cache'] >> 20}MiB",
            f"ret={rs['read_retries']} thr503={rs['http_throttled']} failed={rs['scenes_failed']}",
            f"over={rs['months_over_budget']} final_lim={rs['final_request_limit']}",
            f"events={len(rs['limiter_events'])} | {rs['bottleneck']}",
        )
