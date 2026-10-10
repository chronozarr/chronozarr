"""Band roles: which bands answer to red, green, blue and nir, and which viewer products follow.

A store names its bands freely. The viewer picks the bands of a product (True color, NDVI, ...) by
role, using the band's `common_name` (the STAC `eo:bands` vocabulary, spec 4.4). A band without a
`common_name` also answers to a role when its name is the role ("red") or the Sentinel-2 name for
it ("B04"). Nothing else is guessed. `assign_roles` and `set_band_roles` write `common_name`, the
existing spec field, so the assignment travels with the store. The tables below mirror
js/shared/products.js; tests/test_bands.py fails when they drift.
"""

from __future__ import annotations

import json
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

import zarr
from zarr.errors import ZarrUserWarning

from chronozarr import schema
from chronozarr.schema import Band

# STAC eo extension common names for bands. A store may carry any of them.
STAC_COMMON_NAMES = (
    "coastal",
    "blue",
    "green",
    "yellow",
    "red",
    "rededge",
    "nir",
    "nir08",
    "nir09",
    "cirrus",
    "swir16",
    "swir22",
    "lwir",
    "lwir11",
    "lwir12",
    "pan",
)

# Roles the viewer's products ask for, and the Sentinel-2 band name that stands in for each.
PRODUCT_ROLES = ("red", "green", "blue", "nir")
SENTINEL2_NAMES = {"blue": "B02", "green": "B03", "red": "B04", "nir": "B08"}

# (id, display name, roles it needs), in viewer order. The single-band product needs none.
PRODUCTS = (
    ("true_color", "True color", ("red", "green", "blue")),
    ("false_color", "False color", ("nir", "red", "green")),
    ("ndvi", "NDVI", ("nir", "red")),
    ("ndwi", "NDWI", ("green", "nir")),
    ("water", "Water", ("green", "nir")),
    ("band", "Single band", ()),
)

CLEAR = "none"  # in a role assignment: remove the band's common_name


@dataclass(frozen=True)
class RoleResolution:
    """The bands that answer to one role, and how they do."""

    role: str
    bands: tuple[str, ...]  # names; empty when nothing answers
    source: str | None  # "common_name", "name" or "Sentinel-2 name"

    @property
    def ambiguous(self) -> bool:
        return len(self.bands) > 1


@dataclass(frozen=True)
class ProductStatus:
    id: str
    name: str
    available: bool
    missing: tuple[str, ...]  # roles no band answers to


def resolve_role(bands: Sequence[Band], role: str) -> RoleResolution:
    """The bands that answer to `role`, in the order the viewer tries: a declared `common_name`,
    else a band with no `common_name` named like the role, else one named like its Sentinel-2
    band. The first rule that matches anything decides.
    """
    sentinel2 = SENTINEL2_NAMES.get(role)
    rules = (
        ("common_name", lambda b: b.common_name == role),
        ("name", lambda b: b.common_name is None and b.name.lower() == role),
        (
            "Sentinel-2 name",
            lambda b: sentinel2 is not None and b.common_name is None and b.name == sentinel2,
        ),
    )
    for source, matches in rules:
        found = tuple(b.name for b in bands if matches(b))
        if found:
            return RoleResolution(role, found, source)
    return RoleResolution(role, (), None)


def resolve_roles(bands: Sequence[Band]) -> dict[str, RoleResolution]:
    return {role: resolve_role(bands, role) for role in PRODUCT_ROLES}


def product_status(bands: Sequence[Band]) -> list[ProductStatus]:
    """Which viewer products these bands allow, as the viewer decides it."""
    resolved = resolve_roles(bands)
    statuses = []
    for product_id, name, needs in PRODUCTS:
        missing = tuple(role for role in needs if not resolved[role].bands)
        statuses.append(ProductStatus(product_id, name, not missing, missing))
    return statuses


def parse_roles(texts: Sequence[str]) -> dict[str, str]:
    """`NAME=ROLE` assignments from repeated and/or comma-separated flag values.

    `ROLE` is a STAC eo common name, or `none` to remove the band's common name. Band names
    that contain `=` or `,` cannot be given this way.
    """
    roles: dict[str, str] = {}
    for text in texts:
        for item in text.split(","):
            item = item.strip()
            if not item:
                continue
            name, separator, role = item.partition("=")
            name, role = name.strip(), role.strip()
            if not separator or not name or not role:
                raise ValueError(f"band role {item!r} must look like NAME=ROLE, e.g. B04=red")
            if name in roles:
                raise ValueError(f"band {name!r} is given two roles: {roles[name]!r} and {role!r}")
            roles[name] = role
    return roles


def assign_roles(bands: Sequence[Band], roles: Mapping[str, str | None]) -> tuple[Band, ...]:
    """`bands` with `common_name` set (or cleared, for `none` or None) as `roles` says.

    Raises ValueError for a band the store does not have, a role outside the STAC eo
    vocabulary, or an assignment that leaves two bands answering to the same product role.
    """
    names = [b.name for b in bands]
    unknown = [name for name in roles if name not in names]
    if unknown:
        raise ValueError(f"no band named {', '.join(map(repr, unknown))}; bands are: {names}")
    for name, role in roles.items():
        if role is not None and role != CLEAR and role not in STAC_COMMON_NAMES:
            raise ValueError(
                f"band {name!r}: {role!r} is not a STAC eo common name. Use one of "
                f"{', '.join(STAC_COMMON_NAMES)}, or '{CLEAR}' to remove the band's common name"
            )
    updated = tuple(
        replace(b, common_name=None if roles[b.name] in (None, CLEAR) else roles[b.name])
        if b.name in roles
        else b
        for b in bands
    )
    for resolution in resolve_roles(updated).values():
        if resolution.ambiguous:
            raise ValueError(
                f"after this assignment {', '.join(map(repr, resolution.bands))} would all be "
                f"{resolution.role} (by {resolution.source}); give {resolution.role} to one "
                f"band and a different role, or '{CLEAR}', to the others"
            )
    return updated


def set_band_roles(store: str | Path, roles: Mapping[str, str | None]) -> tuple[Band, ...]:
    """Set the `common_name` of bands of the local store `store` in place; returns its bands.

    Only the root `zarr.json` changes: the bands attribute, and the consolidated metadata when
    the store has it (rewritten, as the stored copy of every child would otherwise be stale).
    Data, scale, offset and units are untouched. Nothing is written when the store does not
    validate or the assignment is refused.
    """
    path = Path(store)
    if not path.is_dir():
        raise ValueError(f"{store} is not a local directory; set roles on a local copy")
    problems = schema.validate(path)
    if problems:
        shown = "\n  ".join(problems[:5])
        raise ValueError(f"{store} does not validate, so nothing was changed:\n  {shown}")
    root = zarr.open_group(path, mode="r+", zarr_format=3, use_consolidated=False)
    parsed = schema.parse_root_attrs(root.attrs.asdict())
    bands = assign_roles(parsed.chronozarr.bands, roles)
    if bands == parsed.chronozarr.bands:
        return bands
    document = json.loads((path / "zarr.json").read_text())
    had_consolidated = document.get("consolidated_metadata") is not None
    root.attrs.update({"chronozarr": replace(parsed.chronozarr, bands=bands).to_attrs()})
    if had_consolidated:
        with warnings.catch_warnings():
            # Consolidated metadata is deliberate (spec 3.1); see encode._write_store.
            warnings.simplefilter("ignore", ZarrUserWarning)
            zarr.consolidate_metadata(str(path))
    return bands


# Largest stored value of the dtypes the viewer treats as reflectance-like. A band whose largest
# possible physical value is at most REFLECTANCE_CEILING gets the fixed reflectance tone mapping;
# any other single band has adjustable display limits.
DTYPE_MAX = {"uint8": 255, "uint16": 65535}
REFLECTANCE_CEILING = 10


def display_limits_apply(dtype: str, band: Band) -> bool:
    """Whether the viewer lets display limits (`r=`) set the single-band view of `band`."""
    largest = DTYPE_MAX.get(dtype)
    if largest is None:
        return True
    physical = largest * (1.0 if band.scale is None else band.scale) + (band.offset or 0.0)
    return physical > REFLECTANCE_CEILING
