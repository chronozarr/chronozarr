"""`convert` from a directory, glob or S3 prefix of GeoTIFFs, and the all-problems preflight."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest
from click.testing import CliRunner

import chronozarr
from chronozarr import _convert_discover as discovery
from chronozarr._convert_discover import (
    discover,
    find_dates,
    is_discovery_source,
    list_tiffs,
    write_manifest,
)
from chronozarr.cli import main
from chronozarr.convert import (
    CogManifestSource,
    Entry,
    PreflightError,
    convert,
    plan_conversion,
    read_manifest,
)
from tests.synthetic import CRS, TRANSFORM, make_truth

pytestmark = pytest.mark.unit

N_TIME, N_BAND, HEIGHT, WIDTH = 4, 2, 40, 50
OPTIONS: dict[str, Any] = {"chunk_size": 16}
MONTHS = ["2024-01", "2024-02", "2024-03", "2024-04"]


def day(text: str) -> np.datetime64:
    return np.datetime64(text, "ms")


# --- dates in file names --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stem", "expected"),
    [
        ("ndvi_20210102", ["2021-01-02"]),
        ("ndvi_2021-01-02", ["2021-01-02"]),
        ("ndvi_2021_01_02", ["2021-01-02"]),
        ("ndvi_2021-01", ["2021-01-01"]),
        ("ndvi_2021_01_v2", ["2021-01-01"]),
        ("S2_20210102T103000", ["2021-01-02"]),
        ("tile_T31UFS_2021-01-02", ["2021-01-02"]),
        ("2021-01-02_2021-01-02", ["2021-01-02"]),
        # nothing a person would call a date
        ("tile_123456", []),
        ("scene_20211340", []),
        ("scene_2021-13", []),
        ("v202101", []),  # bare YYYYMM cannot be told from YYMMDD
        ("scene_2021-0102", []),  # mixed separators
        ("ndvi", []),
        # two dates: ambiguous
        ("20210102_2021-01", ["2021-01-01", "2021-01-02"]),
        ("composite_2021-01-02_2021-02-01", ["2021-01-02", "2021-02-01"]),
    ],
)
def test_default_date_rules_read_a_date_only_when_there_is_one(stem, expected):
    found, invalid = find_dates(stem, None)
    assert sorted(str(t.astype("datetime64[D]")) for t in found) == expected
    assert invalid == []


def test_a_month_inside_a_day_is_not_a_second_date():
    found, _ = find_dates("scene_2021-01-02", None)
    assert list(found) == [day("2021-01-02")]


@pytest.mark.parametrize(
    ("pattern", "stem", "expected"),
    [
        ("%Y%m", "ndvi_202101", ["2021-01-01T00:00:00.000"]),
        ("ndvi_%Y%m%d", "x_ndvi_20210102_20210105", ["2021-01-02T00:00:00.000"]),
        ("%Y%m%d%H%M%S", "a_20210102103015", ["2021-01-02T10:30:15.000"]),
        ("%Y-%m-%d %H", "a_2021-01-02 07", ["2021-01-02T07:00:00.000"]),
        ("%Y%m%d", "a_20210102_20210105", ["2021-01-02T00:00:00.000", "2021-01-05T00:00:00.000"]),
        ("%Y%m%d", "nothing_here", []),
        ("%Y%m%d", "a_202101022", []),  # digits around the date are not a date
    ],
)
def test_date_pattern_says_where_the_date_is(pattern, stem, expected):
    found, invalid = find_dates(stem, discovery._compile_pattern(pattern))
    assert sorted(str(t) for t in found) == expected
    assert invalid == []


def test_date_pattern_reports_a_match_that_is_not_a_calendar_date():
    found, invalid = find_dates("a_20211340", discovery._compile_pattern("%Y%m%d"))
    assert found == {}
    assert invalid == ["20211340"]


@pytest.mark.parametrize("pattern", ["%Y", "%m%d", "%Y%m%Y", "%Y%q", "%Y%", "%Y%j"])
def test_malformed_date_patterns_are_refused(pattern):
    with pytest.raises(ValueError, match="--date-pattern"):
        discovery._compile_pattern(pattern)


# --- what counts as a discovery source -----------------------------------------------------


def test_source_kinds(tmp_path):
    folder = tmp_path / "tifs"
    folder.mkdir()
    (folder / "a.tif").write_bytes(b"")
    zarr_dir = tmp_path / "store"
    zarr_dir.mkdir()
    (zarr_dir / "zarr.json").write_text("{}")
    (tmp_path / "plain.tif").write_bytes(b"")
    assert is_discovery_source(str(folder))
    assert is_discovery_source(str(folder / "*.tif"))
    assert is_discovery_source(str(tmp_path / "**" / "*.tiff"))
    assert is_discovery_source("s3://bucket/prefix/")
    assert is_discovery_source(str(tmp_path / "plain.tif"))
    assert not is_discovery_source(str(zarr_dir))
    assert not is_discovery_source(str(tmp_path / "x.zarr"))
    assert not is_discovery_source(str(tmp_path / "m.csv"))
    assert not is_discovery_source(str(tmp_path / "m.json"))
    assert not is_discovery_source(str(tmp_path / "cube.nc"))
    assert not is_discovery_source("https://example.org/store.zarr")
    assert not is_discovery_source(str(tmp_path / "missing_dir"))


# --- listing --------------------------------------------------------------------------------


def touch(*paths: Path) -> None:
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")


def test_a_directory_lists_its_tiffs_without_recursing(tmp_path):
    touch(
        tmp_path / "b_2024-02.tif",
        tmp_path / "a_2024-01.TIF",
        tmp_path / "c_2024-03.tiff",
        tmp_path / "a_2024-01.tif.aux.xml",
        tmp_path / "notes.txt",
        tmp_path / "deeper" / "d_2024-04.tif",
    )
    found = list_tiffs(str(tmp_path))
    assert [Path(p).name for p in found] == ["a_2024-01.TIF", "b_2024-02.tif", "c_2024-03.tiff"]
    assert all(Path(p).is_absolute() for p in found)


def test_a_glob_lists_matches_and_recurses_only_with_double_star(tmp_path):
    touch(tmp_path / "a_2024-01.tif", tmp_path / "deeper" / "b_2024-02.tif", tmp_path / "c.png")
    assert [Path(p).name for p in list_tiffs(str(tmp_path / "*.tif"))] == ["a_2024-01.tif"]
    assert [Path(p).name for p in list_tiffs(str(tmp_path / "**" / "*.tif"))] == [
        "a_2024-01.tif",
        "b_2024-02.tif",
    ]


def test_nothing_found_is_an_actionable_error(tmp_path):
    touch(tmp_path / "readme.txt")
    with pytest.raises(ValueError, match=r"no GeoTIFF files .* found in .*quote a glob"):
        list_tiffs(str(tmp_path))
    with pytest.raises(ValueError, match=r"no GeoTIFF files .* matching .*\.tif'"):
        list_tiffs(str(tmp_path / "*.tif"))
    with pytest.raises(ValueError, match="does not exist"):
        list_tiffs(str(tmp_path / "missing"))
    with pytest.raises(ValueError, match="no GeoTIFF files"):
        discover(str(tmp_path / "*.tif"))


def test_an_s3_prefix_is_listed_through_the_boundary(monkeypatch):
    calls = []

    def fake_keys(bucket: str, prefix: str) -> list[str]:
        calls.append((bucket, prefix))
        return ["data/ndvi/b_2024-02.tif", "data/ndvi/a_2024-01.TIF", "data/ndvi/readme.txt"]

    monkeypatch.setattr(discovery, "_list_s3_keys", fake_keys)
    assert list_tiffs("s3://bkt/data/ndvi") == [
        "s3://bkt/data/ndvi/a_2024-01.TIF",
        "s3://bkt/data/ndvi/b_2024-02.tif",
    ]
    assert list_tiffs("s3://bkt/data/ndvi/") == list_tiffs("s3://bkt/data/ndvi")
    assert calls == [("bkt", "data/ndvi/")] * 3  # a trailing slash: never a sibling prefix
    assert list_tiffs("s3://bkt/data/ndvi/a_2024-01.TIF") == ["s3://bkt/data/ndvi/a_2024-01.TIF"]
    assert len(calls) == 3  # a single object is not listed


def test_s3_prefix_errors_are_actionable(monkeypatch):
    monkeypatch.setattr(discovery, "_list_s3_keys", lambda bucket, prefix: ["x/readme.txt"])
    with pytest.raises(ValueError, match=r"no GeoTIFF files .* under s3://bkt/x/"):
        list_tiffs("s3://bkt/x")
    with pytest.raises(ValueError, match="needs a bucket"):
        list_tiffs("s3:///x")
    with pytest.raises(ValueError, match="not a glob"):
        list_tiffs("s3://bkt/x/*.tif")


def test_s3_listing_failures_name_the_cause_and_the_fix(monkeypatch):
    boto3 = pytest.importorskip("boto3")
    from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError

    class Client:
        def __init__(self, error: Exception) -> None:
            self.error = error

        def get_paginator(self, name: str):
            raise self.error

    denied = ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "ListObjectsV2")
    cases = [
        (NoCredentialsError(), "no AWS credentials found.*AWS_NO_SIGN_REQUEST"),
        (denied, "AccessDenied.*s3:ListBucket"),
        (EndpointConnectionError(endpoint_url="https://x"), "cannot list s3://bkt/p/"),
    ]
    for error, message in cases:
        monkeypatch.setattr(boto3, "client", lambda *a, _e=error, **k: Client(_e))
        with pytest.raises(ValueError, match=message):
            discovery._list_s3_keys("bkt", "p/")


def test_s3_listing_pages_with_a_delimiter_and_can_skip_signing(monkeypatch):
    boto3 = pytest.importorskip("boto3")
    seen: dict = {}

    class Paginator:
        def paginate(self, **kwargs):
            seen["paginate"] = kwargs
            return [{"Contents": [{"Key": "p/a.tif"}]}, {}, {"Contents": [{"Key": "p/b.tif"}]}]

    class Client:
        def get_paginator(self, name: str):
            seen["operation"] = name
            return Paginator()

    def fake_client(service: str, config=None):
        seen["config"] = config
        return Client()

    monkeypatch.setattr(boto3, "client", fake_client)
    monkeypatch.delenv("AWS_NO_SIGN_REQUEST", raising=False)
    assert discovery._list_s3_keys("bkt", "p/") == ["p/a.tif", "p/b.tif"]
    assert seen["paginate"] == {"Bucket": "bkt", "Prefix": "p/", "Delimiter": "/"}
    assert seen["config"] is None
    monkeypatch.setenv("AWS_NO_SIGN_REQUEST", "YES")
    discovery._list_s3_keys("bkt", "p/")
    assert seen["config"] is not None


# --- discovery results ----------------------------------------------------------------------


def test_discover_dates_and_sorts_a_clean_series(tmp_path):
    touch(*(tmp_path / f"ndvi_{m}.tif" for m in reversed(MONTHS)))
    found = discover(str(tmp_path))
    assert found.problems == []
    assert found.undated == []
    assert [Path(e.uri).name for e in found.entries] == [f"ndvi_{m}.tif" for m in MONTHS]
    assert str(found.entries[0].time) == "2024-01-01T00:00:00.000"


def test_discover_reports_missing_ambiguous_impossible_and_duplicate_dates(tmp_path):
    touch(
        tmp_path / "ok_2024-01.tif",
        tmp_path / "nodate.tif",
        tmp_path / "both_20240215_2024-03.tif",
        tmp_path / "twin_2024-04-01.tif",
        tmp_path / "twin_copy_20240401.tif",
    )
    found = discover(str(tmp_path))
    by_name = {Path(p.uri).name: p.detail for p in found.problems}
    assert "no date in the file name" in by_name["nodate.tif"]
    assert "several dates" in by_name["both_20240215_2024-03.tif"]
    assert "'20240215' (2024-02-15)" in by_name["both_20240215_2024-03.tif"]
    assert "'2024-03' (2024-03-01)" in by_name["both_20240215_2024-03.tif"]
    assert "same date 2024-04-01 as" in by_name["twin_2024-04-01.tif"]
    assert "same date 2024-04-01 as" in by_name["twin_copy_20240401.tif"]
    assert "ok_2024-01.tif" not in by_name
    assert sorted(Path(u).name for u in found.undated) == [
        "both_20240215_2024-03.tif",
        "nodate.tif",
    ]


def test_discover_with_a_pattern_resolves_what_defaults_call_ambiguous(tmp_path):
    touch(tmp_path / "composite_20240215_2024-03.tif")
    assert discover(str(tmp_path)).problems  # ambiguous by default
    found = discover(str(tmp_path), "composite_%Y%m%d")
    assert found.problems == []
    assert [str(e.time) for e in found.entries] == ["2024-02-15T00:00:00.000"]


def test_discover_names_files_that_do_not_match_the_pattern_or_are_impossible(tmp_path):
    touch(tmp_path / "a_20241340.tif", tmp_path / "b_2024.tif")
    found = discover(str(tmp_path), "%Y%m%d")
    text = {Path(p.uri).name: p.detail for p in found.problems}
    assert "is not a real date" in text["a_20241340.tif"]
    assert "does not match --date-pattern '%Y%m%d'" in text["b_2024.tif"]


# --- manifest export ------------------------------------------------------------------------


@pytest.mark.parametrize("suffix", [".csv", ".json"])
def test_written_manifest_reads_back_to_the_same_entries(tmp_path, suffix):
    touch(*(tmp_path / "tifs" / f"ndvi_{m}.tif" for m in MONTHS))
    entries = discover(str(tmp_path / "tifs")).entries
    entries.append(Entry("s3://b/k.tif", np.datetime64("2024-06-01T10:30:15.250", "ms")))
    path = tmp_path / f"m{suffix}"
    write_manifest(entries, path)
    parsed = read_manifest(path)
    assert [(e.uri, e.time) for e in parsed.entries] == [(e.uri, e.time) for e in entries]


def test_manifest_export_refuses_a_wrong_suffix_or_an_existing_file(tmp_path):
    (tmp_path / "m.csv").write_text("keep me")
    with pytest.raises(FileExistsError, match="already exists"):
        write_manifest([], tmp_path / "m.csv")
    assert (tmp_path / "m.csv").read_text() == "keep me"
    with pytest.raises(ValueError, match=r"must be \.csv or \.json"):
        write_manifest([], tmp_path / "m.txt")


# --- converting a discovered series ---------------------------------------------------------

rasterio = pytest.importorskip("rasterio")
from rasterio.errors import RasterioIOError  # noqa: E402  (after importorskip)
from rasterio.transform import Affine  # noqa: E402


def write_tif(
    path: Path,
    array: np.ndarray,
    *,
    transform=TRANSFORM,
    nodata: int | None = 0,
    scales: list[float] | None = None,
    descriptions: list[str] | None = None,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=array.shape[1],
        width=array.shape[2],
        count=array.shape[0],
        dtype=array.dtype,
        crs=CRS,
        transform=Affine(*transform),
        nodata=nodata,
    ) as dst:
        dst.write(array)
        if scales:
            dst.scales = scales
        if descriptions:
            dst.descriptions = descriptions
    return path


@pytest.fixture
def truth() -> np.ndarray:
    return make_truth(N_TIME, N_BAND, HEIGHT, WIDTH)


@pytest.fixture
def series(tmp_path, truth) -> Path:
    """ndvi_2024-01.tif .. ndvi_2024-04.tif, written out of date order."""
    folder = tmp_path / "series"
    for t in (2, 0, 3, 1):
        write_tif(folder / f"ndvi_{MONTHS[t]}.tif", truth[t])
    return folder


def stored(path: Path) -> np.ndarray:
    return chronozarr.open_store(path).to_xarray().values


def test_a_directory_converts_without_a_manifest_and_is_bit_exact(tmp_path, series, truth):
    out = tmp_path / "store"
    report = convert(series, out, **OPTIONS)
    assert report.plan.discovered
    assert report.plan.source.kind == "manifest of COGs"
    assert np.array_equal(stored(out), truth)
    store = chronozarr.open_store(out)
    assert [str(t)[:7] for t in store.times] == MONTHS
    assert chronozarr.validate(out) == []


def test_a_glob_with_a_date_pattern_converts(tmp_path, truth):
    folder = tmp_path / "g"
    for t in range(N_TIME):
        write_tif(folder / f"v2_ndvi_{MONTHS[t].replace('-', '')}15_{t}-2024-12.tif", truth[t])
    out = tmp_path / "store"
    convert(str(folder / "*.tif"), out, date_pattern="ndvi_%Y%m%d", **OPTIONS)
    assert np.array_equal(stored(out), truth)
    assert str(chronozarr.open_store(out).times[0])[:10] == "2024-01-15"


def test_subdirectories_are_not_searched_unless_a_glob_says_so(tmp_path, series, truth):
    write_tif(series / "old" / "ndvi_2023-12.tif", truth[0])
    assert plan_conversion(series, sample=False).n_time == N_TIME
    assert plan_conversion(str(series / "**" / "*.tif"), sample=False).n_time == N_TIME + 1


def test_dry_run_lists_files_and_dates_and_writes_nothing(tmp_path, series):
    out = tmp_path / "store"
    plans = []
    report = convert(series, out, dry_run=True, on_plan=plans.append, **OPTIONS)
    assert report.encode is None
    assert not out.exists()
    assert not (tmp_path / "store.convert-work").exists()
    lines = plans[0].lines(list_files=True)
    assert "files:      4, dated from their names" in lines
    assert f"  2024-02-01  {series / 'ndvi_2024-02.tif'}" in lines
    assert not any(line.startswith("files:") for line in plans[0].lines())


def test_write_manifest_exports_what_was_found_and_converts_the_same(tmp_path, series, truth):
    manifest = tmp_path / "found.csv"
    convert(series, tmp_path / "unused", dry_run=True, write_manifest_to=manifest, **OPTIONS)
    assert [Path(e.uri).name for e in read_manifest(manifest).entries] == [
        f"ndvi_{m}.tif" for m in MONTHS
    ]
    from_dir, from_manifest = tmp_path / "a", tmp_path / "b"
    convert(series, from_dir, **OPTIONS)
    convert(manifest, from_manifest, **OPTIONS)
    assert np.array_equal(stored(from_dir), stored(from_manifest))
    assert np.array_equal(stored(from_dir), truth)


def test_write_manifest_is_checked_before_any_work_and_only_applies_to_discovery(tmp_path, series):
    existing = tmp_path / "m.csv"
    existing.write_text("keep")
    with pytest.raises(FileExistsError):
        convert(series, tmp_path / "o", dry_run=True, write_manifest_to=existing, **OPTIONS)
    assert existing.read_text() == "keep"
    manifest = tmp_path / "given.csv"
    write_manifest(discover(str(series)).entries, manifest)
    with pytest.raises(ValueError, match="already a manifest"):
        convert(manifest, tmp_path / "o", dry_run=True, write_manifest_to=tmp_path / "n.csv")
    assert not (tmp_path / "n.csv").exists()


def test_options_that_do_not_apply_to_discovery_are_refused(tmp_path, series):
    manifest = tmp_path / "given.csv"
    write_manifest(discover(str(series)).entries, manifest)
    with pytest.raises(ValueError, match="--date-pattern applies to a directory"):
        plan_conversion(manifest, date_pattern="%Y%m", sample=False)
    with pytest.raises(ValueError, match="not manifests or GeoTIFFs"):
        plan_conversion(series, variable="v", sample=False)


# --- preflight: every problem at once -------------------------------------------------------


def test_preflight_collects_every_problem_grouped_per_file_and_writes_nothing(tmp_path, truth):
    folder = tmp_path / "bad"
    write_tif(folder / "a_2024-01.tif", truth[0])  # the reference
    write_tif(folder / "b_2024-02.tif", truth[1], scales=[0.5, 1.0])
    write_tif(folder / "c_2024-03.tif", truth[2][:1])  # one band
    shifted = list(TRANSFORM)
    shifted[2] += 50.0
    write_tif(folder / "d_2024-04.tif", truth[3], transform=tuple(shifted))
    write_tif(folder / "e_2024-05.tif", truth[0].astype(np.uint8))
    write_tif(folder / "nodate.tif", truth[0])
    write_tif(folder / "f_2024-06.tif", truth[0], scales=[2.0, 1.0], transform=tuple(shifted))
    (folder / "g_2024-07.tif").write_text("not a raster")
    write_tif(folder / "h_2024-08.tif", truth[0])
    write_tif(folder / "h_copy_20240801.tif", truth[0])
    out = tmp_path / "store"

    with pytest.raises(PreflightError) as error:
        convert(folder, out, **OPTIONS)
    assert not out.exists()
    assert not (tmp_path / "store.convert-work").exists()

    by_file: dict[str, list[str]] = {}
    for problem in error.value.problems:
        by_file.setdefault(Path(problem.uri).name, []).append(problem.detail)
        assert problem.fix, problem
    joined = {name: " | ".join(details) for name, details in by_file.items()}
    assert "scales [0.5, 1.0]" in joined["b_2024-02.tif"]
    assert "has 1 bands; " in joined["c_2024-03.tif"]
    assert "not on the target grid" in joined["d_2024-04.tif"]
    assert "is uint8" in joined["e_2024-05.tif"]
    assert "no date in the file name" in joined["nodate.tif"]
    assert "scales [2.0, 1.0]" in joined["f_2024-06.tif"]
    assert "not on the target grid" in joined["f_2024-06.tif"]  # two findings, one file
    assert "cannot open source" in joined["g_2024-07.tif"]
    assert "same date 2024-08-01" in joined["h_2024-08.tif"]
    assert "same date 2024-08-01" in joined["h_copy_20240801.tif"]
    assert "a_2024-01.tif" not in joined

    message = str(error.value)
    assert message.startswith(f"{len(error.value.problems)} problem(s) in 9 of 10 source file(s)")
    assert "nothing was written" in message
    assert message.count("\n  - ") == len(error.value.problems)
    assert message.index("b_2024-02.tif") < message.index("c_2024-03.tif")


def test_mixed_grids_name_every_off_grid_file_and_warp_when_asked(tmp_path, series, truth):
    shifted = list(TRANSFORM)
    shifted[2] += 50.0
    write_tif(series / "ndvi_2024-02.tif", truth[1], transform=tuple(shifted))
    write_tif(series / "ndvi_2024-03.tif", truth[2], transform=tuple(shifted))
    with pytest.raises(PreflightError) as error:
        plan_conversion(series, sample=False)
    named = sorted(Path(p.uri).name for p in error.value.problems)
    assert named == ["ndvi_2024-02.tif", "ndvi_2024-03.tif"]
    assert all("--resampling" in (p.fix or "") for p in error.value.problems)

    plan = plan_conversion(series, resampling="nearest", sample=False)
    assert plan.warped == 2


def test_an_empty_listing_is_not_a_preflight_error(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="no GeoTIFF files") as error:
        convert(empty, tmp_path / "o", dry_run=True)
    assert not isinstance(error.value, PreflightError)


def test_all_files_unreadable_still_reports_every_one(tmp_path):
    folder = tmp_path / "junk"
    for month in MONTHS:
        (folder).mkdir(exist_ok=True)
        (folder / f"x_{month}.tif").write_text("junk")
    with pytest.raises(PreflightError) as error:
        plan_conversion(folder, sample=False)
    assert len(error.value.problems) == len(MONTHS)


def test_declared_nodata_that_differs_between_files_is_a_plan_note(tmp_path, truth):
    folder = tmp_path / "nd"
    for t in range(3):
        write_tif(folder / f"x_{MONTHS[t]}.tif", truth[t], nodata=0 if t < 2 else 65535)
    plan = plan_conversion(folder, sample=False)
    note = next(line for line in plan.lines() if line.startswith("nodata:"))
    assert "[0, 0]" in note
    assert "[65535, 65535]" in note
    assert "x_2024-03.tif" in note
    assert "mask" in plan.source.info.validity
    explicit = plan_conversion(folder, nodata=0, sample=False)
    assert not any(line.startswith("nodata:") for line in explicit.lines())


# --- S3 prefix without network --------------------------------------------------------------


@pytest.fixture
def fake_s3(monkeypatch, series):
    """An s3:// prefix whose objects are the files of `series`; `denied` keys cannot be opened."""
    denied: set[str] = set()
    real_open = rasterio.open

    def keys(bucket: str, prefix: str) -> list[str]:
        assert (bucket, prefix) == ("bkt", "ndvi/")
        return [f"ndvi/{p.name}" for p in sorted(series.iterdir())]

    def fake_open(uri, *args, **kwargs):
        if isinstance(uri, str) and uri.startswith("s3://"):
            name = uri.rsplit("/", 1)[-1]
            if uri in denied:
                raise RasterioIOError(f"{uri}: HTTP response code: 403 (AccessDenied)")
            uri = str(series / name)
        return real_open(uri, *args, **kwargs)

    monkeypatch.setattr(discovery, "_list_s3_keys", keys)
    monkeypatch.setattr(rasterio, "open", fake_open)
    return denied


def test_an_s3_prefix_converts_like_a_directory(tmp_path, truth, fake_s3):
    out = tmp_path / "store"
    report = convert("s3://bkt/ndvi", out, **OPTIONS)
    assert report.plan.discovered
    assert np.array_equal(stored(out), truth)
    source = report.plan.source
    assert isinstance(source, CogManifestSource)
    assert all(e.uri.startswith("s3://bkt/ndvi/") for e in source.entries)


def test_an_inaccessible_s3_object_is_named_with_a_fix_next_to_the_other_problems(
    tmp_path, fake_s3
):
    fake_s3.add("s3://bkt/ndvi/ndvi_2024-03.tif")
    out = tmp_path / "store"
    with pytest.raises(PreflightError) as error:
        convert("s3://bkt/ndvi/", out, **OPTIONS)
    assert not out.exists()
    (problem,) = error.value.problems
    assert problem.uri == "s3://bkt/ndvi/ndvi_2024-03.tif"
    assert "AccessDenied" in problem.detail
    assert problem.fix is not None
    assert "AWS_PROFILE" in problem.fix
    assert "AWS_NO_SIGN_REQUEST" in problem.fix


# --- command line ---------------------------------------------------------------------------


def run(*args: str):
    return CliRunner().invoke(main, list(args), catch_exceptions=False)


def test_cli_convert_a_directory_dry_run_then_for_real(tmp_path, series, truth):
    out = tmp_path / "store"
    manifest = tmp_path / "found.json"
    dry = run(
        "convert", str(series), str(out), "--dry-run", "--write-manifest", str(manifest),
        "--chunk-size", "16",
    )  # fmt: skip
    assert dry.exit_code == 0, dry.output
    assert "files:      4, dated from their names" in dry.output
    assert f"  2024-04-01  {series / 'ndvi_2024-04.tif'}" in dry.output
    assert f"wrote manifest {manifest}" in dry.output
    assert "dry run: nothing was written" in dry.output
    assert not out.exists()

    real = run("convert", str(series), str(out), "--chunk-size", "16")
    assert real.exit_code == 0, real.output
    assert "files:" not in real.output
    assert np.array_equal(stored(out), truth)


def test_cli_dry_run_reports_all_problems_and_exits_1(tmp_path, series, truth):
    write_tif(series / "ndvi_2024-02.tif", truth[1], scales=[0.5, 1.0])
    write_tif(series / "readme_nodate.tif", truth[0])
    out = tmp_path / "store"
    result = run("convert", str(series), str(out), "--dry-run")
    assert result.exit_code == 1
    assert "2 problem(s) in 2 of 5 source file(s); nothing was written" in result.output
    assert "no date in the file name" in result.output
    assert "scales [0.5, 1.0]" in result.output
    assert "fix: " in result.output
    assert not out.exists()


def test_cli_date_pattern_and_help(tmp_path, series):
    result = run("convert", str(series / "*.tif"), str(tmp_path / "o"), "--date-pattern", "%Y")
    assert result.exit_code == 1
    assert "--date-pattern '%Y' needs at least %Y and %m" in result.output
    help_text = run("convert", "--help").output
    for fragment in (
        "directory, quoted glob or s3:// prefix",
        "--date-pattern",
        "--write-manifest",
    ):
        assert fragment in " ".join(help_text.split())


def test_cli_encode_points_files_at_convert(tmp_path, series):
    manifest = tmp_path / "m.csv"
    write_manifest(discover(str(series)).entries, manifest)
    for source in (str(manifest), str(series), "s3://bkt/ndvi/"):
        result = run("encode", source, str(tmp_path / "o"))
        assert result.exit_code == 1
        assert f"Use `chronozarr convert {source} " in result.output
    assert not (tmp_path / "o").exists()
    assert "chronozarr convert" in run("encode", "--help").output
