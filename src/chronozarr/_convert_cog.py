"""COG and georeferenced image frame conversion adapter."""

from __future__ import annotations

import math
import warnings
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import wraps
from typing import Any, ParamSpec, TypeVar

import numpy as np

from chronozarr import schema
from chronozarr._convert_manifest import Entry
from chronozarr._convert_source import (
    Bounds,
    Grid,
    Source,
    SourceInfo,
    Step,
    _bounds_grid,
    _check_north_up,
    _decide_validity,
    _declared_nodata,
    _epsg,
    _grid_difference,
    _same_grid,
    _scaling,
    _settle_nan,
    _value_valid,
)
from chronozarr.schema import Band, Transform

GDAL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "GDAL_HTTP_MAX_RETRY": "3",
    "GDAL_HTTP_RETRY_DELAY": "1",
}


# Formats whose georeferencing lives in sidecar files (world file, .aux.xml); GDAL finds them only
# by probing for each candidate name, which GDAL_ENV switches off for everything else.
SIDECAR_SUFFIXES = (".png",)


RESAMPLING_METHODS = ("nearest", "bilinear", "cubic", "average", "mode", "min", "max", "med")


_P = ParamSpec("_P")


_R = TypeVar("_R")


def _frame_grid(src: Any, crs: str | None, bounds: Bounds | None) -> Grid:
    """The grid of one opened frame: its own CRS and geotransform, `crs` standing in for a
    missing CRS, or the grid `bounds` gives a frame that has no geotransform."""
    from rasterio.crs import CRS  # ty: ignore[unresolved-import]  # compiled, no stubs

    located = not src.transform.is_identity  # GDAL answers the identity when it finds nothing
    given = None if crs is None else _epsg(CRS.from_user_input(crs), "--crs")
    declared = None if src.crs is None else _epsg(src.crs, "the frame")
    if bounds is not None:
        if given is None:
            raise ValueError("--bounds needs --crs, the CRS the bounds are in")
        if located:
            raise ValueError(
                "it carries its own geotransform (a world file or .aux.xml) and --bounds "
                "was given; drop --bounds, or remove the sidecar of the frames that have one"
            )
        if declared is not None and declared != given:
            raise ValueError(f"it declares {declared} but --crs is {given}")
        return _bounds_grid(bounds, given, src.height, src.width)
    if not located:
        raise ValueError(
            "it has no georeferencing (no geotransform, no world file next to it, no .aux.xml). "
            "Add a .pgw world file or a .aux.xml beside the frame, or pass --bounds "
            "west,south,east,north together with --crs"
        )
    frame_crs = declared or given
    if frame_crs is None:
        raise ValueError(
            "it has a geotransform but no CRS (a world file carries none). Pass --crs EPSG:xxxxx"
        )
    return Grid(
        frame_crs, _check_north_up(tuple(src.transform)[:6], "the frame"), src.height, src.width
    )


@dataclass(frozen=True)
class _CogHeader:
    driver: str  # GDAL driver short name: GTiff, PNG, ...
    grid: Grid
    own_grid: bool  # the file carries its own CRS and geotransform (else `grid` came from options)
    data_indexes: tuple[int, ...]  # 1-based indexes of the data bands, the alpha band excluded
    alpha: int | None  # 1-based index of the alpha band
    internal_mask: bool  # the data bands share an internal or per-dataset mask (no alpha)
    dtype: np.dtype
    nodata: tuple[float | int | str | None, ...]  # declared token per data band
    warp_nodata: float | int | None  # first data band's nodata, for the warper
    descriptions: tuple[str | None, ...]
    colors: tuple[str | None, ...]  # "red", "green" or "blue" for a band of that colour
    scales: tuple[float, ...]
    offsets: tuple[float, ...]
    units: tuple[str | None, ...]

    @property
    def count(self) -> int:
        return len(self.data_indexes)


def _gdal_env(uri: str) -> dict[str, str]:
    """GDAL options for opening `uri`: no directory listing or sidecar probes, which cost a
    request each over HTTP, except for formats that keep their georeferencing in sidecar files."""
    if uri.split("?", 1)[0].lower().endswith(SIDECAR_SUFFIXES):
        return {**GDAL_ENV, "GDAL_DISABLE_READDIR_ON_OPEN": "TRUE"}
    return GDAL_ENV


def _tolerate_unlocated_frames(func: Callable[_P, _R]) -> Callable[_P, _R]:
    """Run `func` without rasterio's warning for a source that has no geotransform.

    Such a frame is valid input when `bounds` locates it, and an error with its own message
    otherwise, so the warning (an error under `-W error`) adds nothing. The filter is set once, by
    the calling thread; the reader threads share it (a filter set per thread would race).
    """

    @wraps(func)
    def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        from rasterio.errors import NotGeoreferencedWarning

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", NotGeoreferencedWarning)
            return func(*args, **kwargs)

    return wrapper


_COLOR_NAMES = ("red", "green", "blue")


def _read_header(uri: str, crs: str | None, bounds: Bounds | None) -> _CogHeader:
    import rasterio
    from rasterio.enums import ColorInterp, MaskFlags

    try:
        with rasterio.Env(**_gdal_env(uri)), rasterio.open(uri) as src:
            if src.driver == "PNG" and ColorInterp.palette in src.colorinterp:
                raise ValueError(
                    "it is a palette (indexed colour) PNG, so its values are palette indices, "
                    "not colours. Expand it to RGB first, for example "
                    "`gdal_translate -expand rgba in.png out.png`, or save the frames as RGB"
                )
            grid = _frame_grid(src, crs, bounds)
            alphas = [i for i, c in enumerate(src.colorinterp, start=1) if c == ColorInterp.alpha]
            if len(alphas) > 1:
                raise ValueError(f"it has {len(alphas)} alpha bands; chronozarr reads one")
            alpha = alphas[0] if alphas else None
            indexes = tuple(i for i in range(1, src.count + 1) if i != alpha)
            if not indexes:
                raise ValueError("it has an alpha band and no data band")
            dtypes = {src.dtypes[i - 1] for i in indexes}
            if len(dtypes) > 1:
                raise ValueError(f"its data bands have different dtypes {sorted(dtypes)}")
            dtype = np.dtype(dtypes.pop())
            internal_mask = alpha is None and any(
                MaskFlags.per_dataset in src.mask_flag_enums[i - 1] for i in indexes
            )
            tokens = tuple(_declared_nodata(src.nodatavals[i - 1], dtype) for i in indexes)
            scaling = [
                _scaling(float(src.scales[i - 1]), float(src.offsets[i - 1]), f"band {i}")
                for i in indexes
            ]
            return _CogHeader(
                driver=src.driver,
                grid=grid,
                own_grid=src.crs is not None and not src.transform.is_identity,
                data_indexes=indexes,
                alpha=alpha,
                internal_mask=internal_mask,
                dtype=dtype,
                nodata=tokens,
                warp_nodata=math.nan if isinstance(tokens[0], str) else tokens[0],
                descriptions=tuple(src.descriptions[i - 1] or None for i in indexes),
                colors=tuple(
                    c if (c := src.colorinterp[i - 1].name) in _COLOR_NAMES else None
                    for i in indexes
                ),
                scales=tuple(s for s, _ in scaling),
                offsets=tuple(o for _, o in scaling),
                units=tuple(src.units[i - 1] or None for i in indexes),
            )
    except (rasterio.errors.RasterioIOError, ValueError) as exc:
        raise ValueError(f"cannot open source {uri}: {exc}") from exc


def _parse_resampling(name: str) -> Any:
    from rasterio.enums import Resampling

    if name not in RESAMPLING_METHODS:
        raise ValueError(f"unknown resampling {name!r}; choose one of {list(RESAMPLING_METHODS)}")
    return Resampling[name]


class CogManifestSource(Source):
    kind = "manifest of COGs"

    def __init__(
        self,
        entries: list[Entry],
        band_names: tuple[str, ...] | None,
        *,
        target_crs: str | None,
        target_transform: Transform | None,
        target_shape: tuple[int, int] | None,
        resampling: str | None,
        nodata: float | int | None | str,
        chunk_size: int,
        bounds: Bounds | None = None,
    ) -> None:
        self.entries = entries
        self.times = [e.time for e in entries]
        self.resampling = resampling
        self.chunk_size = chunk_size
        workers = min(8, len(entries))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            headers = list(pool.map(lambda e: _read_header(e.uri, target_crs, bounds), entries))
        first = headers[0]
        if all(h.driver == "PNG" for h in headers):
            self.kind = "manifest of PNG frames"
        if bounds is not None:
            self._check_same_size(headers)
        self.grid = self._target_grid(first, target_crs, target_transform, target_shape)
        self._check_consistent(headers, compare_labels=band_names is None)
        self._headers = headers
        if band_names is None:
            # a description names the band; else a colour band is named by its colour and says so
            labels = [
                (d, None) if d else (c, c) if c else (str(i + 1), None)
                for i, (d, c) in enumerate(zip(first.descriptions, first.colors, strict=True))
            ]
        else:
            labels = [(n, None) for n in band_names]
        names = tuple(name for name, _ in labels)
        common_names = tuple(common for _, common in labels)
        if len(names) != first.count:
            raise ValueError(
                f"{entries[0].uri} has {first.count} bands but {len(names)} band names were "
                f"given: {list(names)}"
            )
        if len(set(names)) != len(names):
            raise ValueError(f"band names must be unique, got {list(names)}")
        self.warped = frozenset(
            i for i, h in enumerate(headers) if not _same_grid(h.grid, self.grid)
        )
        if self.warped:
            if resampling is None:
                self._explain_mismatch(headers)
            assert resampling is not None
            _parse_resampling(resampling)
        explicit = []
        if any(h.alpha is not None for h in headers):
            explicit.append("alpha band")
        if any(h.internal_mask for h in headers):
            explicit.append("internal mask")
        validity = _decide_validity(
            first.dtype,
            {token for h in headers for token in h.nodata},
            override=nodata,
            explicit=explicit,
            footprint=bool(self.warped),
        )
        self._auto_nodata = nodata == "auto"
        # Values equal to an explicit --nodata are invalid; declared nodata is GDAL's to judge.
        self._sentinels: tuple[float | int, ...] = (
            () if self._auto_nodata or validity.nodata is None else (validity.nodata,)
        )
        self.info = SourceInfo(
            grid=self.grid,
            n_band=first.count,
            dtype=first.dtype,
            nodata=validity.nodata,
            band_names=names,
            bands=tuple(
                Band(
                    name=n,
                    common_name=common_names[i],
                    scale=first.scales[i],
                    offset=first.offsets[i],
                    units=first.units[i],
                )
                for i, n in enumerate(names)
            ),
            mask=validity.mask,
            validity=validity.why,
        )

    @staticmethod
    def _target_grid(
        first: _CogHeader,
        crs: str | None,
        transform: Transform | None,
        shape: tuple[int, int] | None,
    ) -> Grid:
        from rasterio.crs import CRS  # ty: ignore[unresolved-import]  # compiled, no stubs
        from rasterio.warp import calculate_default_transform

        if transform is not None:
            if shape is None or crs is None:
                raise ValueError("--transform needs --shape and --crs as well")
            return Grid(crs, _check_north_up(transform, "--transform"), shape[0], shape[1])
        if crs is None or CRS.from_user_input(crs) == CRS.from_user_input(first.grid.crs):
            if shape is not None:
                raise ValueError("--shape is only meaningful together with --transform")
            return first.grid
        if shape is not None:
            raise ValueError("--shape is only meaningful together with --transform")
        source = first.grid
        west = source.transform[2]
        north = source.transform[5]
        east = west + source.transform[0] * source.width
        south = north + source.transform[4] * source.height
        dst, width, height = calculate_default_transform(
            source.crs, crs, source.width, source.height, west, south, east, north
        )
        return Grid(
            _epsg(CRS.from_user_input(crs), "--crs"),
            _check_north_up(tuple(dst)[:6], "--crs"),
            height,
            width,
        )

    def _check_same_size(self, headers: list[_CogHeader]) -> None:
        """With `bounds` every frame covers the same extent, so a different size is a different
        pixel size: refuse it rather than guess."""
        first = headers[0].grid
        for entry, header in zip(self.entries, headers, strict=True):
            grid = header.grid
            if (grid.height, grid.width) != (first.height, first.width):
                raise ValueError(
                    f"{entry.uri} is {grid.height} x {grid.width} px; {self.entries[0].uri} is "
                    f"{first.height} x {first.width} px. With bounds every frame covers the "
                    "same extent, so all frames must have the same size"
                )

    def _check_consistent(self, headers: list[_CogHeader], *, compare_labels: bool) -> None:
        first = headers[0]
        first_uri = self.entries[0].uri
        for entry, header in zip(self.entries, headers, strict=True):
            if header.count != first.count:
                raise ValueError(
                    f"{entry.uri} has {header.count} bands; {first_uri} has {first.count}"
                )
            if header.dtype != first.dtype:
                raise ValueError(
                    f"{entry.uri} is {header.dtype}; {first_uri} is {first.dtype}. "
                    "All sources must share one dtype"
                )
            fields = [
                ("scales", header.scales, first.scales),
                ("offsets", header.offsets, first.offsets),
                ("units", header.units, first.units),
            ]
            if compare_labels:
                fields.append(("band descriptions", header.descriptions, first.descriptions))
                fields.append(("band colours", header.colors, first.colors))
            for what, found, wanted in fields:
                if found != wanted:
                    raise ValueError(
                        f"{entry.uri} has {what} {list(found)}; {first_uri} has {list(wanted)}. "
                        "A store holds one scale, offset and unit per band for every timestep, "
                        "so the sources must agree: rescale them to one first"
                    )
        if first.dtype.name not in schema.DTYPES:
            raise ValueError(
                f"sources are {first.dtype}; chronozarr stores hold {list(schema.DTYPES)}. "
                "Convert the sources first (for example with gdal_translate -ot)"
            )

    def _explain_mismatch(self, headers: list[_CogHeader]) -> None:
        offenders = [
            (self.entries[i].uri, _grid_difference(headers[i].grid, self.grid))
            for i in sorted(self.warped)
        ]
        shown = "; ".join(f"{uri}: {why}" for uri, why in offenders[:3])
        more = f" (and {len(offenders) - 3} more)" if len(offenders) > 3 else ""
        raise ValueError(
            f"{len(offenders)} of {len(self.entries)} sources are not on the target grid "
            f"({self.grid.describe()}): {shown}{more}. Pass --resampling "
            f"{'|'.join(RESAMPLING_METHODS)} to warp them onto it, or choose the grid with "
            "--crs/--transform/--shape"
        )

    def fingerprint(self) -> Any:
        return {
            "kind": "manifest",
            "entries": [[e.uri, str(e.time)] for e in self.entries],
            "grid": [self.grid.crs, list(self.grid.transform), self.grid.height, self.grid.width],
            "bands": [b.to_attrs() for b in self.info.bands],
            "dtype": self.info.dtype.name,
            "nodata": self.info.nodata,
            "mask": self.info.mask,
            "validity": self.info.validity,
            "resampling": self.resampling if self.warped else None,
            "warped": sorted(self.warped),
        }

    def read(self, t: int) -> Step:
        import rasterio

        entry, header = self.entries[t], self._headers[t]
        grid, info = self.grid, self.info
        data = np.empty((info.n_band, grid.height, grid.width), dtype=info.dtype)
        valid = np.empty((grid.height, grid.width), dtype=np.uint8) if info.mask else None
        try:
            with rasterio.Env(**_gdal_env(entry.uri)), rasterio.open(entry.uri) as src:
                if t in self.warped:
                    self._read_warped(src, header, data, valid)
                else:
                    for top in range(0, grid.height, self.chunk_size):
                        rows = min(self.chunk_size, grid.height - top)
                        window = ((top, top + rows), (0, grid.width))
                        block = src.read(list(header.data_indexes), window=window)
                        data[:, top : top + rows, :] = block
                        if valid is not None:
                            valid[top : top + rows] = self._declared_valid(src, header, window)
                            valid[top : top + rows] &= _value_valid(block, self._sentinels)
        except rasterio.errors.RasterioError as exc:
            raise OSError(f"reading {entry.uri} (timestep {t}): {exc}") from exc
        _settle_nan(data, valid, info.nodata, f"{entry.uri} (timestep {t})")
        return Step(data, valid)

    def _declared_valid(
        self,
        src: Any,
        header: _CogHeader,
        window: tuple[tuple[int, int], tuple[int, int]] | None,
    ) -> np.ndarray:
        """(rows, cols) bool of what the source itself declares valid.

        Its alpha band if it has one (nonzero), else its internal or per-dataset mask, else, with
        an automatic nodata, GDAL's band masks from the nodata value; valid in every data band.
        """
        if header.alpha is not None:
            return src.read(header.alpha, window=window) != 0
        if header.internal_mask or (
            self._auto_nodata and any(token is not None for token in header.nodata)
        ):
            masks = src.read_masks(list(header.data_indexes), window=window)
            return (masks != 0).all(axis=0)
        if window is None:
            return np.ones((src.height, src.width), dtype=bool)
        (row0, row1), (col0, col1) = window
        return np.ones((row1 - row0, col1 - col0), dtype=bool)

    @staticmethod
    def _source_grid(src: Any, header: _CogHeader) -> dict[str, Any]:
        """`reproject` keywords for the source grid: the file's own or the one from options."""
        from rasterio.transform import Affine

        if header.own_grid:
            return {"src_transform": src.transform, "src_crs": src.crs}
        return {"src_transform": Affine(*header.grid.transform), "src_crs": header.grid.crs}

    def _read_warped(
        self, src: Any, header: _CogHeader, data: np.ndarray, valid: np.ndarray | None
    ) -> None:
        import rasterio
        from rasterio.enums import Resampling
        from rasterio.transform import Affine
        from rasterio.warp import reproject

        assert self.resampling is not None
        grid = self.grid
        fill = 0 if self.info.nodata is None else self.info.nodata
        data[:] = fill
        if self._auto_nodata:
            src_nodata = header.warp_nodata
        else:
            src_nodata = self._sentinels[0] if self._sentinels else None
        # A frame whose CRS or transform came from --crs, a world file or --bounds has them only in
        # `header.grid`, so its pixels are handed over with that grid instead of as a dataset band.
        source_grid = self._source_grid(src, header)
        if header.own_grid:
            source, source_options = rasterio.band(src, list(header.data_indexes)), {}
        else:
            source, source_options = src.read(list(header.data_indexes)), source_grid
        reproject(
            source=source,
            destination=data,
            src_nodata=src_nodata,
            **source_options,
            dst_transform=Affine(*grid.transform),
            dst_crs=grid.crs,
            dst_nodata=fill,
            resampling=_parse_resampling(self.resampling),
        )
        if valid is not None:
            # Validity is resampled by nearest neighbour; outside the footprint stays 0.
            warped = np.zeros(valid.shape, dtype=np.uint8)
            reproject(
                source=self._declared_valid(src, header, None).astype(np.uint8),
                destination=warped,
                **source_grid,
                src_nodata=None,
                dst_transform=Affine(*grid.transform),
                dst_crs=grid.crs,
                dst_nodata=0,
                resampling=Resampling.nearest,
            )
            valid[:] = (warped != 0) & _value_valid(data, self._sentinels)
