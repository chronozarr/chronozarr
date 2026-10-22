"""Run `chronozarr convert` on the Phase 0 corpus with two versions; record what a newcomer sees.

Versions:
  A   chronozarr 0.4.1 from the v0.4.1 tag wheel, venv data/corpus/.venv-041, extra [geo]
  B   current main through `uv run` in this worktree, env data/corpus/.venv-main, extra [geo]
  Ax, Bx   the same with the extras [geo,netcdf,s3] (only used for follow-up runs after a refusal
           that names a missing extra)

For each dataset and version it runs exactly what the docs tell a newcomer: `chronozarr convert
INPUT STORE --dry-run`, then, when the dry run exits 0, `chronozarr convert INPUT STORE` (10 minute
timeout each). A converted store is checked with `chronozarr validate` and with the independent
source comparison in _check_truth.py. The automatic class is a draft; final classes come from
`classes.json` (written after reading every message) and are merged into the results file.

    uv run --no-project python scripts/corpus/run_corpus.py [--only ID ...] [--versions A B]
        [--results PATH]
    uv run --no-project python scripts/corpus/run_corpus.py --fix [--only ID ...] --versions A B

`--fix` runs the verified fix chain of each dataset in fixes.json instead of the newcomer path,
and records it under the stage named there (`ID|VERSION|STAGE`).

`--results PATH` writes the records to PATH instead of results-2026-10-16.json (the Phase 0
baseline). Stores, logs and the venvs live under CHRONOZARR_CORPUS (default: data/corpus) and are
named by version, so a re-run in the same directory replaces them: point CHRONOZARR_CORPUS at a
directory of its own (symlink each dataset's raw/ into it) to keep the stores and logs of an
earlier run.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

HERE = Path(__file__).resolve().parent
WORKTREE = HERE.parents[1]
CORPUS = Path(
    os.environ.get("CHRONOZARR_CORPUS", Path(__file__).resolve().parents[2] / "data" / "corpus")
)
RESULTS = CORPUS / "results-2026-10-16.json"
FETCH_PY = CORPUS / ".venv-fetch" / "bin" / "python"
TIMEOUT = 600
WORKTREE_SHA = subprocess.run(
    ["git", "-C", str(WORKTREE), "rev-parse", "--short", "HEAD"],
    capture_output=True,
    text=True,
    check=True,
).stdout.strip()
# The released chronozarr wheel built from its git tag; needed only to create the Ax venv.
TAG_WHEEL_ENV = "CHRONOZARR_TAG_WHEEL"
TAIL_LINES = 60
HEAD_LINES = 15


class VersionSpec(TypedDict):
    venv: Path
    label: str


VERSIONS: dict[str, VersionSpec] = {
    "A": {"venv": CORPUS / ".venv-041", "label": "0.4.1 tag wheel [geo]"},
    "B": {"venv": CORPUS / ".venv-main", "label": f"main {WORKTREE_SHA} via uv run [geo]"},
    "Ax": {"venv": CORPUS / ".venv-041x", "label": "0.4.1 tag wheel [geo,netcdf,s3]"},
    "Bx": {
        "venv": CORPUS / ".venv-mainx",
        "label": f"main {WORKTREE_SHA} via uv run [geo,netcdf,s3]",
    },
}


def command_prefix(version: str) -> list[str]:
    venv = VERSIONS[version]["venv"]
    if version.startswith("A"):
        return [str(venv / "bin" / "chronozarr")]
    extras = ["--extra", "geo"] + (
        ["--extra", "netcdf", "--extra", "s3"] if version == "Bx" else []
    )
    return ["uv", "run", "--project", str(WORKTREE), "--locked", "--no-dev", *extras, "chronozarr"]


def environment(version: str) -> dict[str, str]:
    env = dict(os.environ)
    if version.startswith("B"):
        env["UV_PROJECT_ENVIRONMENT"] = str(VERSIONS[version]["venv"])
    env.pop("VIRTUAL_ENV", None)
    env["NO_COLOR"] = "1"
    return env


def run(cmd: list[str], env: dict[str, str], cwd: Path, log: Path) -> dict:
    """Run `cmd` with the timeout; save stdout and stderr next to `log`; return the record."""
    started = time.time()
    timed_out = False
    try:
        proc = subprocess.run(
            cmd, env=env, cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT
        )
        code, out, err = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        code = -9
        out = (
            exc.stdout.decode(errors="replace")
            if isinstance(exc.stdout, bytes)
            else (exc.stdout or "")
        )
        err = (
            exc.stderr.decode(errors="replace")
            if isinstance(exc.stderr, bytes)
            else (exc.stderr or "")
        )
    log.parent.mkdir(parents=True, exist_ok=True)
    log.with_suffix(".out").write_text(out)
    log.with_suffix(".err").write_text(err)
    return {
        "cmd": " ".join(cmd),
        "exit": code,
        "timed_out": timed_out,
        "seconds": round(time.time() - started, 1),
        "stdout_head": "\n".join(out.splitlines()[:HEAD_LINES]),
        "stdout_tail": "\n".join(out.splitlines()[-TAIL_LINES:]),
        "stderr_head": "\n".join(err.splitlines()[:HEAD_LINES]),
        "stderr_tail": "\n".join(err.splitlines()[-TAIL_LINES:]),
    }


def trash(path: Path) -> None:
    if path.exists():
        subprocess.run(["trash", str(path)], check=True)


def ensure_env(version: str) -> None:
    """Create the extras venvs on first use (never loosens the 7 day quarantine)."""
    venv = VERSIONS[version]["venv"]
    if venv.exists():
        return
    if version == "Ax":
        tag_wheel = os.environ.get(TAG_WHEEL_ENV)
        if not tag_wheel:
            raise SystemExit(
                f"set {TAG_WHEEL_ENV} to the chronozarr wheel built from the release tag "
                "(git archive vX.Y.Z, then uv build --wheel), to create the Ax venv"
            )
        subprocess.run(["uv", "venv", "--python", "3.13", str(venv)], check=True)
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(venv / "bin" / "python"),
                f"chronozarr[geo,netcdf,s3] @ file://{tag_wheel}",
            ],
            check=True,
        )
        return
    subprocess.run(
        [
            "uv",
            "sync",
            "--project",
            str(WORKTREE),
            "--locked",
            "--no-dev",
            "--extra",
            "geo",
            "--extra",
            "netcdf",
            "--extra",
            "s3",
        ],
        env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(venv)},
        check=True,
    )


def run_one(
    entry: dict,
    version: str,
    input_rel: str | None = None,
    flags: list[str] | None = None,
    suffix: str = "",
    truth_rel: str = "raw",
    truth_nodata: float | None = None,
) -> dict:
    """The newcomer path for one dataset and version; a fix run when input_rel/flags are given."""
    id = entry["id"]
    run_dir = CORPUS / id
    base = run_dir / f"store-{version}{suffix}"
    logs = run_dir / "logs"
    trash(base)
    trash(base.with_name(base.name + ".convert-work"))
    newcomer = entry["newcomer"]
    rel = input_rel if input_rel is not None else newcomer["input"]
    flags = list(flags if flags is not None else newcomer["flags"])
    source = str(run_dir / rel)
    env = environment(version)
    prefix = command_prefix(version)
    rec: dict = {
        "id": id,
        "version": version,
        "suffix": suffix,
        "input": source,
        "flags": flags,
        "truth_rel": truth_rel,
        "truth_nodata": truth_nodata,
        "ran_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    dry = run(
        [*prefix, "convert", source, str(base), "--dry-run", *flags],
        env,
        run_dir,
        logs / f"{version}{suffix}-dry",
    )
    rec["dry_run"] = dry
    rec["convert"] = rec["validate"] = rec["checks"] = None
    if dry["exit"] == 0:
        conv = run(
            [*prefix, "convert", source, str(base), *flags],
            env,
            run_dir,
            logs / f"{version}{suffix}-convert",
        )
        rec["convert"] = conv
        if conv["exit"] == 0 and base.exists():
            rec["validate"] = run(
                [*prefix, "validate", str(base)],
                env,
                run_dir,
                logs / f"{version}{suffix}-validate",
            )
            rec["checks"] = check_store(entry, version, base, suffix, truth_rel, truth_nodata)
    rec["auto_class"] = auto_class(rec)
    return rec


def check_store(
    entry: dict,
    version: str,
    store: Path,
    suffix: str,
    truth_rel: str = "raw",
    truth_nodata: float | None = None,
) -> dict:
    id = entry["id"]
    run_dir = CORPUS / id
    venv_python = VERSIONS[version]["venv"] / "bin" / "python"
    dump = run_dir / f"dump-{version}{suffix}.json"
    d = run(
        [str(venv_python), "-I", str(HERE / "_dump_store.py"), str(store), str(dump)],
        environment(version),
        run_dir,
        run_dir / "logs" / f"{version}{suffix}-dump",
    )
    if d["exit"] != 0:
        return {"checked": False, "reason": "dump failed", "dump": d}
    return run_truth(id, version, suffix, truth_rel, truth_nodata)


def run_truth(
    id: str, version: str, suffix: str, truth_rel: str, truth_nodata: float | None = None
) -> dict:
    """Compare the dump of a store with the source files (independent readers)."""
    run_dir = CORPUS / id
    dump = run_dir / f"dump-{version}{suffix}.json"
    result = run_dir / f"check-{version}{suffix}.json"
    c = run(
        [
            str(FETCH_PY),
            "-I",
            str(HERE / "_check_truth.py"),
            id,
            str(run_dir / truth_rel),
            str(dump),
            str(result),
            *([] if truth_nodata is None else [str(truth_nodata)]),
        ],
        os.environ.copy(),
        run_dir,
        run_dir / "logs" / f"{version}{suffix}-truth",
    )
    if c["exit"] != 0:
        return {"checked": False, "reason": "truth comparison failed", "truth": c}
    return json.loads(result.read_text())


def recheck() -> None:
    """Re-run only the source comparison on the stored dumps (after a checker change)."""
    for key, rec in load_results()["records"].items():
        if not rec.get("checks") or "values" not in rec["checks"]:
            continue
        truth_rel = rec.get("truth_rel", "raw")
        rec["truth_rel"] = truth_rel
        rec["checks"] = run_truth(
            rec["id"], rec["version"], rec["suffix"], truth_rel, rec.get("truth_nodata")
        )
        rec["auto_class"] = auto_class(rec)
        store_record(key, rec)
        print(f"{key}: {rec['auto_class']}", flush=True)


FIX_WORDS = re.compile(
    r"\bfix:|pass --|use --|install|chronozarr\[|--date-pattern|--resampling|--variable|--crs"
    r"|--nodata|manifest|gdal_",
    re.I,
)


def auto_class(rec: dict) -> str:
    """Draft class; the final one is set by hand in classes.json after reading each message."""
    steps = [s for s in (rec["dry_run"], rec["convert"], rec["validate"]) if s]
    if any(s["timed_out"] for s in steps):
        return "CRASH"
    if any(
        "Traceback (most recent call last)" in (s["stderr_tail"] + s["stdout_tail"]) for s in steps
    ):
        return "CRASH"
    failed = next((s for s in steps if s["exit"] != 0), None)
    if failed:
        text = failed["stderr_tail"] + failed["stdout_tail"]
        return "ACTIONABLE?" if FIX_WORDS.search(text) else "UNACTIONABLE?"
    checks = rec["checks"]
    if not checks or (checks.get("checked", True) is not True and "values" not in checks):
        return "UNCHECKED"
    values = checks.get("values", {})
    ok = (
        checks.get("dates_equal")
        and checks.get("crs_equal")
        and values.get("checked")
        and values.get("compared", 0) > 0
        and values.get("n_mismatch", 1) == 0
        and values.get("invalid_shown_as_valid", 1) == 0
        and not checks.get("x_beyond_180")
        and checks.get("range", {}).get("store_outside", 0) == 0
        and checks.get("range", {}).get("source_outside", 0) == 0
    )
    return "OK" if ok else "WRONG"


def load_results() -> dict:
    return json.loads(RESULTS.read_text()) if RESULTS.exists() else {"records": {}}


def store_record(key: str, rec: dict) -> None:
    """Merge one record into the results file under an exclusive lock (runners may share it)."""
    with open(RESULTS.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        doc = load_results()
        doc.setdefault("records", {})[key] = rec
        doc["versions"] = {v: VERSIONS[v]["label"] for v in VERSIONS}
        tmp = RESULTS.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=1) + "\n")
        tmp.replace(RESULTS)


def main(argv: list[str]) -> int:
    global RESULTS
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--only", action="append", help="dataset id (repeatable)")
    parser.add_argument("--versions", nargs="+", default=["A", "B"], choices=list(VERSIONS))
    parser.add_argument(
        "--fix",
        action="store_true",
        help="run the verified fix chain in fixes.json instead of the newcomer path",
    )
    parser.add_argument(
        "--recheck", action="store_true", help="re-run the source comparison on stored dumps"
    )
    parser.add_argument(
        "--results",
        type=Path,
        default=RESULTS,
        help="results file to write (default: the Phase 0 baseline, results-2026-10-16.json)",
    )
    args = parser.parse_args(argv)
    RESULTS = args.results.resolve()
    if args.recheck:
        recheck()
        return 0
    manifest = json.loads((HERE / "manifest.json").read_text())
    entries = {e["id"]: e for e in manifest}
    ids = args.only or list(entries)
    fixes = json.loads((HERE / "fixes.json").read_text()) if args.fix else {}
    for id in ids:
        entry = entries[id]
        for version in args.versions:
            if version in ("Ax", "Bx"):
                ensure_env(version)
            if args.fix:
                fix = fixes.get(id)
                if not fix:
                    continue
                key = f"{id}|{version}|{fix['stage']}"
                rec = run_one(
                    entry,
                    version,
                    input_rel=fix.get("input"),
                    flags=fix["flags"],
                    suffix=f"-{fix['stage']}",
                    truth_rel=fix.get("truth", "raw"),
                    truth_nodata=fix.get("truth_nodata"),
                )
                rec["fix_note"] = fix.get("note", "")
            else:
                key = f"{id}|{version}"
                rec = run_one(entry, version)
            store_record(key, rec)
            convert_exit = rec["convert"]["exit"] if rec["convert"] else "-"
            print(
                f"{key}: {rec['auto_class']}  "
                f"(dry {rec['dry_run']['exit']}, convert {convert_exit})",
                flush=True,
            )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
