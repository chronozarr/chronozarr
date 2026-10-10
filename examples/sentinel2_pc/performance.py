"""Resource detection, execution settings and an adaptive request limiter for remote raster reads.

Nothing here knows about Sentinel-2 or Planetary Computer. `plan_settings` turns the machine's
usable CPUs and memory, plus any user overrides, into a small `Settings` record with a reason
for each choice. `AdaptiveLimiter` caps concurrent remote reads and, when adaptation is on,
raises the cap while useful throughput grows by more than `gain`, settles when it stops
growing, and halves it when reads fail repeatedly.
"""

from __future__ import annotations

import math
import os
import platform
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

GIB = 1024**3
MIB = 1024**2

# Concurrent reads in auto mode: start and default ceiling. 16 was the best or tied-best value
# on a home link to Planetary Computer and on a local server without shaping (2026-10-10
# benchmark, README). Climbing above it gained nothing overall there, so by default auto mode only
# goes below 16, when the host pushes back. A higher --max-requests lets it climb (a 50 ms
# link was fastest at 32).
DEFAULT_MAX_REQUESTS = 16
DEFAULT_START_REQUESTS = 16
REQUESTS_PER_CPU = 4
# Fraction of available memory that auto mode budgets for month buffers and the GDAL cache.
MEMORY_FRACTION = 0.5
# Fraction of a cgroup or SLURM memory limit, when that is less than the available memory. The
# limit is memory set aside for this job, not shared with other programs, and the pipeline's
# estimate exceeded the measured peak RSS in every Docker run (by 7 to 45 %, 2026-10-10), so a
# quarter of it is left as headroom instead of half.
LIMIT_FRACTION = 0.75
DEDICATED_LIMITS = ("cgroup memory limit", "SLURM_MEM_PER_NODE", "SLURM_MEM_PER_CPU")


@dataclass(frozen=True)
class Resources:
    """What this process may use. `None` means the value could not be determined."""

    cpus: int
    cpu_source: str
    memory_total: int | None
    memory_available: int | None
    memory_limit: int | None
    memory_source: str
    open_files: int | None


@dataclass(frozen=True)
class Settings:
    """Resolved execution settings for one run."""

    adaptive: bool
    requests: int  # starting (or, without adaptation, fixed) number of concurrent remote reads
    max_requests: int
    cpu_workers: int  # threads for compositing and other CPU work
    memory_budget: int  # bytes for month buffers and the GDAL block cache
    memory_budget_source: str  # "set by user" or how it was derived
    gdal_cache: int  # bytes, process-wide GDAL block cache
    reasons: tuple[str, ...] = field(default=())


def parse_bytes(text: str) -> int:
    """'4GB', '512MiB', '2g', '1000000' -> bytes; GB and GiB both mean 1024**3."""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kmgt]?)(?:i?b)?\s*", text.lower())
    if not match:
        raise ValueError(f"cannot parse a byte size from {text!r}; use e.g. 4GB or 512MB")
    value, unit = float(match.group(1)), match.group(2)
    return int(value * 1024 ** "_kmgt".index(unit)) if unit else int(value)


def _read_int(path: str) -> int | None:
    try:
        text = Path(path).read_text().strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def _cgroup_cpus() -> float | None:
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if quota != "max":
            return int(quota) / int(period)
    except (OSError, ValueError):
        pass
    quota_v1 = _read_int("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
    period_v1 = _read_int("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
    if quota_v1 and period_v1:
        return quota_v1 / period_v1
    return None


def detect_cpus() -> tuple[int, str]:
    """Usable CPUs: affinity, cgroup quota and SLURM allocation, whichever is smallest."""
    if hasattr(os, "process_cpu_count"):
        count, source = os.process_cpu_count() or 1, "process affinity"
    elif hasattr(os, "sched_getaffinity"):
        count, source = len(os.sched_getaffinity(0)), "process affinity"
    else:
        count, source = os.cpu_count() or 1, "logical CPUs"
    quota = _cgroup_cpus()
    if quota is not None and math.ceil(quota) < count:
        count, source = max(1, math.ceil(quota)), "cgroup CPU quota"
    slurm = os.environ.get("SLURM_CPUS_PER_TASK")
    if slurm and slurm.isdigit() and int(slurm) < count:
        count, source = int(slurm), "SLURM_CPUS_PER_TASK"
    return count, source


def _macos_available() -> int | None:
    try:
        out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    page = re.search(r"page size of (\d+) bytes", out)
    pages = {
        m.group(1): int(m.group(2)) for m in re.finditer(r"Pages (\w[\w ]*?):\s+(\d+)\.", out)
    }
    if not page:
        return None
    free = sum(pages.get(k, 0) for k in ("free", "inactive", "speculative", "purgeable"))
    return free * int(page.group(1))


def _linux_available() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except OSError:
        pass
    return None


def detect_memory() -> tuple[int | None, int | None, int | None, str]:
    """(total, available, limit, source). The limit is a cgroup, SLURM or rlimit cap if any."""
    total = None
    if hasattr(os, "sysconf") and "SC_PHYS_PAGES" in os.sysconf_names:
        try:
            total = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        except (OSError, ValueError):
            total = None
    if sys.platform == "darwin":
        available = _macos_available()
    elif sys.platform.startswith("linux"):
        available = _linux_available()
    else:
        available = None

    limits: list[tuple[int, str]] = []
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        value = _read_int(path)
        if value and (total is None or value < total):
            usage = _read_int(
                path.replace("memory.max", "memory.current").replace(
                    "limit_in_bytes", "usage_in_bytes"
                )
            )
            limits.append((value - (usage or 0), "cgroup memory limit"))
    slurm_node = os.environ.get("SLURM_MEM_PER_NODE")
    if slurm_node and slurm_node.isdigit():
        limits.append((int(slurm_node) * MIB, "SLURM_MEM_PER_NODE"))
    slurm_cpu = os.environ.get("SLURM_MEM_PER_CPU")
    if slurm_cpu and slurm_cpu.isdigit():
        cpus = os.environ.get("SLURM_CPUS_PER_TASK", "1")
        limits.append(
            (int(slurm_cpu) * MIB * int(cpus if cpus.isdigit() else 1), "SLURM_MEM_PER_CPU")
        )
    try:
        import resource

        for name in ("RLIMIT_AS", "RLIMIT_DATA"):
            soft, _ = resource.getrlimit(getattr(resource, name))
            if soft != resource.RLIM_INFINITY and soft > 0:
                limits.append((soft, name))
    except (ImportError, AttributeError, ValueError, OSError):
        pass

    if limits:
        limit, source = min(limits)
        return total, available, limit, source
    return total, available, None, "available memory" if available else "physical memory"


def detect_open_files() -> int | None:
    try:
        import resource

        soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (ImportError, AttributeError, ValueError, OSError):
        return None
    return None if soft == resource.RLIM_INFINITY else int(soft)


def detect_resources() -> Resources:
    cpus, cpu_source = detect_cpus()
    total, available, limit, memory_source = detect_memory()
    return Resources(
        cpus=cpus,
        cpu_source=cpu_source,
        memory_total=total,
        memory_available=available,
        memory_limit=limit,
        memory_source=memory_source,
        open_files=detect_open_files(),
    )


def _gib(n: int) -> str:
    return f"{n / GIB:.1f} GiB"


def plan_settings(
    resources: Resources,
    *,
    adaptive: bool = True,
    requests: int | None = None,
    max_requests: int | None = None,
    cpu_workers: int | None = None,
    memory_budget: int | None = None,
) -> Settings:
    """Resolve settings from detected resources and explicit overrides.

    With `adaptive=False` the request concurrency never changes during a run. The memory
    budget still follows the memory available at the start unless `memory_budget` is given;
    it only decides how many months are read at once, never the output. `requests` is the
    start value in adaptive mode and the fixed value otherwise; `max_requests` caps it in both.
    """
    reasons: list[str] = [
        f"{resources.cpus} usable CPUs ({resources.cpu_source})",
    ]

    if cpu_workers is None:
        if resources.cpus > 2:
            cpu_workers = resources.cpus - 1
            reasons.append(f"cpu_workers={cpu_workers}: usable CPUs less one for the scheduler")
        else:
            cpu_workers = resources.cpus
            reasons.append(f"cpu_workers={cpu_workers}: all {resources.cpus} usable CPUs")
    else:
        reasons.append(f"cpu_workers={cpu_workers}: set by user")

    if max_requests is None:
        max_requests = DEFAULT_MAX_REQUESTS
        why = f"default ceiling {DEFAULT_MAX_REQUESTS}; raise it to let auto mode climb higher"
        # A read holds up to about 8 descriptors (connections, the file); keep 32 for the rest.
        by_files = None if resources.open_files is None else (resources.open_files - 32) // 8
        if by_files is not None and by_files < max_requests:
            max_requests = max(1, by_files)
            why = (
                f"open-file limit {resources.open_files}: about 8 descriptors per read after "
                "32 for everything else"
            )
        reasons.append(f"max_requests={max_requests}: {why}")
    else:
        reasons.append(f"max_requests={max_requests}: set by user")

    if requests is None:
        # Every read also decodes; on 1-2 CPUs 16 at once ran slower than 4 (2026-10-10, Docker).
        requests = min(DEFAULT_START_REQUESTS, REQUESTS_PER_CPU * resources.cpus, max_requests)
        if not adaptive:
            why = "fixed default"
        elif requests < max_requests:
            why = f"start value; climbs toward {max_requests} while throughput rises"
        else:
            why = "start value; lowered only if the host fails or throttles reads"
        reasons.append(f"requests={requests}: {why}")
    else:
        requests = min(requests, max_requests)
        reasons.append(f"requests={requests}: set by user")
    if not adaptive:
        max_requests = requests
        reasons.append("adaptation off: request concurrency stays fixed")

    if memory_budget is None:
        candidates = [
            v for v in (resources.memory_available, resources.memory_limit) if v is not None
        ]
        base = min(candidates) if candidates else resources.memory_total
        dedicated = (
            resources.memory_limit is not None
            and base == resources.memory_limit
            and resources.memory_source in DEDICATED_LIMITS
        )
        fraction = LIMIT_FRACTION if dedicated else MEMORY_FRACTION
        if base is None:
            memory_budget = 2 * GIB
            budget_source = "memory size unknown, conservative default"
            reasons.append(f"memory_budget=2.0 GiB: {budget_source}")
        else:
            memory_budget = max(256 * MIB, int(base * fraction))
            budget_source = f"{int(fraction * 100)}% of {_gib(base)} ({resources.memory_source})"
            reasons.append(f"memory_budget={_gib(memory_budget)}: {budget_source}")
    else:
        budget_source = "set by user"
        reasons.append(f"memory_budget={_gib(memory_budget)}: set by user")

    # GDAL's default block cache is 5% of physical RAM (6.4 GiB on a 128 GiB machine), outside any
    # budget. One windowed read decodes a few dozen 512 x 512 blocks, so a small cache suffices.
    gdal_cache = int(min(512 * MIB, max(64 * MIB, memory_budget // 16)))
    reasons.append(f"gdal_cache={gdal_cache // MIB} MiB: 1/16 of the budget, 64-512 MiB")

    # GDAL_NUM_THREADS stays at GDAL's default of 1. With 2 or more, GTiff fetches the needed
    # tiles in one multi-range batch: 22% fewer bytes on a partial window, but 30-45% longer
    # wall time on a ~20 MB/s link (2026-10-10, 8 concurrent reads), so streaming wins.
    return Settings(
        adaptive=adaptive,
        requests=requests,
        max_requests=max_requests,
        cpu_workers=cpu_workers,
        memory_budget=memory_budget,
        memory_budget_source=budget_source,
        gdal_cache=gdal_cache,
        reasons=tuple(reasons),
    )


def describe_machine() -> dict:
    """Platform facts recorded with benchmark results and diagnostics."""
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "python": platform.python_version(),
    }


@dataclass
class LimiterEvent:
    t: float
    limit: int
    rate: float | None  # useful bytes per second in the epoch that ended
    reason: str


class AdaptiveLimiter:
    """Concurrency cap for remote reads, optionally adapted to useful throughput.

    Adaptation works in epochs of at least `epoch_seconds` and `limit` completed reads. Starting
    from `start`, each epoch whose throughput beats the previous one by more than `gain` raises
    the limit by half (at least 1) up to `maximum`; the first epoch that does not settles on the
    best limit seen. An epoch with more throttling responses (HTTP 429/5xx retried below this
    layer, reported through `throttled`) than completed reads cuts the limit by a quarter. A
    failed read within `failure_window` seconds of the previous failure halves it. Either way,
    climbing stays below the limit that was cut for `cooldown_epochs`. After `reprobe_epochs`
    settled epochs it probes one step up again, so a link that got faster is noticed. Without
    adaptation the limit stays at `start`.
    """

    def __init__(
        self,
        start: int,
        maximum: int,
        *,
        minimum: int = 1,
        adaptive: bool = True,
        gain: float = 0.10,
        epoch_seconds: float = 3.0,
        reprobe_epochs: int = 12,
        cooldown_epochs: int = 12,
        failure_window: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 1 <= minimum <= start <= maximum:
            raise ValueError(
                f"need 1 <= minimum <= start <= maximum, got {minimum}, {start}, {maximum}"
            )
        self.minimum, self.maximum, self.adaptive = minimum, maximum, adaptive
        self.gain, self.epoch_seconds = gain, epoch_seconds
        self.reprobe_epochs, self.cooldown_epochs = reprobe_epochs, cooldown_epochs
        self._clock = clock
        self.failure_window = failure_window
        self._last_failure: float | None = None
        self._last_backoff: float | None = None
        self.throttle_events = 0
        self._epoch_throttles = 0
        self._cond = threading.Condition()
        self._limit = start
        self._in_use = 0
        self._mode = "climb"
        self._prev_rate: float | None = None
        self._best_limit = start
        self._settled_epochs = 0
        self._ceiling = maximum
        self._ceiling_epochs = 0
        self._epoch_start = clock()
        self._epoch_bytes = 0
        self._epoch_reads = 0
        self.failures = 0
        self.reads = 0
        self.busy_seconds = 0.0
        self.events: list[LimiterEvent] = [LimiterEvent(0.0, start, None, "start")]
        self._t0 = self._epoch_start

    @property
    def limit(self) -> int:
        return self._limit

    @contextmanager
    def slot(self) -> Iterator[None]:
        """Hold one of `limit` concurrent read slots."""
        with self._cond:
            while self._in_use >= self._limit:
                self._cond.wait()
            self._in_use += 1
        started = self._clock()
        try:
            yield
        finally:
            with self._cond:
                self._in_use -= 1
                self.busy_seconds += self._clock() - started
                self._cond.notify_all()

    def record(self, ok: bool, useful_bytes: int = 0) -> None:
        """Report one finished read; may change the limit."""
        with self._cond:
            now = self._clock()
            self.reads += 1
            if not ok:
                self.failures += 1
                self._failure(now, "read failed")
                return
            self._epoch_bytes += useful_bytes
            self._epoch_reads += 1
            elapsed = now - self._epoch_start
            if (
                not self.adaptive
                or elapsed < self.epoch_seconds
                or self._epoch_reads < self._limit
            ):
                return
            rate = self._epoch_bytes / elapsed
            if self._epoch_throttles > self._epoch_reads:
                self._back_off(self._limit * 3 // 4, now, rate, "host throttled")
                return
            self._end_epoch(rate, now)
            self._restart_epoch(now)

    def throttled(self, total_events: int) -> None:
        """Report the running count of throttling responses (HTTP 429/5xx) seen anywhere.

        These are responses the HTTP layer retried by itself, so they never reach `record`.
        One read makes several HTTP requests, and a few 503s cost little that the throughput
        measurement does not already see; an epoch that averages more than one per completed
        read cuts the limit by a quarter (see `_end_epoch`).
        """
        with self._cond:
            new = total_events - self.throttle_events
            if new > 0:
                self.throttle_events = total_events
                self._epoch_throttles += new

    def _failure(self, now: float, reason: str) -> None:
        # One failure is noise (a DNS hiccup, a dropped connection); a second within
        # `failure_window` seconds is treated as throttling or overload. Failures within one
        # epoch of a back-off belong to the same overload and do not halve again.
        recent = self._last_failure is not None and (
            now - self._last_failure <= self.failure_window
        )
        self._last_failure = now
        settling = self._last_backoff is not None and now - self._last_backoff < self.epoch_seconds
        if not self.adaptive or not recent or settling:
            return
        self._back_off(self._limit // 2, now, None, reason)

    def _back_off(self, limit: int, now: float, rate: float | None, reason: str) -> None:
        """Lower the limit and keep climbing below the old one for `cooldown_epochs`."""
        self._last_backoff = now
        self._ceiling = max(self.minimum, self._limit)
        self._ceiling_epochs = self.cooldown_epochs
        self._set(max(self.minimum, limit), now, rate, reason)
        self._mode, self._prev_rate = "climb", None
        self._best_limit = self._limit
        self._restart_epoch(now)

    def _restart_epoch(self, now: float) -> None:
        self._epoch_start, self._epoch_bytes, self._epoch_reads = now, 0, 0
        self._epoch_throttles = 0

    def _set(self, limit: int, now: float, rate: float | None, reason: str) -> None:
        if limit != self._limit or reason in ("read failed", "host throttled"):
            self.events.append(LimiterEvent(now - self._t0, limit, rate, reason))
        self._limit = limit
        self._cond.notify_all()

    def _end_epoch(self, rate: float, now: float) -> None:
        if self._ceiling_epochs > 0:
            self._ceiling_epochs -= 1
            if self._ceiling_epochs == 0:
                # The cap after a failure has expired: measure again from here and climb.
                self._ceiling = self.maximum
                self._mode, self._prev_rate = "climb", None
        top = min(self.maximum, self._ceiling)
        if self._mode == "hold":
            self._settled_epochs += 1
            if self._settled_epochs < self.reprobe_epochs or self._limit >= top:
                return
            self._mode, self._prev_rate, self._best_limit = "climb", rate, self._limit
            self._set(self._limit + 1, now, rate, "re-probe one step up")
            return
        if self._prev_rate is None or rate > self._prev_rate * (1 + self.gain):
            self._prev_rate, self._best_limit = rate, self._limit
            if self._limit < top:
                step = max(1, self._limit // 2)
                self._set(min(top, self._limit + step), now, rate, "throughput rose; probe up")
            else:
                self._mode, self._settled_epochs = "hold", 0
                self.events.append(LimiterEvent(now - self._t0, self._limit, rate, "at ceiling"))
            return
        self._mode, self._settled_epochs = "hold", 0
        if self._best_limit == self._limit:
            self.events.append(LimiterEvent(now - self._t0, self._limit, rate, "settle"))
        else:
            self._set(self._best_limit, now, rate, f"no gain above {self._best_limit}; settle")
