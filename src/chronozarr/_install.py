"""How to add an optional extra, for messages that name a missing dependency."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version


def _pin() -> str:
    """`==X.Y.Z` for the installed chronozarr, as the install commands in the docs write it.

    Without the pin, reinstalling with an extra can move chronozarr to another release. Empty when
    chronozarr runs from a source tree that is not installed.
    """
    try:
        return f"=={version('chronozarr')}"
    except PackageNotFoundError:
        return ""


def extra_hint(extra: str) -> str:
    """The commands that add `extra`, one for each way of installing chronozarr that the docs show.

    `uv sync --extra NAME` works only in a checkout of the repository: an installed package has no
    project to sync, so the installed routes come first.
    """
    example = "geo" if extra == "geo" else f"geo,{extra}"
    pin = _pin()
    return (
        f"install the {extra} extra: `uv tool install --python 3.13 'chronozarr[{example}]{pin}'` "
        f"for the command line (list every extra you use), `uv add 'chronozarr[{extra}]{pin}'` in "
        f"a uv project, `pip install 'chronozarr[{extra}]{pin}'` with pip, or "
        f"`uv sync --extra {extra}` in a checkout of the repository"
    )
