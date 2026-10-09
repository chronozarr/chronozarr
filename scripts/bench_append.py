"""Measure what appending one date to a chronozarr store costs, on real monthly mosaics.

Reads data/mosaics/<aoi>/YYYY-MM.npz (the files `reencode_aoi.py` reads). For each layout
(whole-axis shard, shard_time=12, unsharded) with true stored values it builds a store
from the first months, appends the next months one at a time, and records per operation: wall
seconds and peak RSS (each operation runs in its own process), the objects and bytes written
(file sizes, mtimes and sha256 before and after), what `aws s3 sync` and `aws s3 sync
--size-only` would upload, and the requests a cold 3 x 3 cell view of a timestep needs. After
every step the store must validate and every timestep must equal its source bit for bit.

Usage:
    uv run python scripts/bench_append.py run --work-dir /tmp/append-bench
    uv run python scripts/bench_append.py run --work-dir /tmp/append-bench --base-months 115 \
        --layouts whole-axis --encodings none          # the production-sized store
"""

from __future__ import annotations

import argparse
import hashlib
import json
import resource
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TypedDict

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import reencode_aoi as live

import chronozarr
from chronozarr.append import append

MOSAICS = live.DATA / "mosaics"


class Layout(TypedDict, total=False):
    """The `chronozarr.encode` keywords that set a store's shard layout."""

    shard: bool
    shard_time: int


LAYOUTS: dict[str, Layout] = {
    "whole-axis": {"shard": True},  # shard_time = months at creation
    "shard-time-12": {"shard": True, "shard_time": 12},
    "unsharded": {"shard": False},  # the encoder default
}
VIEW_ROWS, VIEW_COLS = (1, 2, 3), (1, 2, 3)  # a 3 x 3 block of level-0 cells
MB = 1e6


# --- One operation in its own process -----------------------------------------------------------


def peak_rss_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return (
        peak / MB if sys.platform == "darwin" else peak * 1024 / MB
    )  # bytes on macOS, KiB on Linux


def child_build(args: argparse.Namespace) -> dict:
    paths = live.mosaic_paths(MOSAICS / args.aoi, args.base_months)
    grid = live.read_grid(paths[0])
    started = time.perf_counter()
    chronozarr.encode(
        live.iter_mosaics(paths, grid),
        args.store,
        times=live.mosaic_times(paths),
        bands=live.s2_bands(grid.band_names),
        crs=f"EPSG:{grid.epsg}",
        transform=grid.transform,
        provenance=live.PROVENANCE,
        **LAYOUTS[args.layout],
    )
    return {
        "seconds": time.perf_counter() - started,
        "encoding": "none",
        "peak_rss_mb": peak_rss_mb(),
    }


def child_append(args: argparse.Namespace) -> dict:
    paths = live.mosaic_paths(MOSAICS / args.aoi)
    chosen = paths[args.first_month : args.first_month + args.count]
    grid = live.read_grid(paths[0])
    started = time.perf_counter()
    report = append(
        args.store,
        live.iter_mosaics(chosen, grid),
        times=live.mosaic_times(chosen),
    )
    return {
        "seconds": time.perf_counter() - started,
        "report_objects": report.objects_written,
        "report_bytes": report.bytes_written,
        "peak_rss_mb": peak_rss_mb(),
    }


def run_child(op: str, **options: object) -> dict:
    command = [sys.executable, __file__, "child", op, json.dumps(options, default=str)]
    done = subprocess.run(command, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise SystemExit(f"{op} failed:\n{done.stdout}\n{done.stderr}")
    line = next(line for line in reversed(done.stdout.splitlines()) if line.startswith("RESULT "))
    return json.loads(line.removeprefix("RESULT "))


# --- Store state --------------------------------------------------------------------------------


def snapshot(store: Path) -> dict[str, tuple[int, int, str]]:
    """(size, mtime_ns, sha256) of every object under the store, by key."""
    entries = {}
    for path in sorted(p for p in store.rglob("*") if p.is_file()):
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            for block in iter(lambda f=f: f.read(1 << 22), b""):
                digest.update(block)
        stat = path.stat()
        entries[str(path.relative_to(store))] = (
            stat.st_size,
            stat.st_mtime_ns,
            digest.hexdigest(),
        )
    return entries


def diff(before: dict, after: dict) -> dict:
    new = [k for k in after if k not in before]
    content = [k for k in after if k in before and after[k][2] != before[k][2]]
    touched_only = [
        k
        for k in after
        if k in before and after[k][2] == before[k][2] and after[k][1] != before[k][1]
    ]
    size_changed = [k for k in after if k in before and after[k][0] != before[k][0]]
    written = new + content + touched_only
    return {
        "objects_new": len(new),
        "objects_rewritten": len(content),
        "objects_touched_identical": len(touched_only),
        "objects_written": len(written),
        "bytes_written": sum(after[k][0] for k in written),
        "mb_written": sum(after[k][0] for k in written) / MB,
        # default sync: size differs or the local file is newer than the remote copy
        "sync_objects": len(written),
        "sync_bytes": sum(after[k][0] for k in written),
        # --size-only: new objects and objects whose size changed
        "size_only_objects": len(new) + len(size_changed),
        "size_only_bytes": sum(after[k][0] for k in new + size_changed),
        "size_only_misses": len(content) - len([k for k in content if k in size_changed]),
        "store_mb": sum(v[0] for v in after.values()) / MB,
        "n_objects": len(after),
    }


def load_month(index: int, aoi: str) -> np.ndarray:
    paths = live.mosaic_paths(MOSAICS / aoi)
    with np.load(paths[index], allow_pickle=False) as npz:
        return npz["bands"]


def verify(store: Path, n_time: int, aoi: str) -> None:
    """The store validates and every timestep equals its source mosaic bit for bit."""
    problems = chronozarr.validate(store)
    if problems:
        raise SystemExit(f"{store} does not validate: {problems[:3]}")
    opened = chronozarr.open_store(store)
    if len(opened.times) != n_time:
        raise SystemExit(f"{store} has {len(opened.times)} timesteps, expected {n_time}")
    with ThreadPoolExecutor(max_workers=2) as pool:
        sources = pool.map(lambda i: load_month(i, aoi), range(n_time))
        for t, source in enumerate(sources):
            if not np.array_equal(opened.read(t), source):
                raise SystemExit(f"{store}: timestep {t} differs from its source")


class CountingStore:
    """Counts the reads of data objects: shard index reads (suffix ranges) and chunk reads."""

    def __init__(self, path: Path) -> None:
        from zarr.abc.store import ByteRequest
        from zarr.core.buffer import Buffer, BufferPrototype
        from zarr.storage import LocalStore

        outer = self

        class Counting(LocalStore):
            async def get(
                self,
                key: str,
                prototype: BufferPrototype | None = None,
                byte_range: ByteRequest | None = None,
            ) -> Buffer | None:
                outer.record(key, byte_range)
                return await super().get(key, prototype, byte_range)

        self.index_reads = self.chunk_reads = self.bytes = 0
        self.store = Counting(path, read_only=True)

    def record(self, key: str, byte_range: object) -> None:
        if "/data/c/" not in key:
            return
        kind = type(byte_range).__name__
        if kind == "SuffixByteRequest":
            self.index_reads += 1
        else:
            self.chunk_reads += 1


def view_requests(store: Path, t: int) -> dict:
    """Cold requests to read timestep t over a 3 x 3 block of level-0 cells (no index cache)."""
    counting = CountingStore(store)
    opened = chronozarr.open_store(counting.store)
    counting.index_reads = counting.chunk_reads = 0
    for row in VIEW_ROWS:
        for col in VIEW_COLS:
            opened.read_cell(t, row, col)
    return {
        "t": t,
        "index_reads": counting.index_reads,
        "chunk_reads": counting.chunk_reads,
        "total": counting.index_reads + counting.chunk_reads,
    }


# --- Driver -------------------------------------------------------------------------------------


def run(args: argparse.Namespace) -> None:
    work = args.work_dir
    work.mkdir(parents=True, exist_ok=True)
    rows = []
    for encoding in args.encodings:
        for layout in args.layouts:
            store = work / f"{layout}-{encoding}-{args.base_months}"
            if store.exists():
                raise SystemExit(f"{store} exists; choose a fresh --work-dir")
            print(f"== {layout} / {encoding}: build {args.base_months} months", flush=True)
            built = run_child(
                "build",
                aoi=args.aoi,
                store=store,
                base_months=args.base_months,
                layout=layout,
            )
            verify(store, args.base_months, args.aoi)
            state = snapshot(store)
            row = {
                "layout": layout,
                "encoding_requested": encoding,
                "encoding": built["encoding"],
                "build": {
                    **built,
                    "store_mb": sum(v[0] for v in state.values()) / MB,
                    "n_objects": len(state),
                },
                "views_before": [view_requests(store, t) for t in args.view_steps],
                "appends": [],
            }
            for step in range(args.appends):
                month = args.base_months + step
                print(f"   append month {month + 1}", flush=True)
                done = run_child("append", aoi=args.aoi, store=store, first_month=month, count=1)
                after = snapshot(store)
                verify(store, month + 1, args.aoi)
                row["appends"].append(
                    {
                        "month": month + 1,
                        **done,
                        **diff(state, after),
                        "views": [view_requests(store, t) for t in [*args.view_steps, month]],
                    }
                )
                state = after
            rows.append(row)
            print(json.dumps(row), flush=True)
    out = work / "results.json"
    out.write_text(json.dumps(rows, indent=1))
    print(f"wrote {out}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    run_cmd = sub.add_parser("run", help="build, append, verify and measure")
    run_cmd.add_argument("--aoi", default="ucayali_santa_maria")
    run_cmd.add_argument("--work-dir", type=Path, required=True)
    run_cmd.add_argument("--base-months", type=int, default=12)
    run_cmd.add_argument("--appends", type=int, default=2)
    run_cmd.add_argument("--layouts", nargs="+", choices=list(LAYOUTS), default=list(LAYOUTS))
    run_cmd.add_argument("--encodings", nargs="+", choices=["none"], default=["none"])
    run_cmd.add_argument("--view-steps", type=int, nargs="+", default=[5, 11])
    run_cmd.set_defaults(func=run)

    child = sub.add_parser("child")
    child.add_argument("op", choices=["build", "append"])
    child.add_argument("options")

    args = parser.parse_args()
    if args.command == "child":
        options = argparse.Namespace(**json.loads(args.options))
        for key in ("store",):
            if hasattr(options, key):
                setattr(options, key, Path(getattr(options, key)))
        result = child_build(options) if args.op == "build" else child_append(options)
        print("RESULT " + json.dumps(result))
        return
    args.func(args)


if __name__ == "__main__":
    main()
