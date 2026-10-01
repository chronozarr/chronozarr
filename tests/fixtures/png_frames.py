"""Tiny georeferenced PNG frames for the `convert` tests, one builder per sidecar case.

GDAL's PNG driver can only copy a dataset, and it writes sidecars of its own choosing, so each
PNG is assembled here from its chunks (8-bit, no interlace, filter 0) and the sidecar is written
by hand. That keeps every case exact:

* world file: `<frame>.pgw`, six lines, no CRS;
* `.aux.xml`: `<frame>.png.aux.xml`, the CRS and optionally the geotransform;
* neither: a bare PNG.

Special pixels move one column or row per timestep so masks differ between timesteps:

* alpha 0 at (3+t, 4) over the colour 200, a transparent pixel that holds a value;
* alpha 128 at (2, 2+t), partly transparent, still valid;
* colour 0 with alpha 255 at (5, 6+t), a valid zero.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from rasterio.crs import CRS as RasterioCRS  # ty: ignore[unresolved-import]  # compiled

from tests.synthetic import CRS, TRANSFORM

N_TIME, HEIGHT, WIDTH = 4, 24, 32
DATES = ["2024-01-01", "2024-02-01", "2024-03-01", "2024-04-01"]
# PNG colour type by channel count: gray, gray + alpha, RGB, RGBA
_COLOR_TYPES = {1: 0, 2: 4, 3: 2, 4: 6}
_PALETTE_TYPE = 3


def _chunk(tag: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body))


def write_png(path: Path, pixels: np.ndarray, *, palette: np.ndarray | None = None) -> Path:
    """An 8-bit PNG of `pixels` (height, width[, channels]); `palette` (n, 3) makes it indexed."""
    array = np.asarray(pixels, dtype=np.uint8)
    if array.ndim == 2:
        array = array[:, :, np.newaxis]
    height, width, channels = array.shape
    if palette is not None and channels != 1:
        raise ValueError("a palette PNG has one channel of indexes")
    color_type = _PALETTE_TYPE if palette is not None else _COLOR_TYPES[channels]
    rows = b"".join(b"\x00" + array[row].tobytes() for row in range(height))
    parts = [
        b"\x89PNG\r\n\x1a\n",
        _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)),
    ]
    if palette is not None:
        parts.append(_chunk(b"PLTE", np.asarray(palette, dtype=np.uint8).tobytes()))
    parts += [_chunk(b"IDAT", zlib.compress(rows)), _chunk(b"IEND", b"")]
    path.write_bytes(b"".join(parts))
    return path


def write_world_file(frame: Path, transform: tuple[float, ...] = TRANSFORM) -> Path:
    """`<frame>.pgw`: A, D, B, E, then C and F, the centre of the upper-left pixel."""
    a, b, c, d, e, f = transform
    values = [a, d, b, e, c + a / 2 + b / 2, f + d / 2 + e / 2]
    path = frame.with_suffix(".pgw")
    path.write_text("".join(f"{value!r}\n" for value in values))
    return path


def write_aux_xml(
    frame: Path, *, crs: str | None = CRS, transform: tuple[float, ...] | None = TRANSFORM
) -> Path:
    """`<frame>.aux.xml` as GDAL's PAM writes it: the SRS and GeoTransform, each optional."""
    parts = []
    if crs is not None:
        parts.append(f"<SRS>{RasterioCRS.from_user_input(crs).to_wkt()}</SRS>")
    if transform is not None:
        a, b, c, d, e, f = transform
        parts.append(f"<GeoTransform>{c!r}, {a!r}, {b!r}, {f!r}, {d!r}, {e!r}</GeoTransform>")
    path = frame.parent / f"{frame.name}.aux.xml"
    path.write_text(f"<PAMDataset>{''.join(parts)}</PAMDataset>")
    return path


@dataclass(frozen=True)
class FrameSet:
    """Frame files and what they hold. `pixels` is (time, y, x, channel) as written."""

    paths: list[Path]
    pixels: np.ndarray

    @property
    def rgb(self) -> np.ndarray:
        """(time, band, y, x) of the three colour channels, which is what a store holds."""
        return self.pixels[..., :3].transpose(0, 3, 1, 2)

    @property
    def alpha(self) -> np.ndarray:
        """(time, y, x) bool: alpha is nonzero."""
        return np.asarray(self.pixels[..., 3] != 0)

    def dates(self) -> list[str]:
        return DATES[: len(self.paths)]


def write_manifest(path: Path, frames: FrameSet, bands: str | None = None) -> Path:
    header = "uri,datetime" + (",bands" if bands else "")
    rows = [
        f"{png},{date}" + (f",{bands}" if bands else "")
        for png, date in zip(frames.paths, frames.dates(), strict=True)
    ]
    path.write_text("\n".join([header, *rows[::-1]]) + "\n")  # reversed: manifests need not sort
    return path


def rgba_frames(
    folder: Path,
    sidecar: str = "pgw",
    *,
    crs: str = CRS,
    transform: tuple[float, ...] = TRANSFORM,
) -> FrameSet:
    """RGBA frames with a `sidecar`: "pgw" (world file), "aux" (`.aux.xml` with SRS and
    GeoTransform) or "none"."""
    rng = np.random.default_rng(11)
    pixels = np.empty((N_TIME, HEIGHT, WIDTH, 4), dtype=np.uint8)
    pixels[..., :3] = rng.integers(10, 200, size=(N_TIME, HEIGHT, WIDTH, 3))
    pixels[..., 3] = 255
    for t in range(N_TIME):
        pixels[t, 3 + t, 4] = (200, 200, 200, 0)
        pixels[t, 2, 2 + t, 3] = 128
        pixels[t, 5, 6 + t] = (0, 0, 0, 255)
    paths = []
    for t in range(N_TIME):
        png = write_png(folder / f"frame_{t}.png", pixels[t])
        if sidecar == "pgw":
            write_world_file(png, transform)
        elif sidecar == "aux":
            write_aux_xml(png, crs=crs, transform=transform)
        elif sidecar != "none":
            raise ValueError(f"unknown sidecar {sidecar!r}")
        paths.append(png)
    return FrameSet(paths, pixels)


def bounds_of(
    transform: tuple[float, ...] = TRANSFORM, height: int = HEIGHT, width: int = WIDTH
) -> tuple[float, float, float, float]:
    """west, south, east, north of a north-up grid."""
    a, _, c, _, e, f = transform
    return (c, f + e * height, c + a * width, f)
