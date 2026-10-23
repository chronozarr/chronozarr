"""Collect the shipped tests from an isolated source archive, without relying on a Git checkout."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


def check_sdist(source: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="chronozarr-sdist-") as directory:
        destination = Path(directory).resolve()
        with tarfile.open(source) as archive:
            for member in archive.getmembers():
                target = (destination / member.name).resolve()
                if not target.is_relative_to(destination) or not (
                    member.isdir() or member.isfile()
                ):
                    raise ValueError(f"unexpected source-archive member: {member.name}")
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    content = archive.extractfile(member)
                    assert content is not None
                    with content, target.open("wb") as output:
                        shutil.copyfileobj(content, output)
        roots = list(destination.iterdir())
        if len(roots) != 1 or not (roots[0] / "pyproject.toml").is_file():
            raise ValueError("expected one source directory containing pyproject.toml")
        root = roots[0]
        env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(root), str(root / "src")])}
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "--collect-only",
                "-q",
                "-q",
                "-m",
                "unit",
                "-p",
                "no:cacheprovider",
                "tests",
            ],
            cwd=root,
            env=env,
            check=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="the built .tar.gz source distribution")
    args = parser.parse_args()
    check_sdist(args.source)


if __name__ == "__main__":
    main()
