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
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from functools import lru_cache
from types import ModuleType
from typing import Any

CODE_RE = re.compile(r"[^:]+:[^:]+")  # proj v0.1: an authority and a code, one colon between
_CODE_TEXT_RE = re.compile(r"[A-Za-z][\w.-]*:[^\s:\[\]{}]+")  # `EPSG:32631`, never WKT or JSON
_EPSG_RE = re.compile(r"EPSG:(\d+)")
_WKT2_RE = re.compile(r"\s*[A-Z]*CRS\s*\[")  # WKT2 roots end in CRS (PROJCRS); WKT1's in CS
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

    @classmethod
    def from_string(cls, text: str) -> Crs:
        """A CRS given as text: an authority code or WKT2 as they are, anything else via pyproj.

        WKT1, PROJJSON and PROJ strings need pyproj to become WKT2. A code is kept as written
        (authority in capitals) and a WKT2 gets its EPSG code when pyproj identifies one.
        """
        stripped = text.strip()
        if is_code_text(stripped):
            authority, _, number = stripped.partition(":")
            return cls(code=f"{authority.upper()}:{number}")
        pyproj = _pyproj()
        if _WKT2_RE.match(stripped) and pyproj is None:
            return cls(wkt2=stripped)
        if pyproj is None:
            raise ValueError(
                f"cannot read the CRS {_clipped(stripped)}: pass an authority code such as "
                "EPSG:32631 or WKT2 text, or install pyproj to read other forms"
            )
        try:
            parsed = pyproj.CRS.from_user_input(stripped)
        except _crs_error() as exc:
            raise ValueError(
                f"cannot read the CRS {_clipped(stripped)}: {exc}. Pass an authority code such "
                "as EPSG:32631 or WKT2 text"
            ) from exc
        epsg = parsed.to_epsg()
        wkt2 = stripped if _WKT2_RE.match(stripped) else parsed.to_wkt("WKT2_2019")
        return cls(code=None if epsg is None else f"EPSG:{epsg}", wkt2=wkt2)

    @property
    def epsg(self) -> int | None:
        """The EPSG number when the code is an EPSG one."""
        matched = None if self.code is None else _EPSG_RE.fullmatch(self.code)
        return None if matched is None else int(matched.group(1))

    def with_wkt2(self) -> Crs:
        """This CRS with the WKT2 of its code added, when it has none and pyproj can supply it.

        A code PROJ does not know, or no pyproj, leaves the CRS as it is: the code still names it.
        """
        pyproj = _pyproj()
        if self.wkt2 is not None or self.code is None or pyproj is None:
            return self
        try:
            known = (
                pyproj.CRS.from_epsg(self.epsg)
                if self.epsg is not None
                else pyproj.CRS.from_user_input(self.code)
            )
            return replace(self, wkt2=known.to_wkt())  # pyproj's default is WKT2:2019
        except _crs_error():
            return self

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


def is_code_text(text: str) -> bool:
    """Whether `text` is an authority code such as `EPSG:32631`, not WKT, JSON or a PROJ string."""
    return _CODE_TEXT_RE.fullmatch(text) is not None


def _clipped(text: str, limit: int = 60) -> str:
    one_line = " ".join(text.split())
    return repr(one_line if len(one_line) <= limit else one_line[: limit - 3] + "...")


def _pyproj() -> ModuleType | None:
    """pyproj if it is installed. Core does not depend on it."""
    try:
        return importlib.import_module("pyproj")
    except ImportError:
        return None


def wkt2_from_cf(attributes: Mapping[str, Any], name: str) -> str:
    """The WKT2 of a CF grid mapping variable's attributes, which name `name` in their file.

    A CF grid mapping holds projection parameters, not a CRS definition, so reading it needs
    pyproj.
    """
    pyproj = _pyproj()
    if pyproj is None:
        raise ValueError(
            f"the CRS is the CF grid mapping {name!r}, which needs pyproj to read: install "
            "pyproj, or pass --crs with an EPSG code or WKT2 text"
        )
    try:
        return pyproj.CRS.from_cf(dict(attributes)).to_wkt()  # pyproj's default is WKT2:2019
    except _crs_error() as exc:
        raise ValueError(f"cannot read the CF grid mapping {name!r}: {exc}") from exc


def _crs_error() -> type[Exception]:
    """pyproj's exception for a CRS it cannot read; only called once pyproj is known present."""
    return importlib.import_module("pyproj.exceptions").CRSError


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
    except _crs_error():
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
