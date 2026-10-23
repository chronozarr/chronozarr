"""Check that Python, npm, lockfiles and an optional release tag name the same release."""

from __future__ import annotations

import argparse
import json
import re
import tomllib
from pathlib import Path


def npm_version(python_version: str) -> str:
    """Spell a supported PEP 440 release as semver, including its prerelease number."""
    match = re.fullmatch(r"(\d+\.\d+\.\d+)(?:(a|b|rc)(\d+)|\.dev(\d+))?", python_version)
    if match is None:
        raise ValueError(f"unsupported release version: {python_version!r}")
    release, phase, number, dev = match.groups()
    if dev is not None:
        return f"{release}-dev.{dev}"
    if phase is not None:
        phase = {"a": "alpha", "b": "beta", "rc": "rc"}[phase]
        return f"{release}-{phase}.{number}"
    return release


def check_versions(root: Path, tag: str | None = None) -> tuple[str, str]:
    """Refuse a mixed release before building or publishing either package."""
    project = tomllib.loads((root / "pyproject.toml").read_text())
    python_version = project["project"]["version"]
    expected_npm = npm_version(python_version)
    package = json.loads((root / "js/package.json").read_text())
    lock = json.loads((root / "js/package-lock.json").read_text())
    python_lock = tomllib.loads((root / "uv.lock").read_text())
    locked_project = [p for p in python_lock["package"] if p["name"] == "chronozarr"]
    if len(locked_project) != 1 or locked_project[0]["version"] != python_version:
        raise ValueError(f"uv.lock must select chronozarr {python_version}")
    for label, actual in (
        ("js/package.json", package["version"]),
        ("js/package-lock.json", lock["version"]),
        ("js/package-lock.json root package", lock["packages"][""]["version"]),
    ):
        if actual != expected_npm:
            raise ValueError(f"{label} version {actual!r} must be {expected_npm!r}")
    if tag is not None and tag != f"v{expected_npm}":
        raise ValueError(f"release tag {tag!r} must be 'v{expected_npm}'")
    return python_version, expected_npm


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="expected shared release tag, using npm semver spelling")
    args = parser.parse_args()
    try:
        python_version, javascript_version = check_versions(
            Path(__file__).resolve().parents[1], args.tag
        )
    except (ValueError, KeyError, OSError) as error:
        parser.exit(1, f"release versions: {error}\n")
    print(f"release versions: Python {python_version}; npm {javascript_version}")


if __name__ == "__main__":
    main()
