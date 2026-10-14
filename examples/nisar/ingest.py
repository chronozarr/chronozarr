r"""Ingest a NISAR L2 GCOV backscatter time series from the ASF DAAC into a chronozarr store.

One box (--bbox, WGS84), one track, one orbit direction and one frame: every timestep is the
same ground footprint seen from the same geometry, so the series compares like with like. The
scenes come from asf_search (dataset NISAR, product GCOV, standard production), restricted to
one collection maturity (--collection, PROVISIONAL by default) and to dual-pol HH+HV. By default
the track, direction and frame with the most dates over the box is taken; --track, --direction
and --frame pin one. A date acquired twice (a reprocessed product) keeps the highest product
counter.

Each granule is opened at its ASF URL with an Earthdata login (the urs.earthdata.nasa.gov entry
in ~/.netrc) through HTTP range reads, and only the window of the box is read from
/science/LSAR/GCOV/grids/frequencyA: HHHH, HVHV and mask. Nothing else of the granule (about
2 GB) is transferred. The window is cut on the product's own 10 m UTM grid, pixel for pixel, with
no warp; a granule whose grid differs from the first one's fails the run.

Stored values. HH and HV are gamma0 backscatter power (radiometric terrain corrected, as GCOV
delivers it) in dB, 10 * log10(power), quantized to 0.01 dB as uint16: stored = (dB + 60) * 100,
so the band metadata (scale 0.01, offset -60) gives dB back; values below -60 dB are clipped to
-60 dB. The store carries a mask: a pixel is valid when the GCOV mask marks it as a fully focused
sample of a subswath (neither 0 nor 255), both powers are finite and both are above zero. Invalid
pixels store 0. Coarser levels average dB (the spec's mean pyramid), which is the geometric mean
of power and below the linear mean; level 0 holds the quantized product values. A scene whose
window holds no valid pixel (a partial frame that ends before the box) is left out and named in
the manifest.

Usage:
    uv run --extra nisar python examples/nisar/ingest.py --bbox -114.82 32.50 -114.42 32.90 \
        --name colorado_yuma
    uv run --extra nisar python examples/nisar/ingest.py --bbox -114.82 32.50 -114.42 32.90 \
        --name colorado_yuma --dry-run
    uv run --extra nisar python examples/nisar/ingest.py --bbox -114.82 32.50 -114.42 32.90 \
        --name colorado_yuma --track 5 --direction A --frame 18 --start 2026-01-01

Output: <out-dir>/stores/<name>/<store> (the store; `--store` names it, nisar-gcov-v1 by
default, and a re-encode takes a new name because a published store is immutable),
<out-dir>/stores/<name>/<store>.scenes.json (the manifest: query, ASF properties,
identification, window and validity of every scene) and <out-dir>/cache/nisar/<name>/
<granule>.npz (the window of each granule with its manifest record beside it, which a rerun
reuses instead of reading the granule again).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np

import chronozarr
from chronozarr.schema import Band

ROOT = Path(__file__).resolve().parents[2]
STORE_NAME = "nisar-gcov-v1"
GROUP = "science/LSAR/GCOV/grids/frequencyA"
IDENTIFICATION = "science/LSAR/identification"
COLLECTIONS = {
    "PROVISIONAL": "NISAR_L2_GCOV_PROVISIONAL_V1",
    "BETA": "NISAR_L2_GCOV_BETA_V1",
}
DB_SCALE = 0.01
DB_OFFSET = -60.0
MASK_INVALID = 0  # partially focused or invalid radar samples
MASK_OUTSIDE = 255  # outside the acquisition extent
BLOCK_SIZE = 16 * 1024 * 1024
READ_TIMEOUT = 120  # seconds without bytes before a granule read fails
ATTEMPTS = 2
IDENTIFICATION_FIELDS = (
    "absoluteOrbitNumber",
    "compositeReleaseId",
    "frameNumber",
    "isFullFrame",
    "orbitPassDirection",
    "processingDateTime",
    "productVersion",
    "trackNumber",
    "zeroDopplerStartTime",
    "zeroDopplerEndTime",
)
# NISAR_L2_PR_GCOV_<cycle>_<track>_<A|D>_<frame>_<mode>_<pol>_<x>_<start>_<end>_<crid>_<flags>_
# <counter>
NAME_RE = re.compile(
    r"^NISAR_L2_PR_GCOV_(?P<cycle>\d{3})_(?P<track>\d{3})_(?P<direction>[AD])_(?P<frame>\d{3})_"
    r"(?P<mode>\d{4})_(?P<pol>[A-Z]{4})_[A-Z]_(?P<start>\d{8}T\d{6})_(?P<end>\d{8}T\d{6})_"
    r"(?P<crid>[A-Z0-9]+)_(?P<flags>[A-Z_]+)_(?P<counter>\d{3})$"
)

logger = logging.getLogger("nisar_ingest")


@dataclass(frozen=True)
class Scene:
    granule: str
    start: str  # ISO 8601, from ASF
    url: str
    md5: str | None
    collection: str
    cycle: int
    track: int
    direction: str
    frame: int
    mode: str
    pol: str
    counter: int
    full_frame: bool


@dataclass(frozen=True)
class Window:
    """The box on the product grid: pixel-centre coordinate ranges and the level-0 transform."""

    epsg: int
    x: tuple[float, float]  # first and last column centre
    y: tuple[float, float]  # first (north) and last (south) row centre
    dx: float
    dy: float
    width: int
    height: int

    @property
    def transform(self) -> list[float]:
        return [self.dx, 0.0, self.x[0] - self.dx / 2, 0.0, self.dy, self.y[0] - self.dy / 2]


def parse_scene(properties: dict, short_name: str) -> Scene | None:
    """A Scene from an asf_search result, or None when the name is not a standard GCOV product."""
    name = properties.get("sceneName") or ""
    match = NAME_RE.match(name)
    if match is None:
        return None
    flags = match["flags"].split("_")
    return Scene(
        granule=name,
        start=properties["startTime"],
        url=properties["url"],
        md5=properties.get("md5sum"),
        collection=short_name,
        cycle=int(match["cycle"]),
        track=int(match["track"]),
        direction=match["direction"],
        frame=int(match["frame"]),
        mode=match["mode"],
        pol=match["pol"],
        counter=int(match["counter"]),
        full_frame="F" in flags[1:2],
    )


def select_scenes(
    scenes: list[Scene],
    track: int | None = None,
    direction: str | None = None,
    frame: int | None = None,
) -> tuple[tuple[int, str, int], list[Scene]]:
    """One (track, direction, frame) series in time order, one scene per start time.

    Without a pin, the series with the most distinct start times wins. A start time acquired
    more than once keeps the scene with the highest product counter.
    """
    pinned = [
        s
        for s in scenes
        if (track is None or s.track == track)
        and (direction is None or s.direction == direction)
        and (frame is None or s.frame == frame)
    ]
    if not pinned:
        raise SystemExit("no scene matches the track, direction and frame asked for")
    counts = Counter((s.track, s.direction, s.frame) for s in pinned)
    key = max(counts, key=lambda k: (counts[k], -k[0], k[1], -k[2]))
    chosen = [s for s in pinned if (s.track, s.direction, s.frame) == key]
    latest: dict[str, Scene] = {}
    for scene in chosen:
        held = latest.get(scene.start)
        if held is None or scene.counter > held.counter:
            latest[scene.start] = scene
    return key, sorted(latest.values(), key=lambda s: s.start)


BBox = tuple[float, float, float, float]


def discover(bbox: BBox, start: str, end: str, collection: str) -> list[Scene]:
    import asf_search as asf

    west, south, east, north = bbox
    wkt = f"POLYGON(({west} {south},{east} {south},{east} {north},{west} {north},{west} {south}))"
    results = asf.search(
        dataset="NISAR",
        processingLevel="GCOV",
        productionConfiguration="PR",
        intersectsWith=wkt,
        start=start,
        end=end,
        maxResults=2000,
    )
    scenes = []
    for result in results:
        short_name = (result.umm or {}).get("CollectionReference", {}).get("ShortName", "")
        if short_name != COLLECTIONS[collection]:
            continue
        scene = parse_scene(result.properties, short_name)
        if scene is not None and scene.pol == "DHDH":
            scenes.append(scene)
    return scenes


def box_on_grid(bbox: BBox, epsg: int) -> BBox:
    """The box in the product CRS: the bounding rectangle of its edges, sampled every ~1 km."""
    from pyproj import Transformer

    west, south, east, north = bbox
    n = 64
    lons = np.linspace(west, east, n)
    lats = np.linspace(south, north, n)
    edge_lon = np.concatenate([lons, lons, np.full(n, west), np.full(n, east)])
    edge_lat = np.concatenate([np.full(n, south), np.full(n, north), lats, lats])
    to_grid = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
    x, y = to_grid.transform(edge_lon, edge_lat)
    return float(x.min()), float(y.min()), float(x.max()), float(y.max())


def window_on(x: np.ndarray, y: np.ndarray, epsg: int, box: BBox) -> Window:
    """The pixels of the first scene's grid whose centres fall inside `box` (product CRS)."""
    xmin, ymin, xmax, ymax = box
    dx = float(x[1] - x[0])
    dy = float(y[1] - y[0])
    if dx <= 0 or dy >= 0:
        raise SystemExit(f"grid is not north-up with increasing x: dx={dx}, dy={dy}")
    columns = np.flatnonzero((x >= xmin) & (x <= xmax))
    rows = np.flatnonzero((y >= ymin) & (y <= ymax))
    if columns.size == 0 or rows.size == 0:
        raise SystemExit("the box does not overlap the product grid")
    return Window(
        epsg=epsg,
        x=(float(x[columns[0]]), float(x[columns[-1]])),
        y=(float(y[rows[0]]), float(y[rows[-1]])),
        dx=dx,
        dy=dy,
        width=int(columns.size),
        height=int(rows.size),
    )


def slices_of(
    window: Window, x: np.ndarray, y: np.ndarray, epsg: int, granule: str
) -> tuple[slice, slice]:
    """Row and column slices of `window` on this scene's grid; the grid must hold it exactly."""
    if epsg != window.epsg:
        raise SystemExit(f"{granule} is on EPSG:{epsg}, the series is on EPSG:{window.epsg}")
    c0 = int(np.searchsorted(x, window.x[0]))
    r0 = int(np.searchsorted(-y, -window.y[0]))
    cols = slice(c0, c0 + window.width)
    rows = slice(r0, r0 + window.height)
    expected_x = window.x[0] + window.dx * np.arange(window.width)
    expected_y = window.y[0] + window.dy * np.arange(window.height)
    if not (np.array_equal(x[cols], expected_x) and np.array_equal(y[rows], expected_y)):
        raise SystemExit(
            f"{granule} does not hold the window on the same grid (x {x[0]}..{x[-1]} step "
            f"{x[1] - x[0]}, y {y[0]}..{y[-1]} step {y[1] - y[0]})"
        )
    return rows, cols


def stored_db(power: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """uint16 dB at 0.01 dB (stored = (dB + 60) * 100, clipped at -60 dB); 0 where invalid."""
    db = np.zeros(power.shape, dtype=np.float32)
    np.log10(power, out=db, where=valid)
    stored = np.zeros(power.shape, dtype=np.uint16)
    quantized = np.rint((db[valid] * 10.0 - DB_OFFSET) / DB_SCALE)
    stored[valid] = np.clip(quantized, 0, np.iinfo(np.uint16).max).astype(np.uint16)
    return stored


def validity(mask: np.ndarray, hh: np.ndarray, hv: np.ndarray) -> np.ndarray:
    return (
        (mask != MASK_INVALID)
        & (mask != MASK_OUTSIDE)
        & np.isfinite(hh)
        & np.isfinite(hv)
        & (hh > 0)
        & (hv > 0)
    )


def open_granule(url: str):
    """(fsspec file, h5py.File) on the granule at `url`, with the Earthdata token.

    A read that receives nothing for READ_TIMEOUT seconds fails instead of hanging; `extract`
    retries the granule once.
    """
    import aiohttp
    import earthaccess
    import fsspec
    import h5py

    auth = earthaccess.login(strategy="netrc", persist=False)
    token = (auth.token or {}).get("access_token") if auth.authenticated else None
    if not token:
        raise SystemExit(
            "Earthdata login failed: check the urs.earthdata.nasa.gov entry in ~/.netrc"
        )
    fs = fsspec.filesystem(
        "http",
        headers={"Authorization": f"Bearer {token}"},
        client_kwargs={
            "timeout": aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=READ_TIMEOUT)
        },
    )
    # A synchronous block cache: the background one (a prefetch thread) hung a read for minutes.
    remote = fs.open(url, "rb", block_size=BLOCK_SIZE, cache_type="blockcache")
    return remote, h5py.File(remote, "r")


def read_grid(h5) -> tuple[np.ndarray, np.ndarray, int]:
    grid = h5[GROUP]
    needed = ("HHHH", "HVHV", "mask", "xCoordinates", "yCoordinates", "projection")
    missing = [name for name in needed if name not in grid]
    if missing:
        raise SystemExit(f"{GROUP} lacks {missing}")
    projection = grid["projection"]
    epsg = int(projection.attrs.get("epsg_code", projection[()]))
    return grid["xCoordinates"][:], grid["yCoordinates"][:], epsg


def read_identification(h5) -> dict:
    ident = h5[IDENTIFICATION]
    out = {}
    for key in IDENTIFICATION_FIELDS:
        if key in ident:
            value = ident[key][()]
            if isinstance(value, bytes):
                value = value.decode()
            elif isinstance(value, np.generic):
                value = value.item()
            out[key] = value
    return out


def read_window(scene: Scene, window: Window) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict]:
    """HHHH, HVHV, mask of the window and the identification of one granule."""
    remote, h5 = open_granule(scene.url)
    with remote, h5:
        x, y, epsg = read_grid(h5)
        rows, cols = slices_of(window, x, y, epsg, scene.granule)
        grid = h5[GROUP]
        hh = grid["HHHH"][rows, cols]
        hv = grid["HVHV"][rows, cols]
        mask = grid["mask"][rows, cols]
        return hh, hv, mask, read_identification(h5)


def extract(scene: Scene, window: Window, out: Path) -> dict:
    """Read the window of one granule into `out` (.npz) and return its record for the manifest."""
    t0 = time.perf_counter()
    for attempt in range(1, ATTEMPTS + 1):
        try:
            hh, hv, mask, identification = read_window(scene, window)
            break
        except (OSError, TimeoutError) as error:
            if attempt == ATTEMPTS:
                raise RuntimeError(
                    f"{scene.granule}: read failed {ATTEMPTS} times: {error}"
                ) from error
            logger.warning("%s: %s; reading again", scene.granule, error)
    valid = validity(mask, hh, hv)
    hh_db = stored_db(hh, valid)
    hv_db = stored_db(hv, valid)
    out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out, HH=hh_db, HV=hv_db, mask=valid.astype(np.uint8))
    record = {
        **asdict(scene),
        "identification": identification,
        "npz": str(out),
        "npz_sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
        "valid_fraction": float(valid.mean()),
        "subswaths": sorted(int(v) for v in np.unique(mask[valid])),
        "read_seconds": round(time.perf_counter() - t0, 1),
    }
    if valid.any():
        for name, stored in (("HH", hh_db), ("HV", hv_db)):
            p2, p50, p98 = np.percentile(stored[valid], [2, 50, 98])
            record[f"{name}_dB_p2_p50_p98"] = [
                round(float(v) * DB_SCALE + DB_OFFSET, 2) for v in (p2, p50, p98)
            ]
    # The sidecar names the window file, not its path: the cache directory may move.
    sidecar_of(out).write_text(json.dumps({**record, "npz": out.name}, indent=2, default=str))
    return record


def sidecar_of(npz: Path) -> Path:
    """The manifest record of a cached window, written beside it."""
    return npz.with_suffix(".json")


def first_window(scene: Scene, bbox: tuple[float, float, float, float]) -> Window:
    remote, h5 = open_granule(scene.url)
    with remote, h5:
        x, y, epsg = read_grid(h5)
    return window_on(x, y, epsg, box_on_grid(bbox, epsg))


def extract_all(scenes: list[Scene], window: Window, cache: Path, workers: int) -> list[dict]:
    jobs = [(scene, window, cache / f"{scene.granule}.npz") for scene in scenes]
    records: dict[str, dict] = {}
    pending = []
    for scene, _, path in jobs:
        if path.is_file() and sidecar_of(path).is_file():
            logger.info("cached %s", path.name)
            record = json.loads(sidecar_of(path).read_text())
            records[scene.granule] = {**record, "npz": str(path), "cached": True}
        else:
            pending.append((scene, window, path))
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for record in pool.map(extract, *zip(*pending, strict=True)) if pending else []:
            records[record["granule"]] = record
            logger.info(
                "%s: valid %.1f %%, %s s",
                record["start"][:10],
                100 * record["valid_fraction"],
                record["read_seconds"],
            )
    return [records[scene.granule] for scene in scenes]


def utc_naive(iso: str) -> datetime:
    """An ASF start time (ISO 8601 with a zone) as a naive UTC datetime for numpy."""
    stamp = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError(f"{iso!r} carries no time zone")
    return stamp.astimezone(UTC).replace(tzinfo=None)


def encode_store(records: list[dict], window: Window, store: Path, manifest: dict) -> None:
    kept = [r for r in records if r["valid_fraction"] > 0]
    dropped = [r["granule"] for r in records if r["valid_fraction"] == 0]
    if not kept:
        raise SystemExit("no scene holds a valid pixel in the box")
    times = np.array([utc_naive(r["start"]) for r in kept], dtype="datetime64[ns]")

    def frames():
        for record in kept:
            with np.load(record["npz"]) as z:
                yield np.stack([z["HH"], z["HV"]])

    def masks():
        for record in kept:
            with np.load(record["npz"]) as z:
                yield z["mask"]

    notes = (
        f"NISAR L2 GCOV ({kept[0]['collection']}), track {manifest['series']['track']} "
        f"{manifest['series']['direction']} frame {manifest['series']['frame']}, frequency A, "
        "read from the ASF DAAC granules by HTTP range requests on the product's 10 m UTM grid, "
        "no warp. HH and HV are gamma0 power (HHHH, HVHV) as 10*log10 in dB at 0.01 dB: stored = "
        "(dB + 60) * 100, clipped at -60 dB. Valid = GCOV mask neither 0 nor 255, both powers "
        "finite and > 0; invalid pixels store 0. Coarser levels average dB (geometric mean of "
        f"power). Access {manifest['access_date']}."
    )
    if dropped:
        notes += f" Left out, no valid pixel in the box: {', '.join(dropped)}."
    report = chronozarr.encode(
        frames(),
        store,
        crs=f"EPSG:{window.epsg}",
        transform=window.transform,
        times=times,
        bands=[
            Band(name="HH", scale=DB_SCALE, offset=DB_OFFSET, units="dB"),
            Band(name="HV", scale=DB_SCALE, offset=DB_OFFSET, units="dB"),
        ],
        mask=masks(),
        provenance={
            "sources": [r["granule"] for r in kept],
            "composite": "none",
            "gap_fill": "none",
            "notes": notes,
        },
    )
    manifest["store"] = {
        "path": str(store),
        "timesteps": len(kept),
        "dropped": dropped,
        "levels": len(report.levels),
        "files": report.n_files,
        "bytes": report.total_bytes,
    }
    logger.info(
        "Encoded %d timesteps, %d levels: %.1f MB in %d files -> %s",
        len(kept),
        len(report.levels),
        report.total_bytes / 1e6,
        report.n_files,
        store,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--bbox", nargs=4, type=float, required=True, metavar=("W", "S", "E", "N"))
    parser.add_argument("--name", required=True, help="store directory under <out-dir>/stores")
    parser.add_argument(
        "--store", default=STORE_NAME, help="store name under <out-dir>/stores/NAME"
    )
    parser.add_argument("--start", default="2025-10-01")
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--collection", choices=sorted(COLLECTIONS), default="PROVISIONAL")
    parser.add_argument("--track", type=int)
    parser.add_argument("--direction", choices=("A", "D"))
    parser.add_argument("--frame", type=int)
    parser.add_argument("--max-scenes", type=int, help="keep only the first N scenes (smoke runs)")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--workers", type=int, default=4, help="granules read at once")
    parser.add_argument(
        "--dry-run", action="store_true", help="list the scenes and the window, write nothing"
    )
    args = parser.parse_args(argv)
    west, south, east, north = args.bbox
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        parser.error("--bbox is west south east north in degrees, west < east and south < north")
    for option, value in (("--name", args.name), ("--store", args.store)):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
            parser.error(f"{option} may use letters, digits, '_', '-' and '.'")
    return args


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.WARNING, format="%(asctime)s %(message)s", datefmt="%H:%M:%S"
    )
    logger.setLevel(logging.INFO)
    args = parse_args(argv)
    bbox: BBox = (args.bbox[0], args.bbox[1], args.bbox[2], args.bbox[3])
    store = args.out_dir / "stores" / args.name / args.store
    manifest_path = store.with_name(f"{args.store}.scenes.json")
    cache = args.out_dir / "cache" / "nisar" / args.name
    if store.exists() and any(store.iterdir()):
        raise SystemExit(f"{store} exists; stores are immutable: another --name or --out-dir")

    t0 = time.perf_counter()
    scenes = discover(bbox, args.start, args.end, args.collection)
    if not scenes:
        raise SystemExit(
            f"ASF returns no {COLLECTIONS[args.collection]} HH+HV scene over the box between "
            f"{args.start} and {args.end}"
        )
    series, selected = select_scenes(scenes, args.track, args.direction, args.frame)
    if args.max_scenes:
        selected = selected[: args.max_scenes]
    logger.info(
        "%d scenes over the box; track %d %s frame %d: %d dates %s .. %s (%.1f s)",
        len(scenes),
        *series,
        len(selected),
        selected[0].start[:10],
        selected[-1].start[:10],
        time.perf_counter() - t0,
    )
    window = first_window(selected[0], bbox)
    logger.info(
        "window: EPSG:%d, %d x %d px at %g m, x %g..%g, y %g..%g",
        window.epsg,
        window.width,
        window.height,
        window.dx,
        *window.x,
        *window.y,
    )
    if args.dry_run:
        for scene in selected:
            print(scene.start[:10], scene.granule, "" if scene.full_frame else "(partial frame)")
        return

    records = extract_all(selected, window, cache, args.workers)
    manifest = {
        "query": {
            "bbox": list(bbox),
            "start": args.start,
            "end": args.end,
            "collection": COLLECTIONS[args.collection],
        },
        "series": dict(zip(("track", "direction", "frame"), series, strict=True)),
        "window": {**asdict(window), "transform": window.transform},
        "access_date": datetime.now(UTC).date().isoformat(),
        "scenes": records,
    }
    encode_store(records, window, store, manifest)
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str))
    logger.info("manifest -> %s", manifest_path)


if __name__ == "__main__":
    main(sys.argv[1:])
