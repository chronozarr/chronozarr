"""run_quickstart.py and runner_setup.py: running and recording the clean-room steps."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[1]
POSIX_BASH = os.name != "nt" and shutil.which("bash") is not None
needs_bash = pytest.mark.skipif(not POSIX_BASH, reason="runs steps through bash on a POSIX host")


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / "trial" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve the module's annotations here
    spec.loader.exec_module(module)
    return module


rq = load_script("run_quickstart")
setup = load_script("runner_setup")

TUNNEL = {
    "kind": "open-link",
    "scheme": "https",
    "host_suffix": ".trycloudflare.com",
    "timeout_seconds": 5,
}
LOCAL = {"kind": "open-link", "scheme": "http", "host": "127.0.0.1", "timeout_seconds": 5}


def step(text: str, index: int = 1, **overrides):
    return {
        "index": index,
        "heading": "h",
        "label": f"{index:02d} {text.splitlines()[0]}",
        "text": text,
        "long_running": False,
        "ready": None,
        **overrides,
    }


def run(tmp_path: Path, spec: dict, timeout: float = 30):
    out, work = tmp_path / "out", tmp_path / "work"
    (out / "steps").mkdir(parents=True)
    work.mkdir()
    result = rq.run_step(spec, "bash", work, out, "Linux", timeout, echo=False)
    return result, out


# --- what is handed to the shell -------------------------------------------------------------


def test_bash_script_is_the_step_text_and_nothing_else():
    extension, content = rq.step_script("bash", "uv tool install x\nchronozarr --version")

    assert (extension, content) == (".sh", "uv tool install x\nchronozarr --version\n")


@needs_bash
def test_bash_command_skips_profiles_and_stops_at_the_first_failing_line():
    command = rq.shell_command("bash", Path("1.sh"))

    assert command[1:] == ["--noprofile", "--norc", "-e", "1.sh"]


def test_powershell_script_is_a_preamble_then_the_step_text():
    extension, content = rq.step_script("pwsh", "chronozarr share my_store")

    assert extension == ".ps1"
    assert content.startswith("$ErrorActionPreference = 'Stop'\n")
    assert "$PSNativeCommandUseErrorActionPreference = $true\n" in content
    assert content.endswith("\nchronozarr share my_store\n")


def test_steps_get_the_environment_a_terminal_would_give():
    environment = rq.step_environment()

    assert environment["PYTHONUNBUFFERED"] == "1"
    assert environment["PYTHONUTF8"] == "1"


# --- reading the printed link ----------------------------------------------------------------


def test_store_comes_from_the_open_line_only_once_it_is_printed():
    link = "https://chronozarr.org/demo/?store=https%3A%2F%2Fa-b.trycloudflare.com%2Fmy_store"

    assert rq.store_from_output(["starting local server", "waiting..."]) is None
    assert rq.store_from_output(["x", f"open: {link}", "stopped"]) == (
        "https://a-b.trycloudflare.com/my_store"
    )


def test_an_open_line_without_a_store_parameter_is_an_error():
    with pytest.raises(ValueError, match="no store parameter"):
        rq.store_from_output(["open: https://chronozarr.org/demo/"])


@pytest.mark.parametrize(
    ("store", "ready", "message"),
    [
        ("http://x.trycloudflare.com/s", TUNNEL, "is not https"),
        ("https://example.org/s", TUNNEL, r"not on a \.trycloudflare\.com"),
        ("https://x.trycloudflare.com.evil.org/s", TUNNEL, "not on a"),
        ("http://localhost:1/s", LOCAL, "not on 127.0.0.1"),
        ("https://127.0.0.1:1/s", LOCAL, "is not http"),
    ],
)
def test_a_link_to_the_wrong_host_is_rejected(store, ready, message):
    with pytest.raises(ValueError, match=message):
        rq.check_store_host(store, ready)


def test_the_expected_hosts_are_accepted():
    rq.check_store_host("https://quiet-river-1.trycloudflare.com/my_store", TUNNEL)
    rq.check_store_host("http://127.0.0.1:50321/my_store", LOCAL)


# --- running steps ---------------------------------------------------------------------------


@needs_bash
def test_a_passing_step_keeps_its_output_verbatim(tmp_path):
    result, out = run(tmp_path, step("echo one\necho two >&2\nprintf 'no newline'"))

    assert result.status == "ok"
    assert result.exit_code == 0
    assert (out / result.log).read_bytes() == b"one\ntwo\nno newline"
    assert (out / "steps" / "01.sh").read_text() == "echo one\necho two >&2\nprintf 'no newline'\n"


@needs_bash
def test_a_failing_line_fails_the_step_even_when_a_later_line_passes(tmp_path):
    result, out = run(tmp_path, step("false\necho reached"))

    assert result.status == "failed"
    assert result.exit_code == 1
    assert "reached" not in (out / result.log).read_text()


@needs_bash
def test_a_hanging_step_times_out_and_is_killed(tmp_path):
    result, _ = run(tmp_path, step("sleep 30"), timeout=1)

    assert result.status == "timeout"
    assert result.seconds < 10
    assert "killed after" in result.note


def fake_share(tmp_path: Path, status: int, exit_after: float | None = None) -> str:
    """A command that prints a link and serves <store>/zarr.json with ``status`` until SIGINT."""
    script = tmp_path / "fake_share.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import http.server, signal, sys, time

            class Handler(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response({status})
                    self.end_headers()
                    self.wfile.write(b"{{}}")
                def log_message(self, *args):
                    pass

            server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
            port = server.server_address[1]
            print("serving", flush=True)
            store = f"http%3A%2F%2F127.0.0.1%3A{{port}}%2Fstore"
            print(f"open: https://chronozarr.org/demo/?store={{store}}", flush=True)
            if {exit_after!r} is not None:
                time.sleep({exit_after!r})
                sys.exit(3)
            server.timeout = 0.2
            try:
                while True:
                    server.handle_request()
            except KeyboardInterrupt:
                print("stopped", flush=True)
            """
        )
    )
    return f'"{sys.executable}" "{script}"'


@needs_bash
def test_a_long_running_step_is_ready_when_the_printed_store_answers_then_stops_on_sigint(
    tmp_path,
):
    spec = step(fake_share(tmp_path, 200), long_running=True, ready=LOCAL)

    result, out = run(tmp_path, spec)

    assert result.status == "ok", result.note
    assert "returned 200" in result.note
    assert "stopped by SIGINT, exit 0" in result.note
    assert result.seconds < 5
    assert (out / result.log).read_text().splitlines()[-1] == "stopped"


@needs_bash
def test_a_store_that_never_answers_200_fails_the_long_running_step(tmp_path):
    spec = step(
        fake_share(tmp_path, 404), long_running=True, ready={**LOCAL, "timeout_seconds": 3}
    )

    result, _ = run(tmp_path, spec)

    assert result.status == "timeout"
    assert "returned 404" in result.note


@needs_bash
def test_a_long_running_step_that_exits_early_fails_with_its_exit_code(tmp_path):
    spec = step(fake_share(tmp_path, 404, exit_after=0.3), long_running=True, ready=LOCAL)

    result, _ = run(tmp_path, spec)

    assert result.status == "failed"
    assert "exited with 3" in result.note


@needs_bash
def test_a_link_on_the_wrong_host_fails_the_long_running_step(tmp_path):
    spec = step(fake_share(tmp_path, 200), long_running=True, ready=TUNNEL)

    result, _ = run(tmp_path, spec)

    assert result.status == "failed"
    assert "is not https" in result.note


# --- the summary -----------------------------------------------------------------------------


def make_result(index: int, status: str, seconds: float, label: str, **extra):
    fields = {
        "index": index,
        "label": label,
        "heading": "h",
        "os": "Linux",
        "shell": "bash",
        "long_running": False,
        "status": status,
        "seconds": seconds,
        "exit_code": 0 if status == "ok" else 1,
        "note": "",
        "log": f"steps/{index:02d}.log",
    }
    return rq.Result(**{**fields, **extra})


def test_summary_table_lists_step_os_seconds_status_and_tails_the_failing_log(tmp_path):
    (tmp_path / "steps").mkdir()
    (tmp_path / "steps" / "02.log").write_text("\n".join(f"line {n}" for n in range(200)) + "\n")
    results = [
        make_result(1, "ok", 12.34, "01 uv tool install | x"),
        make_result(2, "failed", 0.5, "02 chronozarr convert"),
        make_result(3, "not run", 0.0, "03 chronozarr share", exit_code=None, log=""),
    ]
    plan = {
        "source": "docs/quickstart.md",
        "chronozarr_version": "1.2.3",
        "skipped": [{"line": 16, "reason": "an alternative", "text": "python -m pip install x"}],
        "not_run": [{"line": 137, "heading": "7. Read", "reason": "a Python block"}],
    }

    summary = rq.render_summary("Linux", results, plan, tmp_path)

    assert "| Step | OS | Seconds | Status |" in summary
    assert "| 01 uv tool install \\| x | Linux | 12.3 | ok |" in summary
    assert "| 03 chronozarr share | Linux | 0.0 | not run |" in summary
    assert "Skipped (line 16): `python -m pip install x`" in summary
    assert "Not run (line 137, 7. Read)" in summary
    assert "### Failed: 02 chronozarr convert (failed, exit 1)" in summary
    assert "line 199" in summary
    assert "line 139\n" not in summary  # only the last 60 lines


def test_a_passing_run_has_no_failure_section(tmp_path):
    plan = {"source": "p.md", "chronozarr_version": "1", "skipped": [], "not_run": []}

    summary = rq.render_summary("Linux", [make_result(1, "ok", 1.0, "01 x")], plan, tmp_path)

    assert "Failed" not in summary


@needs_bash
def test_main_stops_at_the_first_failure_and_writes_the_records(tmp_path, monkeypatch):
    plan = {
        "source": "p.md",
        "chronozarr_version": "1",
        "skipped": [],
        "not_run": [],
        "steps": [step("echo a", 1), step("exit 7", 2), step("echo never", 3)],
    }
    steps = tmp_path / "steps.json"
    steps.write_text(json.dumps(plan))
    summary_file = tmp_path / "github_summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))
    monkeypatch.setenv("RUNNER_OS", "Linux")
    out = tmp_path / "out"

    code = rq.main([str(steps), "--workdir", str(tmp_path / "work"), "--out", str(out)])

    assert code == 1
    results = json.loads((out / "results.json").read_text())
    assert [(s["index"], s["status"]) for s in results["steps"]] == [
        (1, "ok"),
        (2, "failed"),
        (3, "not run"),
    ]
    assert (out / "summary.md").read_text() == summary_file.read_text()
    assert (out / "environment.txt").is_file()
    assert not (out / "steps" / "03.log").exists()


def test_main_refuses_a_workdir_that_is_not_empty(tmp_path):
    steps = tmp_path / "steps.json"
    steps.write_text('{"source": "p", "chronozarr_version": "1", "steps": []}')
    work = tmp_path / "work"
    work.mkdir()
    (work / "leftover").write_text("x")

    assert rq.main([str(steps), "--workdir", str(work), "--out", str(tmp_path / "out")]) == 2


# --- installing the pinned tools -------------------------------------------------------------


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_cloudflared_is_pinned_for_every_runner_platform():
    assert set(setup.CLOUDFLARED_ASSETS) >= {
        ("linux", "x86_64"),
        ("darwin", "arm64"),
        ("windows", "amd64"),
    }
    for asset in setup.CLOUDFLARED_ASSETS.values():
        assert len(asset.sha256) == 64


def test_platform_key_lowercases_what_python_reports():
    assert setup.platform_key("Windows", "AMD64") == ("windows", "amd64")
    assert setup.platform_key("Darwin", "arm64") == ("darwin", "arm64")


def test_a_cloudflared_download_that_matches_its_pin_is_installed_executable(
    tmp_path, monkeypatch
):
    binary = b"#!/bin/sh\necho cloudflared version test\n"
    pinned = setup.Asset("cloudflared-linux-amd64", sha(binary))
    monkeypatch.setitem(setup.CLOUDFLARED_ASSETS, ("linux", "x86_64"), pinned)
    urls = []

    def download(url: str) -> bytes:
        urls.append(url)
        return binary

    path = setup.install_cloudflared(tmp_path / "bin", ("linux", "x86_64"), download)

    assert path == tmp_path / "bin" / "cloudflared"
    assert path.read_bytes() == binary
    assert os.access(path, os.X_OK) or os.name == "nt"
    assert urls == [
        f"https://github.com/cloudflare/cloudflared/releases/download/"
        f"{setup.CLOUDFLARED_VERSION}/cloudflared-linux-amd64"
    ]


def test_the_windows_asset_is_installed_as_an_exe(tmp_path, monkeypatch):
    binary = b"MZ not really"
    monkeypatch.setitem(
        setup.CLOUDFLARED_ASSETS,
        ("windows", "amd64"),
        setup.Asset("cloudflared-windows-amd64.exe", sha(binary)),
    )

    path = setup.install_cloudflared(tmp_path, ("windows", "amd64"), lambda url: binary)

    assert path.name == "cloudflared.exe"


def test_a_changed_download_is_refused_and_nothing_is_written(tmp_path):
    with pytest.raises(setup.SetupError, match=r"SHA-256 is .*pinned"):
        setup.install_cloudflared(tmp_path / "bin", ("linux", "x86_64"), lambda url: b"tampered")

    assert not (tmp_path / "bin").exists()


def make_tgz(member: str, data: bytes) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo(member)
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_a_tgz_is_checked_whole_and_again_for_the_binary_inside(tmp_path, monkeypatch):
    binary = b"darwin binary"
    archive = make_tgz("cloudflared", binary)
    key = ("darwin", "arm64")
    good = setup.Asset("cloudflared-darwin-arm64.tgz", sha(archive), "cloudflared", sha(binary))
    monkeypatch.setitem(setup.CLOUDFLARED_ASSETS, key, good)

    path = setup.install_cloudflared(tmp_path / "ok", key, lambda url: archive)

    assert path.read_bytes() == binary

    wrong_member = setup.Asset(good.name, sha(archive), "cloudflared", sha(b"another binary"))
    monkeypatch.setitem(setup.CLOUDFLARED_ASSETS, key, wrong_member)
    with pytest.raises(setup.SetupError, match=r"cloudflared-darwin-arm64.tgz:cloudflared"):
        setup.install_cloudflared(tmp_path / "bad", key, lambda url: archive)


def test_an_unpinned_platform_names_the_pinned_ones(tmp_path):
    with pytest.raises(setup.SetupError, match=r"no pinned cloudflared .* \('freebsd', 'amd64'\)"):
        setup.install_cloudflared(tmp_path, ("freebsd", "amd64"))


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.org", *args],
        check=True,
        capture_output=True,
    )


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_a_tag_that_is_not_in_the_checkout_is_named(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "f").write_text("x")
    git(repo, "add", "f")
    git(repo, "commit", "-q", "-m", "one")

    with pytest.raises(setup.SetupError, match=r"tag v9\.9\.9 is not in the checkout"):
        setup.build_tag_wheel("9.9.9", tmp_path / "wheels", repo)

    assert not (tmp_path / "wheels").exists()


def test_the_wheel_folder_must_start_empty(tmp_path):
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    (wheels / "stray.whl").write_text("x")

    with pytest.raises(setup.SetupError, match="is not empty"):
        setup.build_tag_wheel("1.2.3", wheels, tmp_path)


def test_github_actions_files_need_their_variable(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_PATH", raising=False)
    with pytest.raises(setup.SetupError, match=r"needs \$GITHUB_PATH"):
        setup.append_github_file("GITHUB_PATH", "x")

    target = tmp_path / "path"
    monkeypatch.setenv("GITHUB_PATH", str(target))
    setup.append_github_file("GITHUB_PATH", "/a")
    setup.append_github_file("GITHUB_PATH", "/b")

    assert target.read_text() == "/a\n/b\n"


def fake_uv_build(monkeypatch, wheels: list[str], seen: list):
    """Replace only the `uv build` call: record it and drop ``wheels`` into its --out-dir."""
    real_run = subprocess.run

    def run(command, *args, **kwargs):
        if command[0] != "uv":
            return real_run(command, *args, **kwargs)
        seen.append((command, kwargs))
        out = Path(command[command.index("--out-dir") + 1])
        out.mkdir(parents=True, exist_ok=True)
        (out / ".gitignore").write_text("*\n")  # what uv build writes next to the wheel
        for name in wheels:
            (out / name).write_bytes(b"wheel")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(setup.subprocess, "run", run)


@pytest.fixture
def tagged_repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "pyproject.toml").write_text("[project]\nname = 'chronozarr'\n")
    (repo / ".python-version").write_text("3.13\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "release")
    git(repo, "tag", "v1.2.3")
    return repo


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_the_wheel_is_built_from_the_tag_with_this_interpreter_and_a_private_cache(
    tmp_path, tagged_repo, monkeypatch
):
    seen: list = []
    fake_uv_build(monkeypatch, ["chronozarr-1.2.3-py3-none-any.whl"], seen)

    built = setup.build_tag_wheel("1.2.3", tmp_path / "wheels", tagged_repo)

    assert built == tmp_path / "wheels" / "chronozarr-1.2.3-py3-none-any.whl"
    ((command, kwargs),) = seen
    assert command[:3] == ["uv", "build", "--wheel"]
    assert command[command.index("--python") + 1] == sys.executable
    assert Path(kwargs["cwd"]).name != "repo"  # built from the extracted tag, not the checkout
    assert kwargs["env"]["UV_CACHE_DIR"] != os.environ.get("UV_CACHE_DIR")


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
@pytest.mark.parametrize(
    "wheels",
    [
        [],
        ["chronozarr-1.2.3-py3-none-any.whl", "chronozarr-1.2.3-cp313-linux.whl"],
        ["chronozarr-1.2.4-py3-none-any.whl"],
        ["chronozarr-1.2.3-py3-none-any.whl", "numpy-2.5.3-py3-none-any.whl"],
    ],
)
def test_the_wheel_folder_must_end_with_exactly_the_documented_version(
    tmp_path, tagged_repo, monkeypatch, wheels
):
    fake_uv_build(monkeypatch, wheels, [])

    with pytest.raises(setup.SetupError, match=r"expected exactly one chronozarr-1\.2\.3-"):
        setup.build_tag_wheel("1.2.3", tmp_path / "wheels", tagged_repo)
