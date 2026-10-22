"""How to add an optional extra, for messages that name a missing dependency."""

from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError, version

REPOSITORY = "git+https://github.com/chronozarr/chronozarr"


def _installed_version() -> str | None:
    """The installed chronozarr's version, or None when it runs from a source tree that is not
    installed."""
    try:
        return version("chronozarr")
    except PackageNotFoundError:
        return None


def _is_development(installed: str) -> bool:
    """True for a PEP 440 development release (`0.5.0.dev0`) or a local build (`0.5.0+g1a2b3c4`).

    The version on main is the next release's `.dev0`: no index has it.
    """
    return re.search(r"\.dev\d+|\+", installed) is not None


def extra_hint(extra: str) -> str:
    """The commands that add `extra`, one for each way of installing chronozarr that the docs show.

    `uv sync --extra NAME` works only in a checkout of the repository: an installed package has no
    project to sync, so the installed routes come first.

    A release is pinned as the docs pin it: without the pin, reinstalling with an extra can move
    chronozarr to another release. A development version is never pinned, since no index has it
    and the latest release can lack the extra or the fixes of the code that runs: its routes
    install from the repository.
    """
    example = "geo" if extra == "geo" else f"geo,{extra}"
    installed = _installed_version()
    if installed is not None and _is_development(installed):
        source = f" @ {REPOSITORY}"
        lead = (
            f"install the {extra} extra from the repository, because chronozarr {installed} is a "
            f"development version and a release from PyPI can lack the extra or the fixes in this "
            f"code: "
        )
    else:
        source = f"=={installed}" if installed is not None else ""
        lead = f"install the {extra} extra: "
    return (
        f"{lead}`uv tool install --python 3.13 'chronozarr[{example}]{source}'` "
        f"for the command line (list every extra you use), "
        f"`uv add 'chronozarr[{extra}]{source}'` in a uv project, "
        f"`pip install 'chronozarr[{extra}]{source}'` with pip, or "
        f"`uv sync --extra {extra}` in a checkout of the repository"
    )
