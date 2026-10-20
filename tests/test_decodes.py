"""Readers decode what they can; `validate` still says what does not conform.

Each case rewrites a copy of an encoded store, the way the strip tests of the 2026-10 review did,
so only the metadata changes and the stored values must read back exactly. A deviation that does
not change a value is read through and reported as a note on the opened store (`ChronoStore.notes`)
and by `validate_report`. `validate` fails where the spec says MUST and passes with a note where
it says SHOULD.
"""

from __future__ import annotations

import json
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
from tests.narrow import array_at, attrs_block, group_at
from tests.synthetic import build_store, make_truth

pytestmark = pytest.mark.unit

LEVELS = ("0", "1", "2")
PLANES = ("data", "mask", "coverage")
COORDINATES = ("time", "band", "x", "y")
Edit = Callable[[dict[str, Any]], None]


@pytest.fixture(scope="module", params=[False, True], ids=["unsharded", "sharded"])
def good_store(request, tmp_path_factory):
    path = tmp_path_factory.mktemp("decodes") / "store"
    truth = make_truth(3, 2, 64, 64)
    mask = np.ones((3, 64, 64), dtype="uint8")
    mask[:, :8, :8] = 0
    build_store(
        path, truth, shard=request.param, chunk_size=16, mask=mask, coverage=mask * 2, nodata=None
    )
    return path


@pytest.fixture
def store_copy(good_store, tmp_path):
    copied = tmp_path / "copy"
    shutil.copytree(good_store, copied)
    return copied


def rewrite(
    path: Path,
    *,
    root: Edit | None = None,
    block: Edit | None = None,
    arrays: Edit | None = None,
    coordinates: Edit | None = None,
    groups: Edit | None = None,
) -> None:
    """Edit attributes: the root group, its chronozarr block, and per level group and array."""
    store = zarr.open_group(str(path), mode="r+", zarr_format=3, use_consolidated=False)
    if root is not None:
        edited = store.attrs.asdict()
        root(edited)
        store.attrs.put(edited)
    if block is not None:
        edited = attrs_block(store, "chronozarr")
        block(edited)
        store.attrs["chronozarr"] = edited
    for level in (k for k in LEVELS if k in store):
        group = group_at(store, level)
        if groups is not None:
            edited = group.attrs.asdict()
            groups(edited)
            group.attrs.put(edited)
        for names, edit in ((PLANES, arrays), (COORDINATES, coordinates)):
            for name in (n for n in names if n in group):
                array = array_at(group, name)
                edited = array.attrs.asdict()
                if edit is not None:
                    edit(edited)
                array.attrs.put(edited)
    consolidate(path)


def consolidate(path: Path) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ZarrUserWarning)  # consolidated metadata is deliberate
        zarr.consolidate_metadata(str(path))


def drop_arrays(path: Path, names: tuple[str, ...]) -> None:
    for level in LEVELS:
        for name in names:
            shutil.rmtree(path / level / name)
    consolidate(path)


def stored(path: Path) -> np.ndarray:
    return chronozarr.open_store(path).to_xarray().values


def pop(*keys: str) -> Edit:
    def edit(attrs: dict[str, Any]) -> None:
        for key in keys:
            attrs.pop(key, None)

    return edit


def together(*edits: Edit) -> Edit:
    """One edit that applies each of `edits` in turn."""

    def edit(attrs: dict[str, Any]) -> None:
        for each in edits:
            each(attrs)

    return edit


def set_key(**values: Any) -> Edit:
    def edit(attrs: dict[str, Any]) -> None:
        attrs.update(values)

    return edit


def resample(method: str) -> Edit:
    def edit(attrs: dict[str, Any]) -> None:
        attrs["multiscales"]["resampling_method"] = method

    return edit


def upstream_registrations(attrs: dict[str, Any]) -> None:
    """Register the conventions as the EOPF Sentinel-2 product does: v1 tags, `geo-proj`."""
    for entry in attrs["zarr_conventions"]:
        name = entry.get("name")
        if name not in ("multiscales", "proj", "spatial"):
            continue
        repo = "zarr-experimental/geo-proj" if name == "proj" else f"zarr-conventions/{name}"
        entry["schema_url"] = f"https://raw.githubusercontent.com/{repo}/refs/tags/v1/schema.json"
        entry["spec_url"] = f"https://github.com/{repo}/blob/v1/README.md"
        if name != "multiscales":
            entry["name"] = f"{name}:"


def has(texts: list[str] | tuple[str, ...], *fragments: str) -> bool:
    """Whether one text holds every fragment."""
    return any(all(f in text for f in fragments) for text in texts)


# --- What a conforming store reports ---------------------------------------------------------


def test_a_conforming_store_has_no_notes(good_store):
    assert chronozarr.open_store(good_store).notes == ()
    assert "chronozarr_notes" not in xr.open_dataset(good_store, engine="chronozarr").attrs


# --- Registrations ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["multiscales", "proj", "spatial"])
def test_a_registration_is_matched_by_uuid_whatever_its_urls_say(store_copy, good_store, name):
    def respell(attrs: dict[str, Any]) -> None:
        for entry in attrs["zarr_conventions"]:
            if entry["name"] == name:
                entry["schema_url"] = entry["schema_url"].replace("refs/tags/v0.1", "main")

    rewrite(store_copy, root=respell if name == "multiscales" else None)
    if name != "multiscales":
        rewrite(store_copy, arrays=respell)
    opened = chronozarr.open_store(store_copy)
    assert has(opened.notes, f"{name} registration", "UUID")
    np.testing.assert_array_equal(opened.to_xarray().values, stored(good_store))
    assert any(f"{name} registration" in p for p in chronozarr.validate(store_copy))


def test_registrations_as_the_upstream_convention_repositories_spell_them(store_copy, good_store):
    rewrite(store_copy, root=upstream_registrations, arrays=upstream_registrations)
    opened = chronozarr.open_store(store_copy)
    assert has(opened.notes, "proj registration", "geo-proj")
    assert has(opened.notes, "spatial registration", "'spatial:'")
    assert has(opened.notes, "multiscales registration", "refs/tags/v1")
    np.testing.assert_array_equal(opened.to_xarray().values, stored(good_store))
    problems = chronozarr.validate(store_copy)
    assert any("proj registration" in p for p in problems)
    assert any("multiscales registration" in p for p in problems)


def test_a_store_that_registers_no_conventions_is_read_through_its_attributes(
    store_copy, good_store
):
    rewrite(
        store_copy,
        root=lambda a: a.update(zarr_conventions=[]),
        arrays=pop("zarr_conventions"),
    )
    opened = chronozarr.open_store(store_copy)
    assert has(opened.notes, "no multiscales registration")
    assert has(opened.notes, "no proj registration")
    assert has(opened.notes, "no spatial registration")
    assert has(opened.notes, "no chronozarr registration")
    np.testing.assert_array_equal(opened.to_xarray().values, stored(good_store))
    problems = chronozarr.validate(store_copy)
    assert any("no proj registration" in p for p in problems)


def test_the_stored_geometry_is_still_read_when_registrations_are_spelled_otherwise(store_copy):
    rewrite(store_copy, root=upstream_registrations, arrays=upstream_registrations)
    opened = chronozarr.open_store(store_copy)
    assert opened.crs.code == "EPSG:32631"
    assert opened.levels[0].transform == (10.0, 0.0, 746090.0, 0.0, -10.0, 2540440.0)


# --- Overview method -------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["nearest", None], ids=["nearest", "undeclared"])
def test_another_resampling_method_is_read_and_the_overviews_are_flagged(
    store_copy, good_store, method
):
    def relabel(attrs: dict[str, Any]) -> None:
        if method is None:
            attrs["multiscales"].pop("resampling_method")
        else:
            attrs["multiscales"]["resampling_method"] = method

    rewrite(store_copy, root=relabel)
    opened = chronozarr.open_store(store_copy)
    assert has(opened.notes, "resampling_method", "not chronozarr's", "level 0")
    for level in range(len(opened.levels)):
        np.testing.assert_array_equal(
            opened.read(1, level), chronozarr.open_store(good_store).read(1, level)
        )
    assert any("resampling_method must be average" in p for p in chronozarr.validate(store_copy))


# --- Spec versions ---------------------------------------------------------------------------


@pytest.mark.parametrize("version", ["0.3.1", "0.3.12", "0.4.1", "0.4.7"])
def test_a_patch_version_of_a_known_minor_is_read_and_noted(store_copy, good_store, version):
    rewrite(store_copy, block=lambda b: b.update(spec_version=version))
    opened = chronozarr.open_store(store_copy)
    assert opened.attrs.spec_version == version
    assert has(opened.notes, version, "0.3.0", "0.4.0")
    np.testing.assert_array_equal(opened.to_xarray().values, stored(good_store))
    problems = chronozarr.validate(store_copy)
    assert any(f"'{version}'" in p for p in problems)


@pytest.mark.parametrize(
    "version", ["0.2.0", "0.5.0", "0.30.0", "1.0.0", "0.3", "v0.3.0", 3, None]
)
def test_any_other_version_is_refused_naming_the_versions_accepted(store_copy, version):
    rewrite(store_copy, block=lambda b: b.update(spec_version=version))
    with pytest.raises(schema.SchemaError, match=r"0\.3\.0 or 0\.4\.0.*chronozarr convert"):
        chronozarr.open_store(store_copy)


# --- The data variable's name ----------------------------------------------------------------


def test_the_variable_defaults_to_data_with_a_note(store_copy, good_store):
    rewrite(store_copy, block=pop("variable"))
    opened = chronozarr.open_store(store_copy)
    assert opened.attrs.variable == "data"
    assert has(opened.notes, "chronozarr.variable", "'data'")
    np.testing.assert_array_equal(opened.to_xarray().values, stored(good_store))
    assert any("chronozarr.variable" in p for p in chronozarr.validate(store_copy))


# --- Dimension names -------------------------------------------------------------------------


def test_array_dimensions_may_be_left_out_when_dimension_names_exist(store_copy, good_store):
    every = pop("_ARRAY_DIMENSIONS")
    rewrite(store_copy, arrays=every, coordinates=every)
    opened = chronozarr.open_store(store_copy)
    assert has(opened.notes, "_ARRAY_DIMENSIONS", "dimension_names")
    np.testing.assert_array_equal(opened.to_xarray().values, stored(good_store))
    report = schema.validate_report(store_copy)
    assert report.problems == []
    assert has(report.notes, "_ARRAY_DIMENSIONS")


def test_array_dimensions_that_contradict_the_dimension_names_are_refused(store_copy):
    rewrite(store_copy, arrays=lambda a: a.update(_ARRAY_DIMENSIONS=["time", "y", "x", "band"]))
    with pytest.raises(schema.SchemaError, match="_ARRAY_DIMENSIONS must be"):
        chronozarr.open_store(store_copy)
    assert any("_ARRAY_DIMENSIONS must be" in p for p in chronozarr.validate(store_copy))


def test_a_data_array_without_dimension_names_is_refused(store_copy):
    path = store_copy / "0" / "data" / "zarr.json"
    document = json.loads(path.read_text())
    del document["dimension_names"]
    path.write_text(json.dumps(document))
    consolidate(store_copy)
    with pytest.raises(schema.SchemaError, match="dimension_names must be"):
        chronozarr.open_store(store_copy)


# --- Mirrors ---------------------------------------------------------------------------------


def test_the_levels_and_band_names_mirrors_may_be_left_out(store_copy, good_store):
    rewrite(store_copy, block=pop("levels", "band_names"))
    opened = chronozarr.open_store(store_copy)
    assert opened.bands == ("B04", "B08")
    assert has(opened.notes, "chronozarr.levels")
    assert has(opened.notes, "chronozarr.band_names")
    np.testing.assert_array_equal(opened.to_xarray().values, stored(good_store))
    report = schema.validate_report(store_copy)
    assert report.problems == []
    assert has(report.notes, "chronozarr.levels")
    assert has(report.notes, "chronozarr.band_names")


def test_the_level_group_mirrors_may_be_left_out(store_copy, good_store):
    rewrite(store_copy, groups=pop("crs", "transform", "resolution"))
    opened = chronozarr.open_store(store_copy)
    assert has(opened.notes, "transform", "resolution", "level group")
    np.testing.assert_array_equal(opened.to_xarray().values, stored(good_store))
    assert opened.levels[1].resolution == 20.0
    report = schema.validate_report(store_copy)
    assert report.problems == []
    assert has(report.notes, "transform", "resolution", "level group")


def test_a_level_group_without_mirrors_is_still_checked_against_its_array(store_copy):
    rewrite(store_copy, groups=pop("crs", "transform", "resolution"))
    root = zarr.open_group(str(store_copy), mode="r+", zarr_format=3, use_consolidated=False)
    array_at(root, "1/data").attrs["spatial:transform"] = [
        30.0,
        0.0,
        746090.0,
        0.0,
        -30.0,
        2540440.0,
    ]
    consolidate(store_copy)
    assert any("not the level-0 transform scaled" in p for p in chronozarr.validate(store_copy))


def test_a_mirror_that_is_present_and_wrong_is_still_refused(store_copy):
    rewrite(store_copy, block=lambda b: b.update(band_names=["B08", "B04"]))
    with pytest.raises(schema.SchemaError, match="band_names"):
        chronozarr.open_store(store_copy)
    assert any("band_names" in p for p in chronozarr.validate(store_copy))


# --- Coordinate arrays and the time attributes -----------------------------------------------


def test_coordinate_arrays_may_be_left_out(store_copy, good_store):
    drop_arrays(store_copy, COORDINATES)
    opened = chronozarr.open_store(store_copy)
    np.testing.assert_array_equal(opened.to_xarray().values, stored(good_store))
    assert opened.to_xarray().coords["time"].values.tolist() == [
        t.astype("datetime64[ns]").tolist() for t in opened.times
    ]
    report = schema.validate_report(store_copy)
    assert report.problems == []
    assert has(report.notes, "coordinate arrays", "time", "band", "x", "y")


def test_the_time_attributes_may_be_left_out_but_not_changed(store_copy):
    rewrite(store_copy, coordinates=lambda a: a.pop("units", None) if "calendar" in a else None)
    report = schema.validate_report(store_copy)
    assert report.problems == []
    assert has(report.notes, "time", "units")
    rewrite(
        store_copy,
        coordinates=lambda a: a.update(units="days since 1970-01-01") if "calendar" in a else None,
    )
    assert any("time: units must be" in p for p in chronozarr.validate(store_copy))


# --- Everything at once, and what is still refused -------------------------------------------


def test_a_store_with_every_such_deviation_decodes_exactly(store_copy, good_store):
    rewrite(
        store_copy,
        root=together(upstream_registrations, resample("nearest")),
        block=together(set_key(spec_version="0.3.1"), pop("levels", "band_names", "crs")),
        arrays=together(upstream_registrations, pop("_ARRAY_DIMENSIONS")),
        coordinates=pop("_ARRAY_DIMENSIONS", "units", "calendar"),
        groups=pop("crs", "transform", "resolution"),
    )
    opened = chronozarr.open_store(store_copy)
    truth = chronozarr.open_store(good_store)
    for level in range(len(opened.levels)):
        for t in range(len(opened.times)):
            np.testing.assert_array_equal(opened.read(t, level), truth.read(t, level))
            np.testing.assert_array_equal(opened.read_mask(t, level), truth.read_mask(t, level))
    np.testing.assert_array_equal(opened.physical(1), truth.physical(1))
    assert len(opened.notes) >= 6
    assert chronozarr.validate(store_copy) != []


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda b: b.update(bands=["B04", "B08"]), "v0.3 requires band objects"),
        (lambda b: b.pop("bands"), "missing required key 'bands'"),
        (lambda b: b.pop("times"), "missing required key 'times'"),
        (lambda b: b.pop("nodata"), "missing required key 'nodata'"),
    ],
    ids=["bands-as-strings", "no-bands", "no-times", "no-nodata"],
)
def test_a_block_without_what_the_values_need_is_still_refused(store_copy, edit, message):
    rewrite(store_copy, block=edit)
    with pytest.raises(schema.SchemaError, match=message):
        chronozarr.open_store(store_copy)
    assert any(message in p for p in chronozarr.validate(store_copy))


def test_a_pyramid_that_is_not_a_factor_two_ladder_is_still_refused(store_copy):
    def rescale(attrs: dict[str, Any]) -> None:
        attrs["multiscales"]["layout"][1]["transform"]["scale"] = [3, 3]

    rewrite(store_copy, root=rescale)
    with pytest.raises(schema.SchemaError, match="scale 2"):
        chronozarr.open_store(store_copy)


# --- Where the notes show --------------------------------------------------------------------


def test_the_xarray_engine_carries_the_notes_on_the_dataset(store_copy):
    rewrite(store_copy, root=resample("nearest"))
    dataset = xr.open_dataset(store_copy, engine="chronozarr", physical=False)
    assert has(dataset.attrs["chronozarr_notes"], "resampling_method")


def test_info_prints_the_notes(store_copy):
    rewrite(store_copy, root=resample("nearest"))
    result = CliRunner().invoke(main, ["info", str(store_copy)])
    assert result.exit_code == 0, result.output
    lines = [line for line in result.stdout.splitlines() if line.startswith("note: ")]
    assert has(lines, "resampling_method")


def test_info_prints_no_note_for_a_conforming_store(good_store):
    result = CliRunner().invoke(main, ["info", str(good_store)])
    assert result.exit_code == 0, result.output
    assert "note: " not in result.stdout


def test_validate_prints_the_notes_of_a_store_it_passes(store_copy):
    rewrite(store_copy, block=pop("levels", "band_names"))
    result = CliRunner().invoke(main, ["validate", str(store_copy)])
    assert result.exit_code == 0, result.output
    assert "note: chronozarr.levels" in result.stdout
    assert "note: chronozarr.band_names" in result.stdout
