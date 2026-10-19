"""Run the quickstart steps that quickstart_commands.py extracted, as a newcomer would.

    run_quickstart.py STEPS.json --workdir DIR --out DIR [--shell bash|pwsh]

Each step's text goes to the shell a scientist on that OS uses, unchanged: bash on Linux and
macOS, PowerShell (pwsh) on Windows. The only additions are what a terminal gives a person and a
file does not: `bash -e` and, for PowerShell, a two-line preamble make a failing line stop the
step, `PYTHONUNBUFFERED` gives the line buffering a terminal has, and `PYTHONUTF8` stands in for
the console's UTF-8 output. Steps run in order in an empty working directory and stop at the
first failure.

A long-running step (share, preview) is started, then polled until the link it prints answers:
the `open:` line names the store, and `<store>/zarr.json` must return 200 at the host the plan
expects. Then it is stopped with SIGINT, as the page says (Windows has no SIGINT to send to a
process group, so the process tree is terminated there).

Written to --out: steps/NN.sh|ps1 (what ran), steps/NN.log (its output, verbatim), results.json,
summary.md (also appended to $GITHUB_STEP_SUMMARY) and environment.txt.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

POWERSHELL_PREAMBLE = (
    "$ErrorActionPreference = 'Stop'\n$PSNativeCommandUseErrorActionPreference = $true\n"
)
OPEN_LINE = re.compile(r"^open: (\S+)\s*$")
INTERRUPT_GRACE_SECONDS = 30
SUMMARY_TAIL_LINES = 60


@dataclass
class Result:
    index: int
    label: str
    heading: str
    os: str
    shell: str
    long_running: bool
    status: str  # ok, failed, timeout or not run
    seconds: float  # to exit, or to a ready link for a long-running step
    exit_code: int | None
    note: str
    log: str


class Output:
    """A process's combined stdout and stderr, kept verbatim in a log file and as decoded lines."""

    def __init__(self, process: subprocess.Popen[bytes], log: Path, echo: bool) -> None:
        self.lines: list[str] = []
        self._process = process
        self._lock = threading.Lock()
        self._log = log
        self._echo = echo
        self._thread = threading.Thread(target=self._drain, args=(process,), daemon=True)
        self._thread.start()

    def _drain(self, process: subprocess.Popen[bytes]) -> None:
        assert process.stdout is not None
        with self._log.open("wb") as handle:
            for raw in iter(process.stdout.readline, b""):
                handle.write(raw)
                handle.flush()
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                with self._lock:
                    self.lines.append(line)
                if self._echo:
                    print(line, flush=True)

    def snapshot(self) -> list[str]:
        with self._lock:
            return list(self.lines)

    def finish(self) -> None:
        """Wait for the last output; close the pipe unless a grandchild still holds it open."""
        self._thread.join(timeout=10)
        if not self._thread.is_alive() and self._process.stdout is not None:
            self._process.stdout.close()


def default_shell() -> str:
    return "pwsh" if os.name == "nt" else "bash"


def step_script(shell: str, text: str) -> tuple[str, str]:
    """The file extension and content that run a step's text in ``shell``."""
    if shell == "pwsh":
        return ".ps1", POWERSHELL_PREAMBLE + text + "\n"
    return ".sh", text + "\n"


def shell_command(shell: str, script: Path) -> list[str]:
    executable = shutil.which(shell)
    if executable is None:
        raise FileNotFoundError(f"{shell} is not on PATH")
    if shell == "pwsh":
        return [
            executable,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
        ]
    return [executable, "--noprofile", "--norc", "-e", str(script)]


def step_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONUNBUFFERED"] = "1"
    environment["PYTHONUTF8"] = "1"
    return environment


def launch(
    command: list[str], cwd: Path, log: Path, echo: bool
) -> tuple[subprocess.Popen[bytes], Output]:
    # A process group of its own, so that kill_tree stops share's cloudflared child too.
    if sys.platform == "win32":
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=step_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    else:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=step_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return process, Output(process, log, echo)


def kill_tree(process: subprocess.Popen[bytes]) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False
        )
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    process.wait()


def interrupt(process: subprocess.Popen[bytes]) -> tuple[bool, str]:
    """Stop a long-running step the way the page says; return whether it ended cleanly."""
    if process.poll() is not None:
        return False, f"had already exited with {process.returncode} before it was stopped"
    if os.name == "nt":
        kill_tree(process)
        return True, "terminated with taskkill (Ctrl-C is not sent to a process tree on Windows)"
    os.killpg(process.pid, signal.SIGINT)
    try:
        process.wait(timeout=INTERRUPT_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        kill_tree(process)
        return False, f"still running {INTERRUPT_GRACE_SECONDS} s after SIGINT; killed"
    if process.returncode != 0:
        return False, f"SIGINT ended it with exit {process.returncode}, not 0"
    return True, "stopped by SIGINT, exit 0"


def http_status(url: str, timeout: float = 15.0) -> int | None:
    request = urllib.request.Request(url, headers={"User-Agent": "chronozarr-clean-room-ci"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, TimeoutError, OSError):
        return None


def store_from_output(lines: list[str]) -> str | None:
    """The store URL that the first `open: <viewer link>` line names, or None before it prints."""
    for line in lines:
        match = OPEN_LINE.match(line)
        if match:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(match.group(1)).query)
            stores = query.get("store")
            if not stores:
                raise ValueError(f"the printed link has no store parameter: {line!r}")
            return stores[0]
    return None


def check_store_host(store: str, ready: dict[str, Any]) -> None:
    parsed = urllib.parse.urlparse(store)
    host = parsed.hostname or ""
    if parsed.scheme != ready["scheme"]:
        raise ValueError(f"the link's store {store!r} is not {ready['scheme']}")
    if "host" in ready and host != ready["host"]:
        raise ValueError(f"the link's store {store!r} is not on {ready['host']}")
    if "host_suffix" in ready and not host.endswith(ready["host_suffix"]):
        raise ValueError(f"the link's store {store!r} is not on a {ready['host_suffix']} address")


def wait_until_ready(
    process: subprocess.Popen[bytes], output: Output, ready: dict[str, Any], started: float
) -> tuple[float, str]:
    """Poll until the printed link's store answers 200; return the seconds taken and a note."""
    deadline = started + ready["timeout_seconds"]
    last = "no `open:` line was printed"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"exited with {process.returncode} before its link answered: {last}"
            )
        store = store_from_output(output.snapshot())
        if store is not None:
            check_store_host(store, ready)
            url = store.rstrip("/") + "/zarr.json"
            status = http_status(url)
            if status == 200:
                return time.monotonic() - started, f"{url} returned 200"
            last = f"{url} returned {status}"
        time.sleep(1)
    raise TimeoutError(f"no ready link within {ready['timeout_seconds']} s: {last}")


def run_step(
    step: dict[str, Any],
    shell: str,
    workdir: Path,
    out: Path,
    os_name: str,
    step_timeout: float,
    echo: bool,
) -> Result:
    index = step["index"]
    extension, content = step_script(shell, step["text"])
    script = out / "steps" / f"{index:02d}{extension}"
    log = out / "steps" / f"{index:02d}.log"
    script.write_text(content, encoding="utf-8")
    result = Result(
        index=index,
        label=step["label"],
        heading=step["heading"],
        os=os_name,
        shell=shell,
        long_running=step["long_running"],
        status="failed",
        seconds=0.0,
        exit_code=None,
        note="",
        log=f"steps/{log.name}",
    )
    started = time.monotonic()
    process, output = launch(shell_command(shell, script), workdir, log, echo)
    try:
        if step["long_running"]:
            result.seconds, result.note = wait_until_ready(process, output, step["ready"], started)
            clean, stopped = interrupt(process)
            result.note += "; " + stopped
            result.status = "ok" if clean else "failed"
        else:
            try:
                process.wait(timeout=step_timeout)
            except subprocess.TimeoutExpired:
                kill_tree(process)
                result.status, result.note = "timeout", f"killed after {step_timeout:.0f} s"
            else:
                result.status = "ok" if process.returncode == 0 else "failed"
            result.seconds = time.monotonic() - started
    except (RuntimeError, TimeoutError, ValueError) as exc:
        result.note = str(exc)
        result.status = "timeout" if isinstance(exc, TimeoutError) else "failed"
        result.seconds = time.monotonic() - started
        if process.poll() is None:
            kill_tree(process)
    output.finish()
    result.exit_code = process.returncode
    return result


def render_summary(os_name: str, results: list[Result], plan: dict[str, Any], out: Path) -> str:
    def cell(text: str) -> str:
        return text.replace("|", "\\|")

    lines = [
        f"## Quickstart clean room: {os_name} {platform.machine()}",
        "",
        f"Page `{plan['source']}`, chronozarr {plan['chronozarr_version']}.",
        "",
        "| Step | OS | Seconds | Status |",
        "|---|---|---:|---|",
    ]
    for result in results:
        lines.append(
            f"| {cell(result.label)} | {result.os} | {result.seconds:.1f} | {result.status} |"
        )
    for item in plan["skipped"]:
        first = item["text"].splitlines()[0]
        lines += ["", f"Skipped (line {item['line']}): `{first}` -- {item['reason']}."]
    for item in plan["not_run"]:
        lines += ["", f"Not run (line {item['line']}, {item['heading']}): {item['reason']}."]
    notes = [r for r in results if r.note]
    if notes:
        lines += ["", "Notes:"] + [f"- {cell(r.label)}: {r.note}" for r in notes]
    failed = next((r for r in results if r.status not in ("ok", "not run")), None)
    if failed is not None:
        tail = (out / failed.log).read_text(encoding="utf-8", errors="replace").splitlines()
        lines += [
            "",
            f"### Failed: {failed.label} ({failed.status}, exit {failed.exit_code})",
            "",
            "```text",
            *tail[-SUMMARY_TAIL_LINES:],
            "```",
        ]
    return "\n".join(lines) + "\n"


def command_output(command: list[str]) -> str:
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"unavailable ({exc})"
    text = (done.stdout + done.stderr).strip()
    return text.splitlines()[0] if text else ""


def record_environment(path: Path, shell: str) -> None:
    uv_variables = {k: v for k, v in sorted(os.environ.items()) if k.startswith("UV_")}
    version_flag = ["--version"]
    lines = [
        f"platform: {platform.platform()} {platform.machine()}",
        f"harness python: {sys.version.split()[0]}",
        f"{shell}: {command_output([shell, *version_flag])}",
        f"uv: {command_output(['uv', *version_flag])}",
        f"cloudflared: {command_output(['cloudflared', *version_flag])}",
        *(f"{key}={value}" for key, value in uv_variables.items()),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("steps", type=Path, help="JSON from quickstart_commands.py")
    parser.add_argument("--workdir", type=Path, required=True, help="an empty folder to work in")
    parser.add_argument("--out", type=Path, required=True, help="where to write logs and results")
    parser.add_argument("--shell", choices=["bash", "pwsh"], default=default_shell())
    parser.add_argument(
        "--step-timeout", type=float, default=900, help="seconds per ordinary step"
    )
    args = parser.parse_args(argv)
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:  # a pipe on Windows defaults to the ANSI code page
        reconfigure(encoding="utf-8", errors="replace")

    if shutil.which(args.shell) is None:
        print(f"run_quickstart: {args.shell} is not on PATH", file=sys.stderr)
        return 2
    plan = json.loads(args.steps.read_text(encoding="utf-8"))
    if args.workdir.exists() and any(args.workdir.iterdir()):
        print(f"run_quickstart: {args.workdir} is not empty", file=sys.stderr)
        return 2
    args.workdir.mkdir(parents=True, exist_ok=True)
    (args.out / "steps").mkdir(parents=True, exist_ok=True)
    record_environment(args.out / "environment.txt", args.shell)
    os_name = os.environ.get("RUNNER_OS") or platform.system()
    in_actions = os.environ.get("GITHUB_ACTIONS") == "true"

    results: list[Result] = []
    failed = False
    for step in plan["steps"]:
        if failed:
            results.append(
                Result(
                    step["index"],
                    step["label"],
                    step["heading"],
                    os_name,
                    args.shell,
                    step["long_running"],
                    "not run",
                    0.0,
                    None,
                    "an earlier step failed",
                    "",
                )
            )
            continue
        if in_actions:
            print(f"::group::{step['label']}", flush=True)
        result = run_step(
            step, args.shell, args.workdir, args.out, os_name, args.step_timeout, echo=True
        )
        if in_actions:
            print("::endgroup::", flush=True)
        print(f"[{result.status}] {result.label}: {result.seconds:.1f} s", flush=True)
        results.append(result)
        failed = result.status != "ok"

    (args.out / "results.json").write_text(
        json.dumps(
            {
                "os": os_name,
                "machine": platform.machine(),
                "source": plan["source"],
                "chronozarr_version": plan["chronozarr_version"],
                "skipped": plan["skipped"],
                "not_run": plan["not_run"],
                "steps": [asdict(r) for r in results],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    summary = render_summary(os_name, results, plan, args.out)
    (args.out / "summary.md").write_text(summary, encoding="utf-8")
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as handle:
            handle.write(summary)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
