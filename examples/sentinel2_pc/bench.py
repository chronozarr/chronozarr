"""Repeatable benchmark for the Sentinel-2 ingest example (mosaic.py).

Everything lives under data/bench/ (gitignored):

    workloads/NAME.json     frozen scene lists (STAC searched once, then never again)
    mirror/<url path>       full copies of the assets, shared by all workloads
    results/NAME.jsonl      one JSON line per measured run (failures included)
    results/summary.json    what `summarize` printed

Typical session (the first three steps touch Planetary Computer; the rest can run offline):

    uv run python examples/sentinel2_pc/bench.py freeze --workload ucayali-1m
    uv run python examples/sentinel2_pc/bench.py mirror --workload ucayali-1m --max-gb 10
    uv run python examples/sentinel2_pc/bench.py lab-workload \\
        --workload ucayali-1m --as ucayali-1m-lab
    uv run python examples/sentinel2_pc/bench.py compare --workloads ucayali-1m-lab \\
        --configs baseline,fixed-8,auto --reps 3 --lab 40,15,0.01
    uv run python examples/sentinel2_pc/bench.py summarize

Against the real host (shared infrastructure, so keep the reps few):

    uv run python examples/sentinel2_pc/bench.py compare --workloads yukon-sparse-3m \\
        --configs baseline,auto --reps 2

One measurement in the current process (`compare` runs this in a fresh subprocess per run so
that no run inherits another's caches or memory):

    uv run python examples/sentinel2_pc/bench.py run --workload yukon-sparse-3m --config auto

Configs:
    baseline      the old pipeline from git ref --baseline-ref (default 01185cd)
    fixed-N       new pipeline, N concurrent reads, no adaptation
    auto          new pipeline, adaptive defaults
    auto-capped   new pipeline, adaptive, max_requests=8, cpu_workers=4, memory_budget=2 GiB
    auto-memN     new pipeline, adaptive, memory_budget=N MiB (the --memory override)

Lab workloads (`lab-workload`) read from a labserver.py process that `run` starts on
data/bench/mirror, with optional shaping `--lab latency_ms,bandwidth_mbps,fail_rate`. The server
is a separate process so its CPU is not counted in the run's CPU seconds. Network numbers in lab
runs are loopback numbers; the NIC byte counter then reads about zero.

`--warm` runs the workload twice in the same process and records the second run. It measures
in-process state only (GDAL's block and header caches, Python and numpy allocations, the
planetary_computer token cache); it does not warm any HTTP cache on the far side. Peak RSS is
the maximum over both runs.

`summarize` checks correctness: for every (workload, month) the SHA-256 of
`bands.tobytes() + coverage.tobytes()` must equal the baseline's.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib
import inspect
import json
import os
import random
import resource
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import urlsplit

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
BENCH = REPO / "data" / "bench"
WORKLOADS_DIR = BENCH / "workloads"
MIRROR_DIR = BENCH / "mirror"
RESULTS_DIR = BENCH / "results"
AOIS_FILE = HERE / "aois.yaml"

DEFAULT_BASELINE_REF = "01185cd"
ASSET_NAMES = ("B02", "B03", "B04", "B08", "SCL")
SMALL_AOI_HALF_DEGREES = 0.0225  # about 2.5 km, so a 5 km box

# Built-in workloads. Keys: aoi, start, end, and optionally
#   half_degrees       shrink the AOI to a box of +-half_degrees around its centre
#   epsg               target EPSG instead of the AOI's own
#   same_scenes_as     reuse the frozen scene list of another workload
WORKLOADS: dict[str, dict] = {
    "ucayali-1m": {
        "aoi": "ucayali_santa_maria",
        "start": "2024-07-01",
        "end": "2024-07-28",
    },
    "ucayali-small-3m": {
        "aoi": "ucayali_santa_maria",
        "start": "2024-01-01",
        "end": "2024-03-31",
        "half_degrees": SMALL_AOI_HALF_DEGREES,
    },
    "lakemead-1m": {
        "aoi": "lake_mead",
        "start": "2024-01-01",
        "end": "2024-01-31",
    },
    "yukon-sparse-3m": {
        "aoi": "yukon_ykd",
        "start": "2024-06-01",
        "end": "2024-08-31",
    },
    "ucayali-warp-1m": {
        "aoi": "ucayali_santa_maria",
        "start": "2024-07-01",
        "end": "2024-07-28",
        "epsg": 32719,
        "same_scenes_as": "ucayali-1m",
    },
}


# --- workload files ----------------------------------------------------------------------------


def as_bbox(values: list[float]) -> tuple[float, float, float, float]:
    lon_min, lat_min, lon_max, lat_max = values
    return lon_min, lat_min, lon_max, lat_max


def scenes_sha256(scenes_by_month: dict) -> str:
    text = json.dumps(scenes_by_month, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def load_aoi(name: str) -> dict:
    import yaml

    aois = yaml.safe_load(AOIS_FILE.read_text())["aois"]
    if name not in aois:
        raise SystemExit(f"AOI {name!r} is not in {AOIS_FILE}; choose one of {sorted(aois)}")
    return aois[name]


def workload_path(name: str) -> Path:
    return WORKLOADS_DIR / f"{name}.json"


def load_workload(name: str) -> dict:
    path = workload_path(name)
    if not path.exists():
        raise SystemExit(
            f"no frozen workload at {path}; run `bench.py freeze --workload {name}` "
            "(built-in names) or `bench.py lab-workload` first"
        )
    workload = json.loads(path.read_text())
    if scenes_sha256(workload["scenes_by_month"]) != workload["sha256"]:
        raise SystemExit(
            f"{path} was edited after it was frozen (sha256 mismatch); freeze it again"
        )
    return workload


def save_workload(workload: dict) -> Path:
    WORKLOADS_DIR.mkdir(parents=True, exist_ok=True)
    workload["sha256"] = scenes_sha256(workload["scenes_by_month"])
    path = workload_path(workload["name"])
    path.write_text(json.dumps(workload, indent=1) + "\n")
    return path


def scene_to_dict(scene) -> dict:
    return {
        "item_id": scene.item_id,
        "datetime": scene.datetime.isoformat(),
        "cloud_cover": scene.cloud_cover,
        "epsg": scene.epsg,
        "mgrs_tile": scene.mgrs_tile,
        "processing_baseline": scene.processing_baseline,
        "asset_hrefs": dict(scene.asset_hrefs),
    }


def scene_from_dict(scene_ref_class, data: dict):
    return scene_ref_class(
        item_id=data["item_id"],
        datetime=date.fromisoformat(data["datetime"]),
        cloud_cover=data["cloud_cover"],
        epsg=data["epsg"],
        mgrs_tile=data["mgrs_tile"],
        processing_baseline=data["processing_baseline"],
        asset_hrefs=dict(data["asset_hrefs"]),
    )


def all_hrefs(workload: dict) -> list[str]:
    seen: dict[str, None] = {}
    for scenes in workload["scenes_by_month"].values():
        for scene in scenes:
            for asset in ASSET_NAMES:
                seen[scene["asset_hrefs"][asset]] = None
    return list(seen)


def mirror_path(href: str) -> Path:
    return MIRROR_DIR / urlsplit(href).path.lstrip("/")


# --- freeze ------------------------------------------------------------------------------------


def cmd_freeze(args: argparse.Namespace) -> None:
    if args.workload not in WORKLOADS:
        raise SystemExit(f"unknown workload {args.workload!r}; built in: {sorted(WORKLOADS)}")
    spec = WORKLOADS[args.workload]
    aoi = load_aoi(spec["aoi"])
    bbox = [float(v) for v in aoi["bbox"]]
    if "half_degrees" in spec:
        lon = (bbox[0] + bbox[2]) / 2
        lat = (bbox[1] + bbox[3]) / 2
        half = spec["half_degrees"]
        bbox = [lon - half, lat - half, lon + half, lat + half]
    epsg = spec.get("epsg", aoi["epsg"])

    source = spec.get("same_scenes_as")
    if source is not None:
        scenes_by_month = load_workload(source)["scenes_by_month"]
    else:
        sys.path.insert(0, str(HERE))
        from catalog import search_scenes_by_month

        found = search_scenes_by_month(as_bbox(bbox), spec["start"], spec["end"])
        scenes_by_month = {m: [scene_to_dict(s) for s in scenes] for m, scenes in found.items()}

    workload = {
        "name": args.workload,
        "aoi": spec["aoi"],
        "bbox": bbox,
        "epsg": epsg,
        "start": spec["start"],
        "end": spec["end"],
        "scenes_by_month": scenes_by_month,
        "frozen_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    path = save_workload(workload)
    counts = {m: len(s) for m, s in scenes_by_month.items()}
    print(f"froze {args.workload}: {sum(counts.values())} scenes {counts} -> {path}")


# --- mirror ------------------------------------------------------------------------------------


def _head_size(url: str) -> int:
    request = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(request, timeout=60) as response:
        return int(response.headers["Content-Length"])


def _download(href: str, size: int, sign) -> None:
    target = mirror_path(href)
    target.parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    for attempt in range(3):
        tmp = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            with (
                urllib.request.urlopen(sign(href), timeout=120) as response,
                tmp.open("wb") as f,
            ):
                shutil.copyfileobj(response, f, length=1024 * 1024)
            if tmp.stat().st_size != size:
                raise OSError(f"got {tmp.stat().st_size} bytes, expected {size}")
            os.replace(tmp, target)
            return
        except OSError as e:
            last_error = e
            tmp.unlink(missing_ok=True)
            time.sleep(2**attempt)
    raise RuntimeError(f"cannot download {href}: {last_error}")


def cmd_mirror(args: argparse.Namespace) -> None:
    sys.path.insert(0, str(HERE))
    from catalog import sign_href

    workload = load_workload(args.workload)
    hrefs = all_hrefs(workload)
    print(f"{args.workload}: {len(hrefs)} assets; reading sizes (HEAD)")
    with ThreadPoolExecutor(4) as pool:
        sizes = dict(zip(hrefs, pool.map(lambda h: _head_size(sign_href(h)), hrefs), strict=True))
    total = sum(sizes.values())
    print(f"total {total / 1e9:.2f} GB (limit {args.max_gb} GB)")
    if total > args.max_gb * 1e9:
        raise SystemExit(
            f"{total / 1e9:.2f} GB is over --max-gb {args.max_gb}; raise the limit or pick a "
            "smaller workload"
        )

    todo = []
    for href in hrefs:
        path = mirror_path(href)
        if path.exists() and path.stat().st_size == sizes[href]:
            continue
        todo.append(href)
    print(f"{len(hrefs) - len(todo)} already mirrored, {len(todo)} to download")

    done_bytes = 0
    done_files = 0
    lock = threading.Lock()

    def fetch(href: str) -> None:
        nonlocal done_bytes, done_files
        _download(href, sizes[href], sign_href)
        with lock:
            done_bytes += sizes[href]
            done_files += 1
            print(
                f"[{done_files}/{len(todo)}] {sizes[href] / 1e6:7.1f} MB "
                f"{urlsplit(href).path.rsplit('/', 1)[-1]} (total {done_bytes / 1e9:.2f} GB)",
                flush=True,
            )

    with ThreadPoolExecutor(4) as pool:
        for future in [pool.submit(fetch, h) for h in todo]:
            future.result()
    print(f"mirror complete under {MIRROR_DIR}")


# --- lab workloads -----------------------------------------------------------------------------


def cmd_lab_workload(args: argparse.Namespace) -> None:
    source = load_workload(args.workload)
    copies = args.alias_copies

    missing = [h for h in all_hrefs(source) if not mirror_path(h).exists()]
    if missing:
        raise SystemExit(
            f"{len(missing)} of {len(all_hrefs(source))} assets are not mirrored (first: "
            f"{mirror_path(missing[0])}); run `bench.py mirror --workload {args.workload}` first"
        )

    def lab_href(href: str, alias: int | None) -> str:
        path = urlsplit(href).path.lstrip("/")
        return f"lab://{path}" if alias is None else f"lab://a{alias}/{path}"

    scenes_by_month: dict[str, list[dict]] = {}
    for month, scenes in source["scenes_by_month"].items():
        out = []
        for alias in [None] if copies is None else range(copies):
            for scene in scenes:
                item_id = scene["item_id"] if alias is None else f"{scene['item_id']}-a{alias}"
                out.append(
                    {
                        **scene,
                        "item_id": item_id,
                        "asset_hrefs": {
                            k: lab_href(v, alias) for k, v in scene["asset_hrefs"].items()
                        },
                    }
                )
        scenes_by_month[month] = out

    workload = {
        **{k: source[k] for k in ("aoi", "bbox", "epsg", "start", "end")},
        "name": args.new_name,
        "lab": True,
        "source_workload": args.workload,
        "alias_copies": copies,
        "scenes_by_month": scenes_by_month,
        "frozen_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    path = save_workload(workload)
    counts = {m: len(s) for m, s in scenes_by_month.items()}
    print(f"lab workload {args.new_name}: {sum(counts.values())} scenes {counts} -> {path}")


def resolve_lab_hrefs(workload: dict, base_url: str) -> dict:
    """The scenes_by_month of a lab workload with `lab://` hrefs pointed at the server."""
    resolved: dict[str, list[dict]] = {}
    for month, scenes in workload["scenes_by_month"].items():
        resolved[month] = [
            {
                **scene,
                "asset_hrefs": {
                    k: v.replace("lab://", f"{base_url}/", 1)
                    for k, v in scene["asset_hrefs"].items()
                },
            }
            for scene in scenes
        ]
    return resolved


def parse_lab(text: str | None) -> dict | None:
    """'40,15,0.01,12' -> latency_ms, bandwidth_mbps, fail_rate, max_inflight (tail optional)."""
    if text is None:
        return None
    names = ("latency_ms", "bandwidth_mbps", "fail_rate", "max_inflight")
    parts = text.split(",")
    if len(parts) > len(names):
        raise SystemExit(f"--lab takes at most {len(names)} values {names}, got {text!r}")
    return {name: float(value) for name, value in zip(names, parts, strict=False) if value}


class LabProcess:
    """A labserver.py subprocess on data/bench/mirror."""

    def __init__(self, shaping: dict | None, seed: int) -> None:
        command = [
            sys.executable,
            str(HERE / "labserver.py"),
            "--root",
            str(MIRROR_DIR),
            "--port",
            "0",
            "--seed",
            str(seed),
        ]
        for name, value in (shaping or {}).items():
            command += [f"--{name.replace('_', '-')}", str(value)]
        self.process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        assert self.process.stdout is not None
        first_line = self.process.stdout.readline().strip()
        if not first_line.startswith("http://"):
            self.stop()
            raise RuntimeError(f"lab server did not start: {first_line!r}")
        self.base_url = first_line

    def _request(self, method: str, path: str) -> dict:
        request = urllib.request.Request(self.base_url + path, method=method)
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())

    def stats(self) -> dict:
        return self._request("GET", "/_stats")

    def reset(self) -> None:
        self._request("POST", "/_reset")

    def stop(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        for stream in (self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()


# --- measurement helpers -----------------------------------------------------------------------


def peak_rss_bytes() -> int:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024  # Linux reports KiB


def nic_rx_bytes() -> int | None:
    """Bytes received by the machine's network interfaces so far, or None when not available."""
    try:
        if sys.platform == "darwin":
            out = subprocess.run(
                ["netstat", "-ib", "-I", "en0"], capture_output=True, text=True, timeout=5
            ).stdout.splitlines()
            column = out[0].split().index("Ibytes")
            return int(out[1].split()[column])
        if sys.platform.startswith("linux"):
            total = 0
            for line in Path("/proc/net/dev").read_text().splitlines()[2:]:
                name, counters = line.split(":", 1)
                if name.strip() != "lo":
                    total += int(counters.split()[0])
            return total
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None
    return None


def _gdal_functions():
    """(get_json, reset, free) from the libgdal rasterio uses, or None."""
    import rasterio

    package = Path(rasterio.__file__).parent
    candidates: list[Path | None] = [None]  # None: symbols already loaded in this process
    for directory in (package.parent / "rasterio.libs", package / ".dylibs", package / ".libs"):
        candidates += sorted(
            p for p in directory.glob("libgdal*") if ".so" in p.name or ".dylib" in p.name
        )
    for candidate in candidates:
        try:
            lib = ctypes.CDLL(None if candidate is None else str(candidate))
            get_json = lib.VSINetworkStatsGetAsSerializedJSON
            reset = lib.VSINetworkStatsReset
            free = lib.VSIFree
        except (OSError, AttributeError):
            continue
        get_json.argtypes = [ctypes.c_void_p]
        get_json.restype = ctypes.c_void_p
        free.argtypes = [ctypes.c_void_p]
        free.restype = None
        return get_json, reset, free
    return None


def gdal_network_reset() -> None:
    functions = _gdal_functions()
    if functions is not None:
        functions[1]()


def gdal_network_stats() -> dict | None:
    """GET and HEAD counts and downloaded bytes over every file GDAL opened over the network.

    Needs CPL_VSIL_NETWORK_STATS_ENABLED=YES in the environment before GDAL starts. The top-level
    "methods" entry of GDAL's JSON is the sum over all handlers and files.
    """
    functions = _gdal_functions()
    if functions is None:
        return None
    get_json, _, free = functions
    pointer = get_json(None)
    if not pointer:
        return None
    try:
        text = ctypes.string_at(pointer).decode()
    finally:
        free(pointer)
    methods = json.loads(text).get("methods", {})
    return {
        "get_requests": methods.get("GET", {}).get("count", 0),
        "head_requests": methods.get("HEAD", {}).get("count", 0),
        "downloaded_bytes": sum(m.get("downloaded_bytes", 0) for m in methods.values()),
    }


def git_info(ref: str = "HEAD") -> tuple[str, bool]:
    sha = subprocess.run(
        ["git", "rev-parse", "--short", ref], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    return sha, dirty


def month_arrays(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """(bands, coverage k/n float32) of a month file of either pipeline.

    Months of the old pipeline are .npz; GeoTIFF months hold the valid-scene count k as their
    last band and n in their metadata, and k / n in float32 is the array the .npz stored.
    """
    if path.suffix == ".npz":
        with np.load(path, allow_pickle=False) as data:
            return data["bands"], data["coverage"]
    import rasterio

    with rasterio.open(path) as src:
        stack = src.read()
        n = json.loads(src.tags()["scenes_searched"])
    count = stack[-1]
    coverage = count.astype(np.float32) / n if n else np.zeros(count.shape, dtype=np.float32)
    return stack[:-1], coverage


def hash_outputs(out_dir: Path) -> tuple[dict[str, str], int, int]:
    """({month: sha256 of bands+coverage bytes}, total band pixels, total band bytes)."""
    shas: dict[str, str] = {}
    pixels = 0
    band_bytes = 0
    for path in sorted([*out_dir.glob("*.npz"), *out_dir.glob("*.tif")]):
        bands, coverage = month_arrays(path)
        shas[path.stem] = hashlib.sha256(bands.tobytes() + coverage.tobytes()).hexdigest()
        pixels += bands.size
        band_bytes += bands.nbytes
    return shas, pixels, band_bytes


# --- running one measurement -------------------------------------------------------------------


def load_baseline_modules(ref: str, directory: Path):
    """(catalog, mosaic) of the old pipeline, imported from copies of the files at git `ref`.

    The old files import `catalog` by name, so the copy directory goes first on sys.path while
    they load. Both names are removed from sys.modules afterwards. Only one pipeline, old or
    new, is loaded per process.
    """
    for name in ("catalog", "mosaic"):
        if name in sys.modules:
            raise RuntimeError(f"{name} is already imported; load one pipeline per process")
        text = subprocess.run(
            ["git", "show", f"{ref}:examples/sentinel2_pc/{name}.py"],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        (directory / f"{name}.py").write_text(text)
    sys.path.insert(0, str(directory))
    importlib.invalidate_caches()
    try:
        catalog = importlib.import_module("catalog")
        mosaic = importlib.import_module("mosaic")
    finally:
        sys.path.remove(str(directory))
        for name in ("catalog", "mosaic"):
            sys.modules.pop(name, None)
    return catalog, mosaic


def settings_for(config: str, resources, performance):
    gib = 1024**3
    if config == "auto":
        return performance.plan_settings(resources, adaptive=True)
    if config == "auto-capped":
        return performance.plan_settings(
            resources, adaptive=True, max_requests=8, cpu_workers=4, memory_budget=2 * gib
        )
    if config.startswith("auto-mem") and config[8:].isdigit():
        mib = 1024**2
        return performance.plan_settings(
            resources, adaptive=True, memory_budget=int(config[8:]) * mib
        )
    if config.startswith("auto-max") and config[8:].isdigit():
        return performance.plan_settings(resources, adaptive=True, max_requests=int(config[8:]))
    if config.startswith("fixed-") and config[6:].isdigit():
        return performance.plan_settings(resources, adaptive=False, requests=int(config[6:]))
    raise SystemExit(
        f"unknown config {config!r}; use baseline, fixed-N, auto, auto-maxN, auto-memN, "
        "or auto-capped, optionally followed by -rowsN (rows per strip)"
    )


def baseline_runner(
    ref: str, directory: Path, scenes_dicts: dict, bbox: tuple, epsg: int, resources, performance
):
    """A function that runs the pipeline of git `ref` into a directory (one per process).

    The original pipeline takes no settings. A later one (such as the first adaptive pipeline,
    PR #90) runs with its default auto settings.
    """
    catalog, mosaic = load_baseline_modules(ref, directory)
    scenes = {
        month: [scene_from_dict(catalog.SceneRef, s) for s in month_scenes]
        for month, month_scenes in scenes_dicts.items()
    }
    takes_settings = "settings" in inspect.signature(mosaic.build_monthly_mosaics).parameters

    def run(out_dir: Path) -> Callable[[float], dict | None]:
        if not takes_settings:
            mosaic.build_monthly_mosaics(scenes, bbox, epsg, out_dir, carry_forward=True)
            return lambda cpu_seconds: None
        settings = performance.plan_settings(resources, adaptive=True)
        report = mosaic.RunReport()
        limiter = performance.AdaptiveLimiter(
            settings.requests, settings.max_requests, adaptive=settings.adaptive
        )
        mosaic.build_monthly_mosaics(
            scenes,
            bbox,
            epsg,
            out_dir,
            settings=settings,
            sign=catalog.sign_href,
            report=report,
            limiter=limiter,
        )
        return lambda cpu_seconds: mosaic.run_summary(
            report, limiter, settings, resources.cpus, cpu_seconds
        )

    return run


def split_config(config: str) -> tuple[str, int | None]:
    """'auto-mem2600-rows512' -> ('auto-mem2600', 512): a -rowsN suffix sets the strip rows."""
    base, sep, rows = config.rpartition("-rows")
    if sep and rows.isdigit():
        return base, int(rows)
    return config, None


def new_runner(
    settings, resources, scenes_dicts: dict, bbox: tuple, epsg: int, strip_rows: int | None
):
    """A function that runs the new pipeline into a directory, with a fresh report per call."""
    import catalog
    import mosaic
    import performance

    scenes = {
        month: [scene_from_dict(catalog.SceneRef, s) for s in month_scenes]
        for month, month_scenes in scenes_dicts.items()
    }

    def run(out_dir: Path) -> Callable[[float], dict | None]:
        report = mosaic.RunReport()
        limiter = performance.AdaptiveLimiter(
            settings.requests, settings.max_requests, adaptive=settings.adaptive
        )
        mosaic.build_monthly_mosaics(
            scenes,
            bbox,
            epsg,
            out_dir,
            settings=settings,
            sign=catalog.sign_href,
            carry_forward=True,
            report=report,
            limiter=limiter,
            strip_rows=strip_rows,
        )
        return lambda cpu_seconds: mosaic.run_summary(
            report, limiter, settings, resources.cpus, cpu_seconds
        )

    return run


def run_once(args: argparse.Namespace) -> dict:
    # GDAL reads this when its network handlers start, so it must be set before rasterio loads.
    os.environ["CPL_VSIL_NETWORK_STATS_ENABLED"] = "YES"
    workload = load_workload(args.workload)
    shaping = parse_lab(args.lab)
    is_lab = bool(workload.get("lab"))
    if shaping is not None and not is_lab:
        raise SystemExit("--lab only applies to workloads made with `lab-workload`")

    lab = LabProcess(shaping, args.lab_seed) if is_lab else None
    baseline_dir = Path(tempfile.mkdtemp(prefix="bench-baseline-"))
    try:
        scenes_by_month_dicts = (
            resolve_lab_hrefs(workload, lab.base_url) if lab else workload["scenes_by_month"]
        )
        bbox = as_bbox(workload["bbox"])
        epsg = workload["epsg"]
        sys.path.insert(0, str(HERE))
        import performance

        resources = performance.detect_resources()
        if args.config == "baseline":
            settings = None
            runner = baseline_runner(
                args.baseline_ref,
                baseline_dir,
                scenes_by_month_dicts,
                bbox,
                epsg,
                resources,
                performance,
            )
            sha, dirty = git_info(args.baseline_ref)[0], False
        else:
            base, strip_rows = split_config(args.config)
            settings = settings_for(base, resources, performance)
            runner = new_runner(settings, resources, scenes_by_month_dicts, bbox, epsg, strip_rows)
            sha, dirty = git_info()

        def execute(out_dir: Path) -> dict:
            gdal_network_reset()
            if lab is not None:
                lab.reset()
            nic0 = nic_rx_bytes()
            cpu0 = time.process_time()
            wall0 = time.time()
            t0 = time.perf_counter()
            finish = runner(out_dir)
            wall = time.perf_counter() - t0
            cpu = time.process_time() - cpu0
            nic1 = nic_rx_bytes()
            summary = finish(cpu)
            if summary is not None:
                first_month = summary["first_month_seconds"]
            else:
                mtimes = [
                    p.stat().st_mtime for p in [*out_dir.glob("*.npz"), *out_dir.glob("*.tif")]
                ]
                first_month = round(min(mtimes) - wall0, 3) if mtimes else None
            return {
                "wall": wall,
                "cpu": cpu,
                "first_month": first_month,
                "summary": summary,
                "nic": None if nic0 is None or nic1 is None else nic1 - nic0,
                "gdal": gdal_network_stats(),
                "lab_stats": lab.stats() if lab else None,
            }

        measured_dir = Path(tempfile.mkdtemp(prefix="bench-out-"))
        spare_dir = Path(tempfile.mkdtemp(prefix="bench-warmup-")) if args.warm else None
        try:
            if spare_dir is not None:
                execute(spare_dir)
            result = execute(measured_dir)
            shas, pixels, band_bytes = hash_outputs(measured_dir)
        finally:
            shutil.rmtree(measured_dir, ignore_errors=True)
            if spare_dir is not None:
                shutil.rmtree(spare_dir, ignore_errors=True)

        wall = result["wall"]
        record = {
            "ok": True,
            "workload": args.workload,
            "config": args.config,
            "rep": args.rep,
            "git_sha": sha,
            "git_dirty": dirty,
            "baseline_ref": args.baseline_ref if args.config == "baseline" else None,
            "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
            "machine": {
                **performance.describe_machine(),
                "resources": resources.__dict__,
            },
            "lab": shaping if is_lab else None,
            "warm": args.warm,
            "wall_seconds": round(wall, 3),
            "first_month_seconds": result["first_month"],
            "cpu_seconds": round(result["cpu"], 2),
            "peak_rss_bytes": peak_rss_bytes(),
            "gdal_network": result["gdal"],
            "lab_server": result["lab_stats"],
            "nic_rx_bytes": result["nic"],
            "month_sha256": shas,
            "output_pixels": pixels,
            "output_bytes": band_bytes,
            "useful_MBps": round(band_bytes / wall / 1e6, 3),
            "Mpx_per_s": round(pixels / wall / 1e6, 3),
            "settings": None
            if settings is None
            else {**settings.__dict__, "reasons": list(settings.reasons)},
            "run_summary": result["summary"],
        }
        return record
    finally:
        if lab is not None:
            lab.stop()
        shutil.rmtree(baseline_dir, ignore_errors=True)


def append_result(workload: str, record: dict) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    with (RESULTS_DIR / f"{workload}.jsonl").open("a") as f:
        f.write(json.dumps(record) + "\n")


def cmd_run(args: argparse.Namespace) -> None:
    record = run_once(args)
    append_result(args.workload, record)
    print(json.dumps(record), flush=True)


# --- compare -----------------------------------------------------------------------------------


def cmd_compare(args: argparse.Namespace) -> None:
    workloads = args.workloads.split(",")
    configs = args.configs.split(",")
    for name in workloads:
        load_workload(name)  # fail before the first run, not in the middle
    rng = random.Random(args.seed)
    pairs = [(w, c) for w in workloads for c in configs]
    total = len(pairs) * args.reps
    index = 0
    for rep in range(args.reps):
        rng.shuffle(pairs)
        for workload, config in pairs:
            index += 1
            command = [
                "uv",
                "run",
                "python",
                str(HERE / "bench.py"),
                "run",
                "--workload",
                workload,
                "--config",
                config,
                "--rep",
                str(rep),
                "--baseline-ref",
                args.baseline_ref,
            ]
            if args.lab:
                command += ["--lab", args.lab]
            if args.warm:
                command.append("--warm")
            print(f"[{index}/{total}] rep {rep} {workload} {config} ...", flush=True)
            started = time.perf_counter()
            error = None
            try:
                done = subprocess.run(
                    command, cwd=REPO, capture_output=True, text=True, timeout=args.timeout
                )
                if done.returncode != 0:
                    tail = "\n".join(done.stderr.strip().splitlines()[-15:])
                    error = f"exit code {done.returncode}: {tail}"
            except subprocess.TimeoutExpired:
                error = f"timed out after {args.timeout} s"
            elapsed = time.perf_counter() - started
            if error is None:
                print(f"    ok in {elapsed:.0f} s", flush=True)
            else:
                print(f"    FAILED after {elapsed:.0f} s: {error.splitlines()[0]}", flush=True)
                append_result(
                    workload,
                    {
                        "ok": False,
                        "workload": workload,
                        "config": config,
                        "rep": rep,
                        "lab": parse_lab(args.lab),
                        "warm": args.warm,
                        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
                        "error": error,
                    },
                )


# --- summarize ---------------------------------------------------------------------------------


def median(values: list) -> float | None:
    values = [v for v in values if v is not None]
    return float(np.median(values)) if values else None


def quartiles(values: list) -> tuple[float, float] | None:
    values = [v for v in values if v is not None]
    if not values:
        return None
    return float(np.percentile(values, 25)), float(np.percentile(values, 75))


def request_count(record: dict) -> int | None:
    gdal = record.get("gdal_network")
    return None if gdal is None else gdal["get_requests"] + gdal["head_requests"]


def downloaded_mb(record: dict) -> float | None:
    gdal = record.get("gdal_network")
    return None if gdal is None else gdal["downloaded_bytes"] / 1e6


def config_sort_key(config: str) -> tuple:
    if config == "baseline":
        return (0, 0, "")
    if config.startswith("fixed-"):
        return (1, int(config[6:]), "")
    return (2, 0, config)


def condition_label(record: dict) -> str:
    parts = []
    lab = record.get("lab")
    if lab is not None:
        parts.append("lab " + ",".join(f"{k}={v:g}" for k, v in lab.items()))
    if record.get("warm"):
        parts.append("warm")
    return " ".join(parts)


def build_tables(workloads: list[str] | None) -> list[dict]:
    records: list[dict] = []
    for path in sorted(RESULTS_DIR.glob("*.jsonl")):
        if workloads and path.stem not in workloads:
            continue
        for line in path.read_text().splitlines():
            if line.strip():
                records.append(json.loads(line))

    baseline_sha: dict[str, dict[str, str]] = {}  # workload -> month -> sha
    for r in records:
        if r["ok"] and r["config"] == "baseline":
            baseline_sha.setdefault(r["workload"], {}).update(r["month_sha256"])

    groups: dict[tuple[str, str], dict[str, list[dict]]] = {}
    for r in records:
        key = (r["workload"], condition_label(r))
        groups.setdefault(key, {}).setdefault(r["config"], []).append(r)

    tables = []
    for (workload, condition), by_config in sorted(groups.items(), key=lambda g: str(g[0])):
        base_wall = median([r["wall_seconds"] for r in by_config.get("baseline", []) if r["ok"]])
        rows = []
        for config in sorted(by_config, key=config_sort_key):
            runs = by_config[config]
            good = [r for r in runs if r["ok"]]
            walls = [r["wall_seconds"] for r in good]
            wall_median = median(walls)
            reference = baseline_sha.get(workload)
            if not good:
                correctness = "no successful run"
            elif config == "baseline":
                correctness = "reference"
            elif reference is None:
                correctness = "no baseline"
            else:
                bad = sorted(
                    {
                        month
                        for r in good
                        for month in sorted(set(r["month_sha256"]) | set(reference))
                        if r["month_sha256"].get(month) != reference.get(month)
                    }
                )
                correctness = "bit-exact" if not bad else "MISMATCH: " + ", ".join(bad)
            rows.append(
                {
                    "config": config,
                    "n": len(good),
                    "failed": len(runs) - len(good),
                    "wall_median": wall_median,
                    "wall_iqr": quartiles(walls),
                    "speedup_vs_baseline": None
                    if base_wall is None or wall_median is None
                    else base_wall / wall_median,
                    "useful_MBps_median": median([r["useful_MBps"] for r in good]),
                    "requests_median": median([request_count(r) for r in good]),
                    "downloaded_MB_median": median([downloaded_mb(r) for r in good]),
                    "peak_rss_MB_median": median([r["peak_rss_bytes"] / 1e6 for r in good]),
                    "cpu_seconds_median": median([r["cpu_seconds"] for r in good]),
                    "first_month_seconds_median": median([r["first_month_seconds"] for r in good]),
                    "correctness": correctness,
                }
            )
        tables.append({"workload": workload, "condition": condition, "rows": rows})
    return tables


def fmt(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def render_markdown(tables: list[dict]) -> str:
    lines: list[str] = []
    header = (
        "| config | n | failed | wall s median [IQR] | speed-up vs baseline | useful MB/s | "
        "GET+HEAD | downloaded MB | peak RSS MB | CPU s | first month s | correctness |"
    )
    divider = "|" + "---|" * 12
    for table in tables:
        title = table["workload"] + (f" ({table['condition']})" if table["condition"] else "")
        lines += [f"### {title}", "", header, divider]
        for row in table["rows"]:
            iqr = row["wall_iqr"]
            wall = (
                "n/a"
                if row["wall_median"] is None or iqr is None
                else f"{row['wall_median']:.1f} [{iqr[0]:.1f}-{iqr[1]:.1f}]"
            )
            speedup = row["speedup_vs_baseline"]
            lines.append(
                f"| {row['config']} | {row['n']} | {row['failed']} | {wall} | "
                f"{'n/a' if speedup is None else f'{speedup:.2f}x'} | "
                f"{fmt(row['useful_MBps_median'], 2)} | {fmt(row['requests_median'], 0)} | "
                f"{fmt(row['downloaded_MB_median'])} | {fmt(row['peak_rss_MB_median'], 0)} | "
                f"{fmt(row['cpu_seconds_median'])} | {fmt(row['first_month_seconds_median'])} | "
                f"{row['correctness']} |"
            )
        lines.append("")
    return "\n".join(lines)


def cmd_summarize(args: argparse.Namespace) -> None:
    workloads = args.workloads.split(",") if args.workloads else None
    tables = build_tables(workloads)
    if not tables:
        raise SystemExit(f"no results under {RESULTS_DIR}")
    print(render_markdown(tables))
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "summary.json").write_text(json.dumps(tables, indent=1) + "\n")


# --- command line ------------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    freeze = sub.add_parser("freeze", help="search STAC once and store the scene list")
    freeze.add_argument("--workload", required=True)
    freeze.set_defaults(func=cmd_freeze)

    mirror = sub.add_parser("mirror", help="download a workload's assets to data/bench/mirror")
    mirror.add_argument("--workload", required=True)
    mirror.add_argument("--max-gb", type=float, default=10.0)
    mirror.set_defaults(func=cmd_mirror)

    lab = sub.add_parser("lab-workload", help="derive a workload that reads from the lab server")
    lab.add_argument("--workload", required=True)
    lab.add_argument("--as", dest="new_name", required=True)
    lab.add_argument(
        "--alias-copies",
        type=int,
        help="replicate each month's scenes this many times under distinct alias URLs",
    )
    lab.set_defaults(func=cmd_lab_workload)

    run = sub.add_parser("run", help="one measurement in this process; prints one JSON line")
    run.add_argument("--workload", required=True)
    run.add_argument("--config", required=True)
    run.add_argument("--lab", help="latency_ms,bandwidth_mbps,fail_rate (lab workloads only)")
    run.add_argument("--lab-seed", type=int, default=0)
    run.add_argument(
        "--warm", action="store_true", help="run twice in this process, record the 2nd"
    )
    run.add_argument("--rep", type=int, default=0)
    run.add_argument("--baseline-ref", default=DEFAULT_BASELINE_REF)
    run.set_defaults(func=cmd_run)

    compare = sub.add_parser("compare", help="run workloads x configs x reps in fresh processes")
    compare.add_argument("--workloads", required=True)
    compare.add_argument("--configs", required=True)
    compare.add_argument("--reps", type=int, default=3)
    compare.add_argument("--lab")
    compare.add_argument("--warm", action="store_true")
    compare.add_argument("--seed", type=int, default=0)
    compare.add_argument("--timeout", type=float, default=1800.0)
    compare.add_argument("--baseline-ref", default=DEFAULT_BASELINE_REF)
    compare.set_defaults(func=cmd_compare)

    summarize = sub.add_parser("summarize", help="markdown tables from data/bench/results")
    summarize.add_argument("--workloads")
    summarize.set_defaults(func=cmd_summarize)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
