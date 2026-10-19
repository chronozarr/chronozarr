"""The CRS of a store: any representation proj v0.1 allows, read with its precedence.

Stores are rewritten in place from an encoded EPSG store, so each case changes only the CRS
metadata and the values must read back unchanged.
"""

from __future__ import annotations

import json
import re
import shutil
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import xarray as xr
import zarr
from click.testing import CliRunner
from zarr.errors import ZarrUserWarning

import chronozarr
from chronozarr import schema
from chronozarr.cli import main
from chronozarr.crs import Crs
from tests.crs_samples import LCC_WKT2, SINUSOIDAL_WKT2
from tests.narrow import array_at, group_at
from tests.synthetic import build_store, make_truth

pytestmark = pytest.mark.unit

LEVELS = ("0", "1", "2")
PLANES = ("data", "mask", "coverage")
ALIASES = ("crs", "_CRS", "proj:code", "proj:wkt2", "proj:projjson")


@pytest.fixture(scope="module")
def good_store(tmp_path_factory):
    path = tmp_path_factory.mktemp("crs") / "store"
    truth = make_truth(3, 2, 64, 64)
    mask = np.ones((3, 64, 64), dtype="uint8")
    mask[:, :8, :8] = 0
    build_store(path, truth, shard=False, chunk_size=16, mask=mask, coverage=mask * 2, nodata=None)
    return path


@pytest.fixture
def store_copy(good_store, tmp_path):
    copy = tmp_path / "copy"
    shutil.copytree(good_store, copy)
    return copy


@pytest.fixture(scope="module")
def pyproj_crs():
    """A pyproj CRS, built from the code the stores are written with."""
    pyproj = pytest.importorskip("pyproj")
    return pyproj.CRS


def rewrite(
    path: Path,
    *,
    arrays: Callable[[dict[str, Any]], None] | None = None,
    groups: Callable[[dict[str, Any]], None] | None = None,
    block: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    """Edit the attributes of every data-like array, level group and the chronozarr block."""
    root = zarr.open_group(str(path), mode="r+", zarr_format=3, use_consolidated=False)
    for level in (k for k in LEVELS if k in root):
        group = group_at(root, level)
        if groups is not None:
            edited = group.attrs.asdict()
            groups(edited)
            group.attrs.put(edited)
        for name in (n for n in PLANES if n in group):
            array = array_at(group, name)
            edited = array.attrs.asdict()
            if arrays is not None:
                arrays(edited)
            array.attrs.put(edited)
    if block is not None:
        edited = root.attrs["chronozarr"]
        assert isinstance(edited, dict)
        block(edited)
        root.attrs["chronozarr"] = edited
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ZarrUserWarning)  # consolidated metadata is deliberate
        zarr.consolidate_metadata(str(path))


def only(**representation: Any) -> Callable[[dict[str, Any]], None]:
    """Array edit: drop every CRS attribute, then set the given proj: ones (`wkt2=...`)."""

    def edit(attrs: dict[str, Any]) -> None:
        for key in ALIASES:
            attrs.pop(key, None)
        for key, value in representation.items():
            attrs[f"proj:{key}"] = value

    return edit


def drop_mirrors(attrs: dict[str, Any]) -> None:
    attrs.pop("crs", None)


def set_version(version: str) -> Callable[[dict[str, Any]], None]:
    def edit(attrs: dict[str, Any]) -> None:
        attrs["spec_version"] = version

    return edit


def stored_values(path: Path) -> np.ndarray:
    return chronozarr.open_store(path).to_xarray().values


def make_non_epsg(path: Path, wkt2: str = LCC_WKT2) -> None:
    """The store as a 0.4.0 writer would leave it: WKT2 only, no mirror, no `_CRS` URL."""

    def block(attrs: dict[str, Any]) -> None:
        drop_mirrors(attrs)
        attrs["spec_version"] = "0.4.0"

    rewrite(path, arrays=only(wkt2=wkt2), groups=drop_mirrors, block=block)


# --- Reading any representation -------------------------------------------------------------


@pytest.mark.parametrize("form", ["wkt2", "projjson"])
def test_store_with_the_crs_as_wkt2_or_projjson_only_opens_and_conforms(
    good_store, store_copy, pyproj_crs, form
):
    """What the 0.3.0 mirrors say (EPSG:32631) agrees with the stored definition."""
    utm = pyproj_crs.from_epsg(32631)
    definition = utm.to_wkt("WKT2_2019") if form == "wkt2" else json.loads(utm.to_json())
    expected = stored_values(good_store)
    rewrite(store_copy, arrays=only(**{form: definition}))

    assert chronozarr.validate(store_copy) == []
    opened = chronozarr.open_store(store_copy)
    assert opened.crs.code is None
    assert getattr(opened.crs, form) == definition
    assert opened.attrs.crs == "EPSG:32631"
    np.testing.assert_array_equal(opened.to_xarray().values, expected)


@pytest.mark.parametrize("wkt2", [LCC_WKT2, SINUSOIDAL_WKT2], ids=["lcc", "sinusoidal"])
def test_store_without_an_epsg_code_or_any_mirror_opens_and_conforms(good_store, store_copy, wkt2):
    expected = stored_values(good_store)
    make_non_epsg(store_copy, wkt2)

    assert chronozarr.validate(store_copy) == []
    assert schema.validate_report(store_copy).notes == []
    opened = chronozarr.open_store(store_copy)
    assert opened.attrs.spec_version == "0.4.0"
    assert opened.attrs.crs is None
    assert (opened.crs.code, opened.crs.wkt2, opened.crs.projjson) == (None, wkt2, None)
    assert opened.crs.to_string() == wkt2
    cube = opened.to_xarray()
    assert cube.attrs["crs"] == wkt2
    np.testing.assert_array_equal(cube.values, expected)
    assert opened.read_mask(0) is not None
    assert opened.read_coverage(0) is not None


def test_xarray_engine_opens_a_store_without_an_epsg_code(good_store, store_copy):
    expected = xr.open_dataset(good_store, engine="chronozarr", physical=False)["data"].values
    make_non_epsg(store_copy)
    dataset = xr.open_dataset(store_copy, engine="chronozarr", physical=False)
    assert dataset.attrs["crs"] == LCC_WKT2
    np.testing.assert_array_equal(dataset["data"].values, expected)


def test_info_names_the_wkt2_instead_of_printing_it(store_copy):
    make_non_epsg(store_copy)
    result = CliRunner().invoke(main, ["info", str(store_copy)])
    assert result.exit_code == 0, result.output
    line = next(ln for ln in result.stdout.splitlines() if ln.startswith("crs:"))
    assert line.split(maxsplit=1)[1] == 'WKT2 "unknown"'
    assert "chronozarr 0.4.0" in result.stdout


def test_info_prints_the_code_when_there_is_one(good_store):
    result = CliRunner().invoke(main, ["info", str(good_store)])
    assert "crs:       EPSG:32631" in result.stdout


@pytest.mark.parametrize("version", ["0.3.0", "0.4.0"])
def test_both_spec_versions_open(store_copy, version):
    rewrite(store_copy, block=set_version(version))
    assert chronozarr.open_store(store_copy).attrs.spec_version == version
    assert chronozarr.validate(store_copy) == []


def test_another_spec_version_is_refused_and_names_the_ones_accepted(store_copy):
    rewrite(store_copy, block=set_version("0.5.0"))
    with pytest.raises(schema.SchemaError, match=r"0\.3\.0.*0\.4\.0.*chronozarr convert"):
        chronozarr.open_store(store_copy)
    (problem,) = chronozarr.validate(store_copy)
    assert "'0.5.0'" in problem


def test_a_store_with_no_crs_at_all_is_refused_with_the_attributes_to_add(store_copy):
    rewrite(store_copy, arrays=only())
    message = "needs one of proj:code, proj:wkt2 or proj:projjson"
    with pytest.raises(schema.SchemaError, match=message):
        chronozarr.open_store(store_copy)
    assert any(message in p for p in chronozarr.validate(store_copy))


@pytest.mark.parametrize(
    ("attrs", "message"),
    [
        ({"proj:code": "32631"}, "proj:code must be 'authority:code'"),
        ({"proj:code": 32631}, "proj:code must be 'authority:code'"),
        ({"proj:wkt2": ""}, "proj:wkt2 must be a non-empty string"),
        ({"proj:wkt2": ["PROJCRS"]}, "proj:wkt2 must be a non-empty string"),
        ({"proj:projjson": "{}"}, "proj:projjson must be an object"),
    ],
)
def test_malformed_representations_are_refused(attrs, message):
    with pytest.raises(schema.SchemaError, match=message):
        schema.parse_crs(attrs, "level 0/data")


def test_null_representations_count_as_absent():
    parsed = schema.parse_crs({"proj:code": None, "proj:wkt2": LCC_WKT2}, "level 0/data")
    assert parsed == Crs(wkt2=LCC_WKT2)


# --- Precedence -----------------------------------------------------------------------------


def test_wkt2_wins_over_the_code_when_they_disagree(store_copy, pyproj_crs):
    """proj v0.1: on a discrepancy proj:wkt2 takes precedence; validate surfaces it."""
    other = pyproj_crs.from_epsg(32632).to_wkt("WKT2_2019")
    rewrite(
        store_copy,
        arrays=lambda a: a.update({"proj:wkt2": other}),
        groups=drop_mirrors,
        block=drop_mirrors,
    )
    opened = chronozarr.open_store(store_copy)
    with pytest.warns(UserWarning, match="describe different CRSs.*Ask whoever wrote"):
        stored = opened.crs
    assert (stored.code, stored.wkt2) == (None, other)
    assert stored.to_string() == other
    problems = chronozarr.validate(store_copy)
    assert any(
        "proj:code 'EPSG:32631' and proj:wkt2 describe different CRSs (compared by pyproj)" in p
        for p in problems
    )


# --- Mirrors --------------------------------------------------------------------------------


def test_the_crs_mirrors_are_optional_and_noted(store_copy):
    rewrite(store_copy, groups=drop_mirrors, block=drop_mirrors)
    opened = chronozarr.open_store(store_copy)
    assert opened.attrs.crs is None
    assert opened.crs.code == "EPSG:32631"
    report = schema.validate_report(store_copy)
    assert report.problems == []
    assert any("crs mirror" in n and "EPSG:32631" in n for n in report.notes)


@pytest.mark.parametrize("pyproj_installed", [True, False], ids=["pyproj", "no-pyproj"])
def test_a_mirror_that_contradicts_the_stored_crs_is_refused(
    store_copy, monkeypatch, pyproj_installed
):
    """Two different codes are told apart by pyproj when it is installed, else by the codes."""
    if not pyproj_installed:
        monkeypatch.setattr("chronozarr.crs._pyproj", lambda: None)
    method = "pyproj" if pyproj_installed else "code"
    rewrite(store_copy, block=lambda a: a.update(crs="EPSG:32632"))
    message = rf"chronozarr\.crs 'EPSG:32632' differs from the stored CRS \(compared by {method}\)"
    with pytest.raises(schema.SchemaError, match=message):
        chronozarr.open_store(store_copy)
    assert any(re.search(message, p) for p in chronozarr.validate(store_copy))


def test_a_mirror_is_compared_with_a_wkt2_only_store_by_pyproj(store_copy, pyproj_crs):
    other = pyproj_crs.from_epsg(32632).to_wkt("WKT2_2019")
    rewrite(store_copy, arrays=only(wkt2=other))
    message = r"chronozarr\.crs 'EPSG:32631' differs from the stored CRS \(compared by pyproj\)"
    with pytest.raises(schema.SchemaError, match=message):
        chronozarr.open_store(store_copy)
    assert any(re.search(message, p) for p in chronozarr.validate(store_copy))


def test_without_pyproj_an_unverifiable_mirror_is_a_note_not_a_failure(
    store_copy, pyproj_crs, monkeypatch
):
    utm = pyproj_crs.from_epsg(32631).to_wkt("WKT2_2019")
    rewrite(store_copy, arrays=only(wkt2=utm))
    monkeypatch.setattr("chronozarr.crs._pyproj", lambda: None)
    chronozarr.open_store(store_copy)
    report = schema.validate_report(store_copy)
    assert report.problems == []
    assert any("not compared" in n and "pyproj" in n for n in report.notes)


def test_a_030_store_without_an_epsg_code_is_noted_for_released_readers(store_copy):
    rewrite(store_copy, arrays=only(wkt2=LCC_WKT2), groups=drop_mirrors, block=drop_mirrors)
    report = schema.validate_report(store_copy)
    assert report.problems == []
    assert any("0.3.0" in n and "0.4.0" in n for n in report.notes)


def test_a_conforming_epsg_store_has_no_notes(good_store):
    report = schema.validate_report(good_store)
    assert (report.problems, report.notes) == ([], [])


def test_validate_command_names_the_version_of_the_store(store_copy):
    make_non_epsg(store_copy)
    result = CliRunner().invoke(main, ["validate", str(store_copy)])
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == f"{store_copy}: conforms to chronozarr 0.4.0"


def test_validate_command_prints_notes_for_a_store_with_a_mirror_missing(store_copy):
    rewrite(store_copy, groups=drop_mirrors, block=drop_mirrors)
    result = CliRunner().invoke(main, ["validate", str(store_copy)])
    assert result.exit_code == 0, result.output
    assert "note: " in result.stdout
    assert "conforms to chronozarr 0.3.0" in result.stdout


def test_other_geometry_checks_still_apply_to_a_store_without_an_epsg_code(store_copy):
    make_non_epsg(store_copy)
    root = zarr.open_group(str(store_copy), mode="r+", zarr_format=3, use_consolidated=False)
    group_at(root, "1").attrs["transform"] = [30.0, 0.0, 746090.0, 0.0, -30.0, 2540440.0]
    assert any("not the level-0 transform scaled" in p for p in chronozarr.validate(store_copy))
