"""How to add an optional extra, for messages that name a missing dependency."""

from __future__ import annotations


def extra_hint(extra: str) -> str:
    """The commands that add `extra`, one for each way of installing chronozarr that the docs show.

    `uv sync --extra NAME` works only in a checkout of the repository: an installed package has no
    project to sync, so the installed routes come first.
    """
    example = "geo" if extra == "geo" else f"geo,{extra}"
    return (
        f"install the {extra} extra: `uv tool install --python 3.13 'chronozarr[{example}]'` for "
        f"the command line (list every extra you use), `uv add 'chronozarr[{extra}]'` in a uv "
        f"project, `pip install 'chronozarr[{extra}]'` with pip, or `uv sync --extra {extra}` in "
        "a checkout of the repository"
    )
