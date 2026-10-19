"""Install what the quickstart clean-room job needs beyond the commands on the page.

    runner_setup.py cloudflared --dest DIR [--github-actions]
    runner_setup.py wheel --steps STEPS.json --out DIR [--repo .] [--github-actions]

--github-actions makes the next workflow steps see the result: DIR goes on $GITHUB_PATH
(cloudflared), or UV_FIND_LINKS=DIR goes to $GITHUB_ENV (wheel). Python writes the path, so a
Windows runner gets its own separators.

Both keep the owner's 7-day quarantine: a pinned release at least 7 days old, with its SHA-256.

cloudflared: the page tells a macOS user to `brew install cloudflared`, which installs the
newest release. The job installs the pinned release below instead, for every OS, after checking
the digest GitHub shows for the asset (a macOS tgz is also checked after unpacking, against the
checksum in the release notes, which covers the binary inside the tgz).

wheel: chronozarr 0.4.1 was published on 2026-10-13, so PyPI's file is inside the quarantine and
uv's exclude-newer hides it. The page pins the version, so this builds the wheel from that
version's git tag (docs/release-0.4.1.md records the tag build as byte-identical to PyPI's file)
into a folder that holds nothing else. The job then sets UV_FIND_LINKS to the folder and keeps
UV_EXCLUDE_NEWER: uv does not apply exclude-newer to a --find-links file (it has no upload
time), so the page's install command runs unchanged, and every dependency still comes from PyPI
under the cutoff. That exemption is why the folder must hold exactly one chronozarr wheel.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# Published 2026-10-05. 2026.10.1 (2026-10-15) is inside the 7-day quarantine until 2026-10-22.
CLOUDFLARED_VERSION = "2026.10.0"
CLOUDFLARED_URL = "https://github.com/cloudflare/cloudflared/releases/download/{version}/{name}"


@dataclass(frozen=True)
class Asset:
    name: str
    sha256: str  # of the downloaded file: the digest GitHub shows for the release asset
    member: str | None = None  # the binary inside a tgz
    member_sha256: str | None = None  # of that binary: the checksum in the release notes


CLOUDFLARED_ASSETS = {
    ("linux", "x86_64"): Asset(
        "cloudflared-linux-amd64",
        "d33ff2d14475178d2012c2c56beba87389ac5ded27649519f198a7d3134a99db",
    ),
    ("linux", "aarch64"): Asset(
        "cloudflared-linux-arm64",
        "e6422b9d4f72d3194bc5a38676f13667c06666523217b842a877d72a80b5ac08",
    ),
    ("darwin", "arm64"): Asset(
        "cloudflared-darwin-arm64.tgz",
        "a2f79ff7b9420aa537d74af239f376da170bbabeb529aec416002adac6a72e70",
        member="cloudflared",
        member_sha256="72edfd3eea463aef4d5cb89e2e209cecb048cc756c2b01915de2e0ad7cb39830",
    ),
    ("windows", "amd64"): Asset(
        "cloudflared-windows-amd64.exe",
        "86aee4017b26625cee8484c113558f48effa4cd47f7aa05fcf425604e5d2b23c",
    ),
}


class SetupError(Exception):
    """A setup step cannot run or its result does not match what is pinned."""


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch(url: str, attempts: int = 3) -> bytes:
    """Download a URL into memory, retrying a few times on network errors."""
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(url, timeout=120) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == attempts:
                raise SetupError(f"download of {url} failed {attempts} times: {exc}") from exc
            time.sleep(2 * attempt)
    raise AssertionError("unreachable")


def check_digest(label: str, data: bytes, expected: str) -> None:
    actual = sha256_hex(data)
    if actual != expected:
        raise SetupError(
            f"{label}: SHA-256 is {actual}, pinned {expected}. Do not loosen the pin; find out "
            "why the file changed"
        )


def platform_key(system: str | None = None, machine: str | None = None) -> tuple[str, str]:
    return (system or platform.system()).lower(), (machine or platform.machine()).lower()


def install_cloudflared(
    dest: Path,
    key: tuple[str, str] | None = None,
    download=fetch,
) -> Path:
    """Put the pinned cloudflared for this OS and CPU into ``dest`` and return its path."""
    key = key or platform_key()
    asset = CLOUDFLARED_ASSETS.get(key)
    if asset is None:
        raise SetupError(
            f"no pinned cloudflared {CLOUDFLARED_VERSION} for {key}; pinned: "
            f"{sorted(CLOUDFLARED_ASSETS)}"
        )
    url = CLOUDFLARED_URL.format(version=CLOUDFLARED_VERSION, name=asset.name)
    data = download(url)
    check_digest(asset.name, data, asset.sha256)
    if asset.member is not None:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            extracted = archive.extractfile(asset.member)
            if extracted is None:
                raise SetupError(f"{asset.name} holds no file named {asset.member!r}")
            data = extracted.read()
        if asset.member_sha256 is not None:
            check_digest(f"{asset.name}:{asset.member}", data, asset.member_sha256)
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / ("cloudflared.exe" if key[0] == "windows" else "cloudflared")
    target.write_bytes(data)
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


def build_tag_wheel(version: str, out: Path, repo: Path) -> Path:
    """Build chronozarr's wheel from the git tag ``v<version>`` into an otherwise empty ``out``."""
    tag = f"v{version}"
    if out.exists() and any(out.iterdir()):
        raise SetupError(f"{out} is not empty; the wheel folder must hold the one wheel only")
    known = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{tag}^{{commit}}"],
        capture_output=True,
        text=True,
    )
    if known.returncode != 0:
        raise SetupError(
            f"tag {tag} is not in the checkout at {repo}; check out with fetch-depth 0 and "
            "fetch-tags, and make sure the docs pin a released version"
        )
    archive = subprocess.run(
        ["git", "-C", str(repo), "archive", "--format=tar", tag], capture_output=True, check=True
    )
    out.mkdir(parents=True, exist_ok=True)
    # The build must leave nothing behind that the page's install would find already there: the tag
    # pins Python 3.13 in .python-version (uv would download it), and a warm cache hides downloads.
    # So build with this script's own interpreter and a cache that is deleted afterwards.
    with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as cache:
        with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as tree:
            tree.extractall(source, filter="data")
        subprocess.run(
            [
                "uv",
                "build",
                "--wheel",
                "--python",
                sys.executable,
                "--out-dir",
                str(out.resolve()),
            ],
            cwd=source,
            env={**os.environ, "UV_CACHE_DIR": cache},
            check=True,
        )
    # uv build drops a .gitignore into --out-dir; uv skips it when it reads find-links.
    wheels = sorted(path for path in out.iterdir() if path.name != ".gitignore")
    expected = f"chronozarr-{version}-"
    if len(wheels) != 1 or not wheels[0].name.startswith(expected) or wheels[0].suffix != ".whl":
        raise SetupError(
            f"expected exactly one {expected}*.whl in {out}, found {[w.name for w in wheels]}"
        )
    return wheels[0]


def append_github_file(variable: str, line: str) -> None:
    """Append a line to the file that GitHub Actions names in ``variable``."""
    target = os.environ.get(variable)
    if not target:
        raise SetupError(f"--github-actions needs ${variable}, which only a workflow step has")
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    cloudflared = commands.add_parser("cloudflared", help="install the pinned cloudflared")
    cloudflared.add_argument("--dest", type=Path, required=True)
    cloudflared.add_argument("--github-actions", action="store_true")
    wheel = commands.add_parser("wheel", help="build the documented version from its git tag")
    wheel.add_argument(
        "--steps", type=Path, required=True, help="JSON from quickstart_commands.py"
    )
    wheel.add_argument("--out", type=Path, required=True)
    wheel.add_argument("--repo", type=Path, default=Path.cwd())
    wheel.add_argument("--github-actions", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "cloudflared":
            path = install_cloudflared(args.dest)
            version = subprocess.run(
                [str(path), "--version"], capture_output=True, text=True, check=True
            )
            print(f"{path}: {version.stdout.strip()}")
            if args.github_actions:
                append_github_file("GITHUB_PATH", str(args.dest.resolve()))
        else:
            plan = json.loads(args.steps.read_text(encoding="utf-8"))
            built = build_tag_wheel(plan["chronozarr_version"], args.out, args.repo)
            print(f"{built}\nsha256 {hashlib.sha256(built.read_bytes()).hexdigest()}")
            if args.github_actions:
                append_github_file("GITHUB_ENV", f"UV_FIND_LINKS={args.out.resolve()}")
    except (SetupError, subprocess.CalledProcessError, OSError) as exc:
        print(f"runner_setup: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
