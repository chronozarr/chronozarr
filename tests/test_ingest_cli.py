"""ingest.py's command line: an AOI from --bbox or --aoi-file runs the whole pipeline."""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("rasterio")
pytest.importorskip("planetary_computer")
pytest.importorskip("pystac_client")
pytest.importorskip("yaml")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "sentinel2_pc"))
import ingest
import mosaic

import chronozarr
from chronozarr.schema import validate
from tests.test_ingest_mosaic import BBOX, EPSG, build_case, settings

pytestmark = [
    pytest.mark.unit,
    # rasterio wraps a numpy warp destination in a MEM dataset inside warnings.catch_warnings()
    # to hide this warning, then sets the transform. catch_warnings is not thread-safe, so with
    # concurrent warps the filter is sometimes gone when the warning fires; the outputs are
    # unaffected. Every input in these tests is georeferenced.
    pytest.mark.filterwarnings(
        "ignore:Dataset has no geotransform, gcps, or rpcs:rasterio.errors.NotGeoreferencedWarning"
    ),
]

WEST, SOUTH, EAST, NORTH = BBOX
BBOX_ARGS = [str(v) for v in BBOX]


def settings_args(start: str = "2024-01-01", end: str = "2024-03-31") -> list[str]:
    return ["--start", start, "--end", end, "--performance", "fixed", "--memory", "2GB"]


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(mosaic, "READ_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(mosaic, "READ_ATTEMPTS", 2)


@pytest.fixture
def synthetic_scenes(tmp_path) -> dict:
    """The synthetic scenes of January and March, one of them warped from another UTM zone."""
    _, by_month = build_case(tmp_path)
    return {m: by_month[m] for m in ("2024-01", "2024-03")}


@pytest.fixture
def searches(monkeypatch, synthetic_scenes) -> list[tuple]:
    """Planetary Computer replaced by the synthetic scenes; every search is recorded as
    (bbox, start, end)."""
    calls: list[tuple] = []

    def search(bbox, start, end, max_cloud_pct):
        calls.append((bbox, start, end))
        return synthetic_scenes

    monkeypatch.setattr(ingest, "search_scenes_by_month", search)
    monkeypatch.setattr(ingest, "sign_href", str)
    return calls


def run_ingest(monkeypatch, *argv: str) -> None:
    monkeypatch.setattr(sys, "argv", ["ingest.py", *argv])
    ingest.main()


def write_polygon(path: Path, bbox=BBOX) -> Path:
    west, south, east, north = bbox
    ring = [[west, south], [east, south], [east, north], [west, north], [west, south]]
    path.write_text(json.dumps({"type": "Polygon", "coordinates": [ring]}))
    return path


def month_hashes(out: Path, name: str) -> dict[str, str]:
    files = sorted((out / "mosaics" / name).glob("*.tif"))
    return {p.stem: mosaic.month_record(p)["bands_sha256"] for p in files}


# --- the pipeline from a bounding box or a file ------------------------------------------------


def test_bbox_builds_months_and_a_valid_store_on_the_derived_grid(tmp_path, monkeypatch, searches):
    out = tmp_path / "out"
    run_ingest(
        monkeypatch,
        *("--bbox", *BBOX_ARGS, "--name", "synth", "--out-dir", str(out)),
        *settings_args(),
    )

    assert searches == [(BBOX, "2024-01-01", "2024-03-31")]
    months = sorted(p.name for p in (out / "mosaics" / "synth").iterdir())
    assert months == ["2024-01.tif", "2024-03.tif"]
    store_dir = out / "stores" / "synth" / "chronozarr"
    assert validate(store_dir) == []
    level = chronozarr.open_store(store_dir).levels[0]
    transform, height, width = mosaic.compute_target_grid(BBOX, EPSG)
    assert level.shape[2:] == (height, width)
    assert tuple(level.transform)[:6] == tuple(transform)[:6]
    assert chronozarr.open_store(store_dir).attrs.crs == f"EPSG:{EPSG}"  # derived, not given


@pytest.mark.parametrize(
    "bbox_args",
    [
        ["--bbox", *BBOX_ARGS],
        ["--bbox", ",".join(BBOX_ARGS)],
        [f"--bbox={','.join(BBOX_ARGS)}"],
    ],
    ids=["four-numbers", "comma-separated", "equals-form"],
)
def test_a_western_bbox_is_accepted_in_every_spelling(tmp_path, monkeypatch, searches, bbox_args):
    """argparse alone reads `--bbox -75.0,-7.6,...` as an option, not a value."""
    run_ingest(
        monkeypatch, *bbox_args, "--name", "west", "--out-dir", str(tmp_path), *settings_args()
    )
    assert searches == [(BBOX, "2024-01-01", "2024-03-31")]


def test_aoi_file_gives_the_same_months_as_its_bbox(tmp_path, monkeypatch, searches):
    """The bounding box of a polygon is the box the --bbox route is given, so both routes
    build the same grid and the same months; the file name names the AOI."""
    by_bbox, by_file = tmp_path / "by_bbox", tmp_path / "by_file"
    run_ingest(
        monkeypatch,
        *("--bbox", *BBOX_ARGS, "--name", "reach", "--out-dir", str(by_bbox)),
        *settings_args(),
    )
    polygon = write_polygon(tmp_path / "reach.geojson")
    run_ingest(
        monkeypatch, *("--aoi-file", str(polygon), "--out-dir", str(by_file)), *settings_args()
    )

    assert set(month_hashes(by_file, "reach")) == {"2024-01", "2024-03"}
    assert month_hashes(by_bbox, "reach") == month_hashes(by_file, "reach")
    assert (by_file / "stores" / "reach" / "chronozarr" / "zarr.json").exists()


def test_name_overrides_the_file_name_and_names_the_stac_id(tmp_path, monkeypatch, searches):
    out = tmp_path / "out"
    polygon = write_polygon(tmp_path / "reach.geojson")
    run_ingest(
        monkeypatch,
        *("--aoi-file", str(polygon), "--name", "bosque", "--stac", "--out-dir", str(out)),
        *settings_args(),
    )
    assert (out / "stores" / "bosque" / "chronozarr").is_dir()
    assert not (out / "mosaics" / "reach").exists()
    collection = json.loads((out / "stores" / "bosque" / "stac" / "collection.json").read_text())
    assert collection["id"] == "bosque-sentinel-2-monthly"


def test_aoi_from_aois_yaml_is_unchanged(tmp_path, monkeypatch):
    seen = {}

    def download(aoi, start, end, mosaic_dir, *args, **kwargs):
        seen.update(aoi=aoi, dates=(start, end), mosaic_dir=mosaic_dir)

    def encode(mosaic_dir, store_dir, **kwargs):
        seen.update(store=store_dir)

    monkeypatch.setattr(ingest, "download_mosaics", download)
    monkeypatch.setattr(ingest, "encode", encode)
    run_ingest(monkeypatch, "--aoi", "lake_mead", "--out-dir", str(tmp_path), *settings_args())

    assert seen["aoi"]["name"] == "lake_mead"
    assert seen["aoi"]["epsg"] == 32611 and seen["aoi"]["bbox"] == [-114.85, 36.00, -114.60, 36.25]
    assert seen["dates"] == ("2024-01-01", "2024-03-31")
    assert seen["mosaic_dir"] == tmp_path / "mosaics" / "lake_mead"
    assert seen["store"] == tmp_path / "stores" / "lake_mead" / "chronozarr"


# --- what is refused, and before any download --------------------------------------------------


@pytest.mark.parametrize(
    "argv,message",
    [
        (["--aoi", "lake_mead", "--bbox", "-1", "-1", "1", "1"], "not allowed with argument"),
        (["--aoi-file", "x.geojson", "--bbox", "-1", "-1", "1", "1"], "not allowed with"),
        ([], "one of the arguments --aoi --bbox --aoi-file is required"),
        (["--bbox", *BBOX_ARGS], "--bbox needs --name"),
        (["--bbox", *BBOX_ARGS, "--name", "a/b"], "AOI name 'a/b'"),
        (
            ["--bbox", "-75", "-7", "-74", "-7.5", "--name", "x"],
            "south .* must be less than north",
        ),
        (["--aoi", "lake_mead", "--name", "x"], "--name goes with --bbox or --aoi-file"),
        (["--aoi", "nowhere"], "Unknown AOI 'nowhere'"),
        (["--aoi-file", "/does/not/exist.geojson"], "cannot read GeoJSON"),
        (["--bbox", *BBOX_ARGS, "--name", "x", "--start", "2024-13-01"], "YYYY-MM-DD"),
        (["--bbox", *BBOX_ARGS, "--name", "x", "--end", "2014-01-01"], "is before --start"),
    ],
)
def test_bad_aoi_or_dates_exit_with_the_reason_and_search_nothing(
    tmp_path, monkeypatch, searches, capsys, argv, message
):
    with pytest.raises(SystemExit) as stopped:
        run_ingest(monkeypatch, *argv, "--out-dir", str(tmp_path))
    assert stopped.value.code == 2
    assert searches == []
    assert list(tmp_path.glob("mosaics/*")) == []
    assert re.search(message, capsys.readouterr().err)


def test_months_on_another_grid_under_the_same_name_stop_the_run_before_any_search(
    tmp_path, monkeypatch, searches, synthetic_scenes
):
    """A month that exists is skipped, so reusing a name for another box would encode the old
    box's months as the new AOI."""
    out = tmp_path / "out"
    mosaic.build_monthly_mosaics(
        synthetic_scenes,
        BBOX,
        EPSG,
        out / "mosaics" / "synth",
        settings=settings(),
        sign=str,
    )
    args = ["--name", "synth", "--out-dir", str(out), *settings_args()]

    shifted = [str(v) for v in (WEST + 0.01, SOUTH, EAST + 0.01, NORTH)]
    with pytest.raises(SystemExit, match=r"different grid .*Months that exist are skipped"):
        run_ingest(monkeypatch, "--bbox", *shifted, *args)
    assert searches == []
    assert not (out / "stores").exists()

    run_ingest(monkeypatch, "--bbox", *BBOX_ARGS, *args)  # the same box resumes from its months
    assert validate(out / "stores" / "synth" / "chronozarr") == []


def test_a_search_without_scenes_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "search_scenes_by_month", lambda *a, **k: {})
    with pytest.raises(SystemExit, match=r"error: no Sentinel-2 L2A scenes .* from 2024-01-01"):
        run_ingest(
            monkeypatch,
            *("--bbox", *BBOX_ARGS, "--name", "empty", "--out-dir", str(tmp_path)),
            *settings_args(),
        )
    assert not (tmp_path / "stores").exists()


# --- dates -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "start,end,partial",
    [
        ("2024-01-01", "2024-03-31", []),
        ("2024-02-01", "2024-02-29", []),  # leap year
        ("2023-02-01", "2023-02-28", []),
        ("2024-01-01", "2024-01-01", ["2024-01 (until 2024-01-01)"]),
        ("2024-01-15", "2024-03-31", ["2024-01 (from 2024-01-15)"]),
        ("2024-01-15", "2024-03-30", ["2024-01 (from 2024-01-15)", "2024-03 (until 2024-03-30)"]),
        ("2015-07-01", "2026-04-01", ["2026-04 (until 2026-04-01)"]),  # the default range
    ],
)
def test_months_a_range_covers_only_in_part(start, end, partial):
    assert ingest.check_dates(start, end) == partial


def test_a_partial_month_is_warned_about_once_before_the_download(
    tmp_path, monkeypatch, searches, caplog
):
    caplog.set_level(logging.WARNING, logger="ingest")
    run_ingest(
        monkeypatch,
        *("--bbox", *BBOX_ARGS, "--name", "partial", "--out-dir", str(tmp_path)),
        *settings_args(start="2024-01-15"),
    )
    warnings_ = [r.getMessage() for r in caplog.records if r.name == "ingest"]
    assert len(warnings_) == 1
    assert "2024-01 (from 2024-01-15)" in warnings_[0] and "only in part" in warnings_[0]
