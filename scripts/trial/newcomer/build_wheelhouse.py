"""Fetch the wheelhouse of the newcomer images from a pylock.toml, with every hash verified.

    uv run --no-project --with packaging python build_wheelhouse.py \
        --lock pylock.wheelhouse.toml --out DIR --chronozarr-wheel PATH

pylock.wheelhouse.toml comes from `uv pip compile wheelhouse-requirements.in --universal
--python-version 3.12 --format pylock.toml`, run under the host's 7-day quarantine
(exclude-newer in ~/.config/uv/uv.toml). This script adds a second check: it refuses any file
uploaded in the last 7 days.

What lands in DIR: the wheels that a Linux aarch64 glibc machine running CPython 3.12, 3.13 or
3.14 can install (pure-Python wheels and those three interpreters' binary wheels), plus the
chronozarr 0.4.1 wheel built from the v0.4.1 tag, copied from --chronozarr-wheel. No sdists:
the images have no compiler.
"""

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import shutil
import sys
import tomllib
import urllib.request
from pathlib import Path

from packaging import tags
from packaging.utils import parse_wheel_filename

QUARANTINE = dt.timedelta(days=7)
TARGET_PYTHONS = [(3, 12), (3, 13), (3, 14)]
# Ubuntu 24.04 ships glibc 2.39.
GLIBC_MINOR_MAX = 39
CHRONOZARR_WHEEL_SHA256 = "e5ec2167370126decf78bd24419b736d0cee5240acd651a758f9fe2198f8dd00"


def platforms() -> list[str]:
    names = ["linux_aarch64", "manylinux2014_aarch64"]
    names += [f"manylinux_2_{minor}_aarch64" for minor in range(GLIBC_MINOR_MAX, 16, -1)]
    return names


def installable_tags(python: tuple[int, int]) -> set[tags.Tag]:
    interpreter = f"cp{python[0]}{python[1]}"
    plats = platforms()
    found = set(tags.cpython_tags(python, platforms=plats))
    found |= set(tags.compatible_tags(python, interpreter=interpreter, platforms=plats))
    return found


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def select_wheels(packages: list[dict]) -> tuple[list[dict], list[str]]:
    """The wheels to fetch, and a message for each package and interpreter without a wheel."""
    per_python = {python: installable_tags(python) for python in TARGET_PYTHONS}
    chosen: dict[str, dict] = {}
    problems: list[str] = []
    for package in packages:
        wheels = package.get("wheels", [])
        usable_by_python: dict[tuple[int, int], list[dict]] = {
            python: [] for python in TARGET_PYTHONS
        }
        for wheel in wheels:
            name = wheel["url"].rsplit("/", 1)[1]
            wheel_tags = parse_wheel_filename(name)[3]
            for python, accepted in per_python.items():
                if wheel_tags & accepted:
                    usable_by_python[python].append(wheel)
                    chosen[name] = wheel
        for python, usable in usable_by_python.items():
            if not usable:
                marker = package.get("marker", "")
                where = f" (marker: {marker})" if marker else ""
                problems.append(
                    f"{package['name']} {package['version']}: "
                    f"no wheel for linux aarch64 cp{python[0]}{python[1]}{where}"
                )
    return list(chosen.values()), problems


def check_quarantine(wheels: list[dict], now: dt.datetime) -> None:
    newest = max(wheel["upload-time"] for wheel in wheels)
    cutoff = now - QUARANTINE
    young = [wheel["url"] for wheel in wheels if wheel["upload-time"] > cutoff]
    if young:
        raise SystemExit(
            f"{len(young)} wheel(s) uploaded after {cutoff:%Y-%m-%dT%H:%M:%SZ}: {young[:3]}"
        )
    print(
        f"quarantine ok: newest upload {newest:%Y-%m-%dT%H:%M:%SZ}, "
        f"cutoff {cutoff:%Y-%m-%dT%H:%M:%SZ}"
    )


def fetch(wheel: dict, out: Path) -> tuple[str, str]:
    name = wheel["url"].rsplit("/", 1)[1]
    expected = wheel["hashes"]["sha256"]
    target = out / name
    if target.exists() and sha256_of(target) == expected:
        return name, "cached"
    partial = out / f"{name}.part"
    with (
        urllib.request.urlopen(wheel["url"], timeout=120) as response,
        partial.open("wb") as handle,
    ):
        shutil.copyfileobj(response, handle)
    actual = sha256_of(partial)
    if actual != expected:
        partial.unlink()
        raise SystemExit(f"hash mismatch for {name}: expected {expected}, got {actual}")
    partial.replace(target)
    return name, "fetched"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--chronozarr-wheel", type=Path, required=True)
    args = parser.parse_args()

    lock = tomllib.loads(args.lock.read_text())
    packages = [package for package in lock["packages"] if package["name"] != "chronozarr"]
    wheels, problems = select_wheels(packages)
    for problem in problems:
        print(f"no installable wheel: {problem}", file=sys.stderr)
    check_quarantine(wheels, dt.datetime.now(dt.UTC))

    args.out.mkdir(parents=True, exist_ok=True)
    local = args.chronozarr_wheel
    if sha256_of(local) != CHRONOZARR_WHEEL_SHA256:
        raise SystemExit(f"{local} is not the v0.4.1 tag wheel (sha256 {sha256_of(local)})")
    if not (args.out / local.name).exists():
        shutil.copyfile(local, args.out / local.name)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda wheel: fetch(wheel, args.out), wheels))
    fetched = sum(1 for _, state in results if state == "fetched")
    print(
        f"{len(results)} wheels ({fetched} fetched, {len(results) - fetched} cached) "
        f"+ {local.name}"
    )

    keep = {name for name, _ in results} | {local.name}
    for stray in args.out.iterdir():
        if stray.suffix == ".whl" and stray.name not in keep:
            print(f"stray file not in the lock: {stray.name}", file=sys.stderr)
            return 1
    listing = "".join(f"{sha256_of(args.out / name)}  {name}\n" for name in sorted(keep))
    (args.out / "SHA256SUMS").write_text(listing)
    print(f"wrote {args.out / 'SHA256SUMS'}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
