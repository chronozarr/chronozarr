"""Resource detection, settings and the adaptive request limiter of the Sentinel-2 ingest."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "sentinel2_pc"))
import performance as perf

pytestmark = pytest.mark.unit

GIB = perf.GIB


def resources(**overrides) -> perf.Resources:
    values = {
        "cpus": 8,
        "cpu_source": "test",
        "memory_total": 32 * GIB,
        "memory_available": 16 * GIB,
        "memory_limit": None,
        "memory_source": "test",
        "open_files": 10240,
    }
    values.update(overrides)
    return perf.Resources(**values)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("4GB", 4 * GIB),
        ("512MiB", 512 * 1024**2),
        ("2g", 2 * GIB),
        ("1000", 1000),
        ("1.5 GB", int(1.5 * GIB)),
    ],
)
def test_parse_bytes(text, expected):
    assert perf.parse_bytes(text) == expected


def test_parse_bytes_rejects_garbage():
    with pytest.raises(ValueError, match="4GB"):
        perf.parse_bytes("lots")


def test_fixed_mode_is_deterministic_and_respects_overrides():
    a = perf.plan_settings(
        resources(), adaptive=False, requests=6, cpu_workers=3, memory_budget=GIB
    )
    b = perf.plan_settings(
        resources(), adaptive=False, requests=6, cpu_workers=3, memory_budget=GIB
    )
    assert a == b
    assert (a.requests, a.max_requests, a.cpu_workers, a.memory_budget) == (6, 6, 3, GIB)
    assert not a.adaptive


def test_auto_defaults_leave_headroom():
    s = perf.plan_settings(resources())
    assert s.adaptive
    assert s.requests == perf.DEFAULT_START_REQUESTS
    assert s.max_requests == perf.DEFAULT_MAX_REQUESTS
    assert s.cpu_workers == 7
    assert s.memory_budget == 8 * GIB  # half of the 16 GiB available, not of the 32 GiB total
    assert 64 * 1024**2 <= s.gdal_cache <= 512 * 1024**2
    assert len(s.reasons) >= 6


def test_memory_limit_beats_available_memory():
    s = perf.plan_settings(resources(memory_limit=4 * GIB))
    assert s.memory_budget == 2 * GIB


def test_unknown_memory_gets_conservative_budget():
    s = perf.plan_settings(resources(memory_total=None, memory_available=None))
    assert s.memory_budget == 2 * GIB


def test_open_file_limit_caps_requests():
    s = perf.plan_settings(resources(open_files=64))
    assert s.max_requests == 4  # (64 - 32 reserved) // 8
    assert s.requests <= s.max_requests


def test_requests_above_ceiling_are_clamped():
    s = perf.plan_settings(resources(), requests=50, max_requests=10)
    assert (s.requests, s.max_requests) == (10, 10)


def test_slurm_and_memory_env_are_honoured(monkeypatch):
    monkeypatch.setenv("SLURM_CPUS_PER_TASK", "1")
    monkeypatch.setenv("SLURM_MEM_PER_NODE", "2048")
    cpus, source = perf.detect_cpus()
    assert (cpus, source) == (1, "SLURM_CPUS_PER_TASK")
    _, _, limit, mem_source = perf.detect_memory()
    assert limit is not None and limit <= 2048 * 1024**2
    assert mem_source


def test_detect_resources_runs_on_this_machine():
    r = perf.detect_resources()
    assert r.cpus >= 1
    assert r.memory_total is None or r.memory_total > 0


# --- AdaptiveLimiter ---------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def run_epoch(limiter: perf.AdaptiveLimiter, clock: Clock, rate: float, seconds: float = 5.0):
    """Complete one epoch's reads at `rate` useful bytes per second."""
    n = limiter.limit
    for _ in range(n):
        clock.t += seconds / n + 1e-9  # float sums must not fall short of the epoch
        limiter.record(True, int(rate * seconds / n))


def test_limiter_climbs_while_throughput_grows_then_settles():
    clock = Clock()
    lim = perf.AdaptiveLimiter(4, 32, clock=clock)
    run_epoch(lim, clock, 10e6)  # first epoch: baseline, probe up
    assert lim.limit == 6
    run_epoch(lim, clock, 15e6)  # +50%: keep climbing
    assert lim.limit == 9
    run_epoch(lim, clock, 15.5e6)  # +3%: plateau, back to the best limit
    assert lim.limit == 6
    for _ in range(5):
        run_epoch(lim, clock, 15e6)
    assert lim.limit == 6  # no oscillation while settled
    assert [e.reason for e in lim.events][-1].startswith("no gain")


def test_limiter_halves_on_failure_and_does_not_climb_back_immediately():
    clock = Clock()
    lim = perf.AdaptiveLimiter(16, 32, clock=clock, cooldown_epochs=6)
    lim.record(False)
    assert lim.limit == 16  # a single failure is noise
    lim.record(False)
    assert lim.limit == 8
    lim.record(False)
    assert lim.limit == 8  # same overload: no second halving within one epoch
    clock.t += lim.epoch_seconds
    lim.record(False)
    assert lim.limit == 4
    for rate in (1e6, 2e6, 4e6, 8e6):
        run_epoch(lim, clock, rate)
    # Climbing is capped at the limit that failed last (8) during the cooldown.
    assert lim.limit == 8
    for rate in (16e6, 32e6, 64e6):
        run_epoch(lim, clock, rate)
    assert lim.limit > 8  # after the cooldown it may probe higher again
    assert lim.failures == 4


def test_limiter_reprobes_after_settling():
    clock = Clock()
    lim = perf.AdaptiveLimiter(4, 32, clock=clock, reprobe_epochs=2)
    run_epoch(lim, clock, 10e6)  # 4 -> 6
    run_epoch(lim, clock, 10e6)  # no gain -> back to 4, settle
    assert lim.limit == 4
    run_epoch(lim, clock, 10e6)
    run_epoch(lim, clock, 10e6)  # second settled epoch: probe one step up
    assert lim.limit == 5


def test_fixed_limiter_never_changes():
    clock = Clock()
    lim = perf.AdaptiveLimiter(5, 5, adaptive=False, clock=clock)
    for rate in (1e6, 5e6, 50e6):
        run_epoch(lim, clock, rate)
    lim.record(False)
    assert lim.limit == 5


def test_short_epochs_do_not_decide():
    clock = Clock()
    lim = perf.AdaptiveLimiter(4, 32, clock=clock, epoch_seconds=5.0)
    for _ in range(100):
        clock.t += 0.01  # 1 s in total: under the epoch length
        lim.record(True, 10**6)
    assert lim.limit == 4


def test_slots_block_at_the_limit():
    lim = perf.AdaptiveLimiter(2, 2, adaptive=False)
    entered = []
    release = threading.Event()

    def worker(i: int) -> None:
        with lim.slot():
            entered.append(i)
            release.wait(5)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for _ in range(100):
        if len(entered) == 2:
            break
        threading.Event().wait(0.01)
    threading.Event().wait(0.05)
    assert len(entered) == 2  # the third waits for a slot
    release.set()
    for t in threads:
        t.join(5)
    assert len(entered) == 3


def test_limiter_rejects_bad_bounds():
    with pytest.raises(ValueError, match="minimum"):
        perf.AdaptiveLimiter(0, 4)
    with pytest.raises(ValueError, match="minimum"):
        perf.AdaptiveLimiter(8, 4)


def test_isolated_failures_do_not_reduce_the_limit():
    clock = Clock()
    lim = perf.AdaptiveLimiter(8, 32, clock=clock, failure_window=30.0)
    lim.record(False)
    clock.t += 31.0
    lim.record(False)
    assert lim.limit == 8
    assert lim.failures == 2


def test_light_throttling_is_left_to_the_throughput_measurement():
    clock = Clock()
    lim = perf.AdaptiveLimiter(16, 32, clock=clock)
    lim.throttled(5)  # 5 retried 503s in an epoch of 16 reads
    run_epoch(lim, clock, 10e6)
    assert lim.limit == 24  # first epoch probes up as usual


def test_heavy_throttling_cuts_the_limit_by_a_quarter():
    clock = Clock()
    lim = perf.AdaptiveLimiter(16, 32, clock=clock, cooldown_epochs=3)
    lim.throttled(40)  # more 503s than the epoch's 16 reads
    run_epoch(lim, clock, 10e6)
    assert lim.limit == 12
    assert lim.events[-1].reason == "host throttled"
    run_epoch(lim, clock, 10e6)  # climbing restarts but stays under the cut limit (16)
    run_epoch(lim, clock, 20e6)
    assert lim.limit <= 16


def test_fixed_limiter_ignores_throttling():
    lim = perf.AdaptiveLimiter(16, 16, adaptive=False)
    lim.throttled(100)
    assert lim.limit == 16


@pytest.mark.parametrize("cpus,start,workers", [(1, 4, 1), (2, 8, 2), (4, 16, 3), (16, 16, 15)])
def test_start_concurrency_and_workers_follow_cpus(cpus, start, workers):
    s = perf.plan_settings(resources(cpus=cpus))
    assert (s.requests, s.cpu_workers) == (start, workers)
    worker_reason = next(r for r in s.reasons if r.startswith("cpu_workers"))
    assert ("less one" in worker_reason) == (cpus > 2)
