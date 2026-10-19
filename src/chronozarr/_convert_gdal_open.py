"""Opening one source file of a raster series with rasterio.

A source is a file GDAL reads, the one raster inside a zip, or one subdataset of a container file
(HDF5, HDF-EOS, netCDF) chosen with `--variable`. `opened` yields the dataset whichever it is, and
the name GDAL opened, which is what later reads of the file open again.
"""

from __future__ import annotations

import re
import textwrap
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chronozarr._convert_discover import ZIP_SUFFIX, zip_raster

_HDF4_SIGNATURE = b"\x0e\x03\x13\x01"
HDF4_DETAIL = (
    "cannot open source: it is an HDF4 file and this GDAL build has no HDF4 driver (the GDAL "
    "inside the rasterio wheel has none)"
)
HDF4_FIX = (
    "write GeoTIFFs with gdal_translate from a GDAL that has the HDF4 driver (check with "
    "`gdalinfo --formats | grep HDF4`; conda-forge's libgdal-hdf4 adds it; `gdalinfo FILE` lists "
    "the subdatasets to translate), then convert the GeoTIFFs"
)


class SubdatasetChoiceError(ValueError):
    """`--variable` does not name exactly one subdataset of the files, or names one in files that
    have none. It concerns the series, not one file, so it is raised, not collected per file."""


@dataclass(frozen=True)
class Subdataset:
    name: str  # what GDAL opens, for example HDF5:"file.h5"://Data/soil
    tail: str  # the part after the file, for example Data/soil
    shape: str  # GDAL's "[406x964]", or "" when it gives none

    @property
    def group(self) -> str:
        """The tail up to and including its last "/" or ":", "" for a name without a group."""
        return self.tail[: max(self.tail.rfind("/"), self.tail.rfind(":")) + 1]

    @property
    def leaf(self) -> str:
        return self.tail[len(self.group) :]


def hdf4_without_driver(uri: str) -> bool:
    """Whether the local file `uri` is HDF4 and this GDAL has no driver for it."""
    import rasterio

    if "://" in uri or uri.startswith("/vsi"):
        return False
    try:
        with open(uri, "rb") as handle:
            if handle.read(len(_HDF4_SIGNATURE)) != _HDF4_SIGNATURE:
                return False
    except OSError:
        return False
    with rasterio.Env() as env:
        return "HDF4" not in env.drivers()


def dataset_name(uri: str) -> str:
    """What GDAL opens for the source `uri`: the one raster inside a zip, else `uri` itself.

    A zip is opened through GDAL's /vsizip/, so the raster and the sidecars next to it are read
    in place and nothing is unpacked. Raises ValueError when the zip cannot be used (see
    `zip_raster`).
    """
    if not uri.lower().endswith(ZIP_SUFFIX):
        return uri
    return f"/vsizip/{Path(uri).resolve()}/{zip_raster(uri)}"


def _subdataset_tail(subdataset_name: str, opened_name: str) -> str:
    """The part of a subdataset name after the file it is in.

    GDAL names them `DRIVER:"path":tail` (`HDF5:"f.h5"://Data/soil`, `NETCDF:"f.nc":sst`,
    `HDF4_EOS:EOS_GRID:"f.hdf":Grid:NDVI`), with the path as it was opened.
    """
    at = subdataset_name.find(opened_name)
    if at >= 0:
        rest = subdataset_name[at + len(opened_name) :]
    else:
        rest = subdataset_name.rpartition('":')[2]
    return rest.removeprefix('"').removeprefix(":").lstrip("/")


def _subdatasets(src: Any, opened_name: str) -> list[Subdataset]:
    tags = src.tags(ns="SUBDATASETS")
    found = []
    number = 1
    while (name := tags.get(f"SUBDATASET_{number}_NAME")) is not None:
        shape = re.match(r"\[[^\]]*\]", tags.get(f"SUBDATASET_{number}_DESC", ""))
        found.append(
            Subdataset(name, _subdataset_tail(name, opened_name), shape.group(0) if shape else "")
        )
        number += 1
    return found


def _listing(subdatasets: Sequence[Subdataset]) -> str:
    """The subdatasets by group and shape: the group, then the names in it."""
    groups: dict[tuple[str, str], list[str]] = {}
    for sub in subdatasets:
        groups.setdefault((sub.group, sub.shape), []).append(sub.leaf)
    lines = []
    for (group, shape), leaves in groups.items():
        lines.append(f"  {group or '(no group)'} {shape}".rstrip())
        lines.append(
            textwrap.fill(
                ", ".join(leaves), width=100, initial_indent="    ", subsequent_indent="    "
            )
        )
    return "\n".join(lines)


def _plural(n: int) -> str:
    return f"{n} subdataset{'s' if n != 1 else ''}"


def _choose(subdatasets: list[Subdataset], variable: str | None, uri: str) -> Subdataset:
    """The subdataset `variable` names: its tail (`Data/soil`), or its last part (`soil`) when only
    one subdataset ends that way. A leading "/" is ignored."""
    if variable is None:
        raise SubdatasetChoiceError(
            f"{uri} has no raster band: it holds {_plural(len(subdatasets))}. Choose one with "
            "--variable NAME, where NAME is a group below followed by one of the names in it, or "
            f"the name alone when only one subdataset has it:\n{_listing(subdatasets)}"
        )
    wanted = variable.strip().lstrip("/")
    matches = [s for s in subdatasets if s.tail == wanted] or [
        s for s in subdatasets if s.leaf == wanted
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SubdatasetChoiceError(
            f"--variable {variable!r} matches none of the {_plural(len(subdatasets))} of {uri}. "
            f"Choose one:\n{_listing(subdatasets)}"
        )
    raise SubdatasetChoiceError(
        f"--variable {variable!r} matches {len(matches)} subdatasets of {uri}: "
        f"{', '.join(s.tail for s in matches)}. Name one in full, with its group"
    )


@contextmanager
def opened(
    uri: str, variable: str | None, env_for: Callable[[str], dict[str, str]]
) -> Iterator[tuple[Any, str, str | None]]:
    """Open the source `uri`; yields (dataset, name GDAL opened, name of the subdataset or None).

    `env_for` gives the GDAL options for opening a dataset name. A file with subdatasets and no
    band needs `variable`; `variable` for a file without subdatasets is refused, so a name that
    does not apply is never ignored. Both raise `SubdatasetChoiceError`.
    """
    import rasterio

    name = dataset_name(uri)
    with rasterio.Env(**env_for(name)), rasterio.open(name) as top:
        subdatasets = _subdatasets(top, name)
        if not subdatasets and variable is not None:
            raise SubdatasetChoiceError(
                f"{uri} has no subdatasets, so --variable {variable!r} does not apply to it; "
                "leave --variable out"
            )
        if not subdatasets or (variable is None and top.count > 0):
            yield top, name, None
            return
        chosen = _choose(subdatasets, variable, uri)
        with rasterio.open(chosen.name) as sub:
            yield sub, chosen.name, chosen.leaf
