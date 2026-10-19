"""The CRS of a store: the representations proj v0.1 allows, and how to compare them.

A store names its CRS by an authority code (`proj:code`), WKT2 text (`proj:wkt2`) or PROJJSON
(`proj:projjson`). `Crs` holds whichever are present. When they disagree proj v0.1 takes
`proj:wkt2`; `Crs.resolved()` applies that, ranking wkt2 above projjson above code, and
`Crs.disagreements()` says what contradicts what so that `validate` can report it.

Equality needs no pyproj. Two CRSs are the same when their codes are equal or their WKT2 text is
identical outside quoted names after whitespace is removed. Anything else needs pyproj, which is
used when it is installed; `CrsMatch.method` says which test decided. Without pyproj a code and
a WKT2 cannot be compared and the answer is "unavailable".
"""

from __future__ import annotations

import importlib
import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from types import ModuleType
from typing import Any

CODE_RE = re.compile(r"[^:]+:[^:]+")  # proj v0.1: an authority and a code, one colon between
_WKT_NAME_RE = re.compile(r'\s*[A-Za-z_]+\[\s*"((?:[^"]|"")*)"')
_BETWEEN_QUOTES_RE = re.compile(r'("(?:[^"]|"")*")|\s+')


@dataclass(frozen=True)
class Crs:
    """A CRS as stored: any of an authority code, WKT2 text and a PROJJSON object."""

    code: str | None = None
    wkt2: str | None = None
    projjson: dict[str, Any] | None = field(default=None, hash=False)

    def __post_init__(self) -> None:
        if self.code is None and self.wkt2 is None and self.projjson is None:
            raise ValueError("a Crs needs a code, wkt2 or projjson")

    def _ranked(self) -> list[tuple[str, Crs]]:
        """Each stored representation on its own, highest precedence first."""
        ranked: list[tuple[str, Crs]] = []
        if self.wkt2 is not None:
            ranked.append(("proj:wkt2", Crs(wkt2=self.wkt2)))
        if self.projjson is not None:
            ranked.append(("proj:projjson", Crs(projjson=self.projjson)))
        if self.code is not None:
            ranked.append(("proj:code", Crs(code=self.code)))
        return ranked

    def _contradictions(self) -> list[tuple[str, str, CrsMatch]]:
        """(key, top key, comparison) for each representation shown to differ from the top."""
        (top_key, top), *lower = self._ranked()
        found = []
        for key, other in lower:
            match = same_crs(top, other)
            if match.same is False:
                found.append((key, top_key, match))
        return found

    def disagreements(self) -> list[str]:
        """One sentence per representation that is shown to describe a different CRS."""
        return [
            f"{self._label(key)} and {top_key} describe different CRSs "
            f"(compared by {match.method}); {top_key} is used"
            for key, top_key, match in self._contradictions()
        ]

    def _label(self, key: str) -> str:
        return f"{key} {self.code!r}" if key == "proj:code" else key

    def resolved(self) -> Crs:
        """This CRS without any representation shown to contradict a higher-ranked one."""
        dropped = {key for key, _, _ in self._contradictions()}
        return Crs(
            code=None if "proj:code" in dropped else self.code,
            wkt2=None if "proj:wkt2" in dropped else self.wkt2,
            projjson=None if "proj:projjson" in dropped else self.projjson,
        )

    def to_string(self) -> str:
        """A string pyproj and rasterio read as a CRS: the code, else the WKT2, else JSON."""
        resolved = self.resolved()
        if resolved.code is not None:
            return resolved.code
        if resolved.wkt2 is not None:
            return resolved.wkt2
        return json.dumps(resolved.projjson, separators=(",", ":"), sort_keys=True)

    def describe(self) -> str:
        """A short name for messages: the code, or the kind and name of the definition."""
        resolved = self.resolved()
        if resolved.code is not None:
            return resolved.code
        if resolved.wkt2 is not None:
            named = _WKT_NAME_RE.match(resolved.wkt2)
            return f'WKT2 "{named.group(1)}"' if named else "WKT2"
        name = (resolved.projjson or {}).get("name")
        return f'PROJJSON "{name}"' if isinstance(name, str) else "PROJJSON"


@dataclass(frozen=True)
class CrsMatch:
    """The answer to "are these the same CRS?": same, or None when it could not be told, and how.

    `method` is "code", "wkt2 text", "pyproj" or "unavailable".
    """

    same: bool | None
    method: str


def _pyproj() -> ModuleType | None:
    """pyproj if it is installed. Core does not depend on it."""
    try:
        return importlib.import_module("pyproj")
    except ImportError:
        return None


def _text_of(crs: Crs) -> str:
    """The representation pyproj reads: WKT2, else PROJJSON, else the code."""
    if crs.wkt2 is not None:
        return crs.wkt2
    if crs.projjson is not None:
        return json.dumps(crs.projjson)
    if crs.code is not None:
        return crs.code
    raise ValueError("a Crs holds at least one representation")


def _squash(wkt2: str) -> str:
    """WKT2 without whitespace, keeping the names in quotes as written."""
    return _BETWEEN_QUOTES_RE.sub(lambda m: m.group(1) or "", wkt2)


@lru_cache(maxsize=64)
def _pyproj_same(first: str, second: str) -> bool | None:
    """pyproj's verdict on two CRS definitions (names are ignored), None if it cannot read one."""
    pyproj = _pyproj()
    if pyproj is None:
        return None
    try:
        return bool(pyproj.CRS.from_user_input(first) == pyproj.CRS.from_user_input(second))
    except pyproj.exceptions.CRSError:
        return None


def same_crs(a: Crs, b: Crs) -> CrsMatch:
    """Compare two CRSs by code, then by WKT2 text, then (if installed) by pyproj."""
    if a.code is not None and b.code is not None and a.code.upper() == b.code.upper():
        return CrsMatch(True, "code")
    if a.wkt2 is not None and b.wkt2 is not None and _squash(a.wkt2) == _squash(b.wkt2):
        return CrsMatch(True, "wkt2 text")
    if _pyproj() is not None:
        verdict = _pyproj_same(_text_of(a), _text_of(b))
        if verdict is not None:
            return CrsMatch(verdict, "pyproj")
    if a.code is not None and b.code is not None:
        return CrsMatch(False, "code")
    return CrsMatch(None, "unavailable")
