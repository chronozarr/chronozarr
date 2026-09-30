"""Export decoded timesteps of a store as Cloud Optimized GeoTIFFs for GDAL and QGIS.

Each file holds every band of one timestep at one pyramid level, with star-delta encoding undone,
so the values are the stored true values in the store's dtype. Band names, nodata, scale, offset
and units become GeoTIFF band metadata. A store's `mask` and `coverage` variables are not
exported. Needs rasterio (`chronozarr[geo]`).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np

from chronozarr.decode import ChronoStore, open_store

_DATE_PREFIX = re.compile(r"^\d{4}-\d{2}(-\d{2}([T ][\d:.]*Z?)?)?$")


def select_times(spec: Sequence[str], times: Sequence[str]) -> list[int]:
    """Timestep indices picked by `spec`; all of them when `spec` is empty.

    Tokens (comma separated inside one item, or several items) are any of: `all`; an integer
    index (negative counts from the end); an index slice `start:stop[:step]` (stop exclusive);
    an ISO date prefix of at least year and month, `2024-03` or `2024-03-15`, selecting every
    timestep inside that period; or an inclusive range of such prefixes, `2020-01..2022-06`.
    `times` are the store's ISO-8601 timestamps. Raises ValueError naming the bad token.
    """
    n = len(times)
    tokens = [t.strip() for item in spec for t in item.split(",") if t.strip()]
    if not tokens:
        return list(range(n))
    chosen: set[int] = set()
    for token in tokens:
        if token == "all":
            chosen.update(range(n))
        elif ".." in token:
            start, _, end = token.partition("..")
            if not (_DATE_PREFIX.match(start) and _DATE_PREFIX.match(end)):
                raise ValueError(
                    f"bad time range '{token}': use ISO date prefixes like 2020-01..2022-06"
                )
            chosen.update(
                i for i, t in enumerate(times) if t[: len(start)] >= start and t[: len(end)] <= end
            )
        elif ":" in token:
            try:
                parts = [int(p) if p else None for p in token.split(":")]
                chosen.update(range(n)[slice(*parts)])
            except (ValueError, TypeError) as exc:
                raise ValueError(
                    f"bad index slice '{token}': use start:stop[:step] with integers"
                ) from exc
        elif re.fullmatch(r"-?\d+", token):
            index = int(token)
            if not -n <= index < n:
                raise ValueError(f"time index {index} out of range: the store has {n} timesteps")
            chosen.add(index % n)
        elif _DATE_PREFIX.match(token):
            matches = [i for i, t in enumerate(times) if t.startswith(token.replace(" ", "T"))]
            if not matches:
                raise ValueError(
                    f"no timestep matches '{token}' (store spans {times[0]} to {times[-1]})"
                )
            chosen.update(matches)
        else:
            raise ValueError(
                f"cannot parse time selector '{token}': use an index, start:stop[:step], "
                "an ISO date prefix such as 2024-03, a range A..B, or all"
            )
    if not chosen:
        raise ValueError(f"no timesteps selected by {list(spec)}")
    return sorted(chosen)


def _file_stem(iso: str) -> str:
    """`2024-03-01` for midnight timestamps, `2024-03-01T120000Z` otherwise."""
    date, _, clock = iso.partition("T")
    if not clock or clock.rstrip("Z").replace(":", "").strip("0.") == "":
        return date
    return f"{date}T{clock.rstrip('Z').replace(':', '')}Z"


def _band_metadata(store: ChronoStore) -> list[tuple[str, float, float, str | None]]:
    """(name, scale, offset, units) per band, for v0.1 string bands and v0.2 band objects."""
    out = []
    for band in store.attrs.bands:
        if isinstance(band, str):
            out.append((band, 1.0, 0.0, None))
        else:
            out.append(
                (
                    str(band.name),
                    float(getattr(band, "scale", None) or 1.0),
                    float(getattr(band, "offset", None) or 0.0),
                    getattr(band, "units", None),
                )
            )
    return out


def export_cog(
    store: ChronoStore | str | Path,
    out_dir: str | Path,
    *,
    level: int = 0,
    times: Sequence[int] | None = None,
) -> list[Path]:
    """Write one COG per timestep into `out_dir` and return the paths, in time order.

    `store` is an opened store, a local path or an https URL. Files are named
    `L<level>_<date>.tif`. One timestep of the level is held in memory at a time, so pick a
    coarser `level` for very large stores. An existing file of the same name is an error.
    """
    import rasterio
    import rasterio.shutil  # ty: ignore[unresolved-import]  # compiled module, no stub
    from rasterio.io import MemoryFile
    from rasterio.transform import Affine

    opened = store if isinstance(store, ChronoStore) else open_store(store)
    if not 0 <= level < len(opened.levels):
        raise ValueError(
            f"level {level} out of range: store has levels 0..{len(opened.levels) - 1}"
        )
    selected = list(range(len(opened.times))) if times is None else [int(t) for t in times]
    lod = opened.levels[level]
    bands = _band_metadata(opened)
    iso_times = opened.attrs.times
    out = Path(out_dir)
    targets = [out / f"L{level}_{_file_stem(iso_times[t])}.tif" for t in selected]
    clashes = [p for p in targets if p.exists()]
    if clashes:
        raise FileExistsError(
            f"{len(clashes)} output file(s) already exist, for example {clashes[0]}; "
            "choose an empty out_dir"
        )
    out.mkdir(parents=True, exist_ok=True)

    nodata: Any = opened.attrs.nodata
    for t, target in zip(selected, targets, strict=True):
        values = np.asarray(opened.read(t, level))
        n_band, height, width = values.shape
        with MemoryFile() as memory:
            with memory.open(
                driver="GTiff",
                width=width,
                height=height,
                count=n_band,
                dtype=values.dtype,
                crs=opened.attrs.crs,
                transform=Affine(*lod.transform),
                nodata=nodata,
                tiled=True,
                blockxsize=512,
                blockysize=512,
            ) as dst:
                dst.write(values)
                dst.descriptions = [name for name, *_ in bands]
                dst.scales = [scale for _, scale, _, _ in bands]
                dst.offsets = [offset for _, _, offset, _ in bands]
                if any(units for *_, units in bands):
                    dst.units = [units or "" for *_, units in bands]
                dst.update_tags(
                    CHRONOZARR_TIME=iso_times[t],
                    CHRONOZARR_LEVEL=str(level),
                    CHRONOZARR_TIMESTEP=str(t),
                )
            with memory.open() as src:
                rasterio.shutil.copy(
                    src,
                    target,
                    driver="COG",
                    compress="DEFLATE",
                    predictor="YES",
                    blocksize=512,
                    overview_resampling="AVERAGE",
                )
    return targets
