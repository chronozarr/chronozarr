"""bench.py `run` plus a peak open-descriptor count for this process and the whole container.

Same arguments as `bench.py run`. A sampler thread lists /proc/self/fd every 10 ms (no locks,
nothing attached to logging) and the record gets `peak_open_fds` (this process). It also
samples resident memory every 250 ms into `rss_timeline`. The record is
appended to data/bench/results like any other run.
"""

import argparse
import contextlib
import importlib.util
import json
import os
import resource
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("bench", REPO / "examples/sentinel2_pc/bench.py")
bench = importlib.util.module_from_spec(spec)
sys.modules["bench"] = bench
spec.loader.exec_module(bench)

parser = argparse.ArgumentParser()
parser.add_argument("--workload", required=True)
parser.add_argument("--config", required=True)
parser.add_argument("--lab")
parser.add_argument("--lab-seed", type=int, default=0)
parser.add_argument("--warm", action="store_true")
parser.add_argument("--rep", type=int, default=0)
parser.add_argument("--baseline-ref", default=bench.DEFAULT_BASELINE_REF)
args = parser.parse_args()

# FDPROBE_NOFILE lowers the soft descriptor limit of this process only: the lab server it starts
# gets the container's limit back, so a descriptor shortage is the pipeline's and not the
# server's (a server at its limit spins on accept()).
client_nofile = os.environ.get("FDPROBE_NOFILE")
if client_nofile:
    server_soft, server_hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (int(client_nofile), server_hard))
    real_popen = subprocess.Popen

    def popen_for_server(*popen_args, **kwargs):
        def raise_limit() -> None:
            resource.setrlimit(resource.RLIMIT_NOFILE, (server_soft, server_hard))

        if "labserver.py" in " ".join(map(str, popen_args[0])):
            kwargs["preexec_fn"] = raise_limit
        return real_popen(*popen_args, **kwargs)

    bench.subprocess.Popen = popen_for_server

peak = [0]
rss_timeline: list[tuple[float, int]] = []  # (seconds since start, resident bytes)
done = threading.Event()


def resident_bytes() -> int:
    with open("/proc/self/statm") as f:
        return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")


def sample() -> None:
    t0 = time.monotonic()
    tick = 0
    while not done.is_set():
        tick += 1
        if tick % 25 == 0:  # every 250 ms
            rss_timeline.append((round(time.monotonic() - t0, 2), resident_bytes()))
        with contextlib.suppress(OSError):
            peak[0] = max(peak[0], len(os.listdir("/proc/self/fd")))
        time.sleep(0.01)


sampler = threading.Thread(target=sample, daemon=True)
sampler.start()
try:
    record = bench.run_once(args)
finally:
    done.set()
record["peak_open_fds"] = peak[0]
record["rss_timeline"] = rss_timeline
bench.append_result(args.workload, record)
print(json.dumps(record), flush=True)
