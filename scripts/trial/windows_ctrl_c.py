"""Qualify the frozen wheel using actual Windows CTRL_C_EVENT broadcasts.

Every case owns a new console. GenerateConsoleCtrlEvent(CTRL_C_EVENT, 0)
signals that console as keyboard Ctrl-C normally does. Successful CLI cases
must not use forced cleanup. No CREATE_NEW_PROCESS_GROUP disables Ctrl-C.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import importlib.metadata
import json
import os
import platform
import re
import socket
import subprocess
import sys
import time
import traceback
from ctypes import wintypes
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen


class Windows:
    def __init__(self):
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.k.GetConsoleProcessList.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
        self.k.GetConsoleProcessList.restype = wintypes.DWORD
        self.k.GenerateConsoleCtrlEvent.argtypes = [wintypes.DWORD, wintypes.DWORD]
        self.k.GenerateConsoleCtrlEvent.restype = wintypes.BOOL
        self.k.SetConsoleCtrlHandler.argtypes = [ctypes.c_void_p, wintypes.BOOL]
        self.k.SetConsoleCtrlHandler.restype = wintypes.BOOL
        self.k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.k.OpenProcess.restype = wintypes.HANDLE
        self.k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.k.WaitForSingleObject.restype = wintypes.DWORD
        self.k.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.k.TerminateProcess.restype = wintypes.BOOL
        self.k.CloseHandle.argtypes = [wintypes.HANDLE]
        self.k.CloseHandle.restype = wintypes.BOOL

    def check(self, result):
        if not result:
            raise ctypes.WinError(ctypes.get_last_error())
        return result

    def pids(self):
        pids = (wintypes.DWORD * 256)()
        count = self.check(self.k.GetConsoleProcessList(pids, len(pids)))
        assert count <= len(pids), "over 256 processes in isolated console"
        return list(pids[:count])

    def handle(self, pid):
        # SYNCHRONIZE | PROCESS_TERMINATE; retained handles avoid PID reuse errors.
        return self.check(self.k.OpenProcess(0x00100000 | 0x0001, False, pid))

    def alive(self, handle):
        state = self.k.WaitForSingleObject(handle, 0)
        assert state in (0, 258), f"WaitForSingleObject returned {state}"
        return state == 258

    def interrupt(self):
        # Ctrl-C cannot target a nonzero process-group ID (Microsoft's contract).
        self.check(self.k.GenerateConsoleCtrlEvent(0, 0))


def text(path):
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def root_answers(url, expected):
    try:
        with urlopen(
            Request(url, headers={"User-Agent": "chronozarr-windows-ctrlc-qa"}), timeout=5
        ) as r:
            return r.status == 200 and r.read() == expected
    except OSError:
        return False


def port_is_open(port):
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def helper(spec_path):
    spec = json.loads(spec_path.read_text())
    out = Path(spec["out"])
    log = out / "command.log"
    result = {
        "case": spec["case"],
        "status": "failed",
        "method": "GenerateConsoleCtrlEvent(CTRL_C_EVENT, 0)",
        "forced_cleanup_used": False,
        "command": spec["command"],
    }
    win = Windows()
    handles = {}
    process = None
    event_sent = False
    received = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

    @callback_type
    def controller_handler(event):
        if event == 0:
            received.append(event)
            return True
        return False

    try:
        # A custom handler is per-process. NULL, TRUE would instead create an
        # inherited ignore attribute and make the child test invalid.
        win.check(win.k.SetConsoleCtrlHandler(None, False))
        with log.open("wb") as stream:
            process = subprocess.Popen(
                spec["command"],
                cwd=spec["cwd"],
                stdout=stream,
                stderr=subprocess.STDOUT,
                env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1"},
            )
            win.check(
                win.k.SetConsoleCtrlHandler(ctypes.cast(controller_handler, ctypes.c_void_p), True)
            )
            result["pid"] = process.pid
            deadline = time.monotonic() + 180
            expected = Path(spec["store"], "zarr.json").read_bytes() if spec.get("store") else b""
            local_url = (
                f"http://127.0.0.1:{spec['port']}/{Path(spec['store']).name}/zarr.json"
                if spec.get("store")
                else ""
            )
            while True:
                output = text(log)
                if process.poll() is not None:
                    raise RuntimeError(
                        f"command exited before readiness: {process.returncode}: {output[-1500:]}"
                    )
                if spec["kind"] == "control":
                    ready = "CONTROL_READY" in output
                elif spec["kind"] == "share-startup":
                    ready = (
                        "waiting for Cloudflare to assign a public address" in output
                        and root_answers(local_url, expected)
                    )
                    if ready:
                        assert "open: " not in output, "startup checkpoint was missed"
                else:
                    ready = "press Ctrl-C to stop" in output and bool(
                        re.search(r"^open: (\S+)", output, re.M)
                    )
                    if ready:
                        ready = root_answers(local_url, expected)
                        link = re.search(r"^open: (\S+)", output, re.M).group(1)
                        store_url = parse_qs(urlsplit(link).query)["store"][0]
                        if spec["kind"] == "share-ready":
                            ready = ready and root_answers(
                                store_url.rstrip("/") + "/zarr.json", expected
                            )
                            result["public_store"] = store_url
                if ready:
                    break
                assert time.monotonic() < deadline, "readiness deadline exceeded"
                time.sleep(0.1)
            result["ready"] = True
            before = win.pids()
            result["console_pids_before"] = before
            assert os.getpid() in before and process.pid in before, (
                "command does not share the isolated console"
            )
            for pid in before:
                if pid != os.getpid():
                    handles[pid] = win.handle(pid)
            result["tracked_pids"] = list(handles)
            if spec["kind"].startswith("share"):
                ps = subprocess.run(
                    [
                        "powershell.exe",
                        "-NoProfile",
                        "-Command",
                        "Get-Process cloudflared -ErrorAction SilentlyContinue | "
                        "Select-Object -ExpandProperty Id",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                tunnel_pids = [int(p) for p in ps.stdout.split() if p.isdigit()]
                result["cloudflared_pids_before"] = [p for p in tunnel_pids if p in before]
                assert result["cloudflared_pids_before"], "no real cloudflared in console"
            result["local_root_verified"] = bool(local_url)
            started = time.monotonic()
            win.interrupt()
            event_sent = True
            result["event_sent"] = True
            if spec.get("ignore_control"):
                time.sleep(2)
                assert process.poll() is None, "negative control did not ignore Ctrl-C"
                assert win.alive(handles[process.pid]), "negative control exited"
                result["negative_control_rejected"] = True
            else:
                result["exit_code"] = process.wait(timeout=30)
                result["seconds_after_ctrl_c"] = time.monotonic() - started
                assert result["exit_code"] == 0, f"exit code {result['exit_code']}"
                stopped = text(log)
                if spec["kind"] == "control":
                    assert "CONTROL_SIGINT_HANDLED" in stopped, (
                        "positive control did not catch KeyboardInterrupt"
                    )
                else:
                    result["stopped_message"] = bool(re.search(r"^stopped\s*$", stopped, re.M))
                    assert result["stopped_message"], (
                        "application did not print clean-shutdown message"
                    )
                    assert "Traceback (most recent call last)" not in stopped
                    until = time.monotonic() + 5
                    while port_is_open(spec["port"]) and time.monotonic() < until:
                        time.sleep(0.1)
                    result["port_closed"] = not port_is_open(spec["port"])
                    assert result["port_closed"], "server port still accepts connections"
                    with socket.socket() as s:
                        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                        s.bind(("127.0.0.1", spec["port"]))
                    result["port_rebound"] = True
                until = time.monotonic() + 5
                while any(win.alive(h) for h in handles.values()) and time.monotonic() < until:
                    time.sleep(0.1)
                result["processes_still_alive"] = [p for p, h in handles.items() if win.alive(h)]
                assert not result["processes_still_alive"], "child processes survived Ctrl-C"
            assert received, "controller did not receive console broadcast"
            result["controller_received_ctrl_c"] = True
            result["status"] = "passed"
    except BaseException:
        result["error"] = traceback.format_exc()
    finally:
        # Cleanup cannot turn a failed case into a pass. Only the intentionally
        # ignoring harness control is allowed to need forced cleanup.
        if process is not None:
            if not event_sent and process.poll() is None:
                try:
                    win.interrupt()
                    process.wait(timeout=10)
                except BaseException:
                    pass
            for pid in win.pids():
                if pid != os.getpid() and pid not in handles:
                    with contextlib.suppress(OSError):
                        handles[pid] = win.handle(pid)
            for handle in handles.values():
                if win.alive(handle):
                    result["forced_cleanup_used"] = True
                    win.check(win.k.TerminateProcess(handle, 199))
                    win.k.WaitForSingleObject(handle, 5000)
            for handle in handles.values():
                win.k.CloseHandle(handle)
        if spec["kind"] != "control" and result["forced_cleanup_used"]:
            result["status"] = "failed"
        (out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return 0 if result["status"] == "passed" else 1


def driver(args):
    import numpy as np

    import chronozarr

    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    package = Path(chronozarr.__file__).resolve()
    assert package.is_relative_to(Path(sys.prefix).resolve()), f"not installed package: {package}"
    assert importlib.metadata.version("chronozarr") == "0.5.2"
    store = out / "synthetic_store"
    chronozarr.encode(
        [np.full((4, 16, 16), v, dtype="uint16") for v in (1200, 1600)],
        store,
        crs="EPSG:32611",
        transform=(10, 0, 700000, 0, -10, 4000000),
        times=np.array(["2024-01-01", "2024-02-01"], dtype="datetime64[ms]"),
        bands=["B02", "B03", "B04", "B08"],
        n_lods=1,
    )
    (out / "environment.json").write_text(
        json.dumps(
            {
                "platform": platform.platform(),
                "python": sys.version,
                "python_executable": sys.executable,
                "base_python": sys._base_executable,
                "installed_package": str(package),
                "version": "0.5.2",
                "source_ref": os.environ["SOURCE_REF"],
                "wheel_sha256": os.environ["QUALIFIED_WHEEL_SHA256"],
                "fixture": (
                    "synthetic two-date four-band 16x16 store; project examples and R2 unchanged"
                ),
                "method_reference": (
                    "https://learn.microsoft.com/en-us/windows/console/generateconsolectrlevent"
                ),
                "scope": "real Windows console events, not a physical keyboard/terminal UI test",
            },
            indent=2,
        )
        + "\n"
    )
    positive = (
        "import time\nprint('CONTROL_READY',flush=True)\ntry:\n"
        " while True: time.sleep(.1)\nexcept KeyboardInterrupt:\n"
        " print('CONTROL_SIGINT_HANDLED',flush=True)\n"
    )
    negative = (
        "import signal,time\nsignal.signal(signal.SIGINT,signal.SIG_IGN)\n"
        "print('CONTROL_READY',flush=True)\nwhile True: time.sleep(.1)\n"
    )
    specs = [
        {
            "case": "positive-control",
            "kind": "control",
            "command": [sys._base_executable, "-u", "-c", positive],
        },
        {
            "case": "ignoring-control",
            "kind": "control",
            "command": [sys._base_executable, "-u", "-c", negative],
            "ignore_control": True,
        },
    ]
    selected = (
        ("share-ready",)
        if args.cases == "share-ready"
        else ("preview-ready", "share-startup", "share-ready")
    )
    for case in selected:
        port = free_port()
        specs.append(
            {
                "case": case,
                "kind": case,
                "store": str(store),
                "port": port,
                "command": [
                    str(args.cli.resolve()),
                    case.split("-")[0],
                    str(store),
                    "--port",
                    str(port),
                    "--no-open",
                ],
            }
        )
    results = []
    for spec in specs:
        case_out = out / spec["case"]
        case_out.mkdir()
        spec.update(out=str(case_out), cwd=str(out))
        spec_path = case_out / "spec.json"
        spec_path.write_text(json.dumps(spec, indent=2) + "\n")
        with (case_out / "controller.log").open("wb") as log:
            controller = subprocess.Popen(
                [sys._base_executable, str(Path(__file__).resolve()), "--helper", str(spec_path)],
                creationflags=subprocess.CREATE_NEW_CONSOLE,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                controller.wait(timeout=260)
            except subprocess.TimeoutExpired:
                subprocess.run(
                    ["taskkill", "/PID", str(controller.pid), "/T", "/F"],
                    capture_output=True,
                    check=False,
                )
                raise RuntimeError(
                    f"controller timeout in {spec['case']}; cannot qualify"
                ) from None
        result_path = case_out / "result.json"
        if not result_path.exists():
            raise RuntimeError(
                f"no controller result for {spec['case']}: {text(case_out / 'controller.log')}"
            )
        result = json.loads(result_path.read_text())
        results.append(result)
        (out / "results.json").write_text(json.dumps({"results": results}, indent=2) + "\n")
        print(json.dumps(result), flush=True)
        if spec["kind"] == "control" and result["status"] != "passed":
            raise RuntimeError("harness control failed; CLI qualification not attempted")
    assert all(r["status"] == "passed" for r in results), "case failed; inspect preserved logs"
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--helper", type=Path)
    parser.add_argument("--cli", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--cases", choices=("all", "share-ready"), default="all")
    args = parser.parse_args()
    assert os.name == "nt", "real Windows only; no POSIX signal emulation"
    return helper(args.helper) if args.helper else driver(args)


if __name__ == "__main__":
    raise SystemExit(main())
