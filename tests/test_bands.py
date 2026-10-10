"""Band roles: detection, assignment, the in-place rewrite, and the CLI and URLs that use them."""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import numpy as np
import pytest
from click.testing import CliRunner

import chronozarr
from chronozarr import schema
from chronozarr.bands import (
    PRODUCT_ROLES,
    PRODUCTS,
    SENTINEL2_NAMES,
    assign_roles,
    display_limits_apply,
    parse_roles,
    product_status,
    resolve_role,
    resolve_roles,
    set_band_roles,
)
from chronozarr.cli import main
from chronozarr.schema import Band
from chronozarr.view import serve_store, viewer_url
from tests.synthetic import CRS, TRANSFORM, make_da, make_truth

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent


def _run(*args: str, ok: bool = True):
    result = CliRunner().invoke(main, list(args), catch_exceptions=False)
    assert (result.exit_code == 0) is ok, result.output
    return result


def _store(path: Path, names: list[str], **kwargs) -> Path:
    truth = make_truth(3, len(names), 40, 50)
    chronozarr.encode(make_da(truth, names), path, chunk_size=16, **kwargs)
    return path


def _stored_bands(path: Path) -> tuple[Band, ...]:
    return chronozarr.open_store(path).attrs.bands


# --- detection: the viewer's rules (js/shared/products.js findBand) ---


def test_common_name_wins_over_name_and_sentinel2_name():
    bands = [Band("B04"), Band("red"), Band("x", common_name="red")]
    found = resolve_role(bands, "red")
    assert (found.bands, found.source, found.ambiguous) == (("x",), "common_name", False)


def test_name_wins_over_sentinel2_name_and_is_case_insensitive():
    found = resolve_role([Band("B04"), Band("Red")], "red")
    assert (found.bands, found.source) == (("Red",), "name")


def test_sentinel2_name_is_exact_and_only_for_bands_without_a_common_name():
    assert resolve_role([Band("B04")], "red").source == "Sentinel-2 name"
    assert resolve_role([Band("b04")], "red").bands == ()
    assert resolve_role([Band("B04", common_name="nir")], "red").bands == ()


def test_a_band_with_another_common_name_is_not_found_by_its_name():
    assert resolve_role([Band("red", common_name="nir")], "red").bands == ()


def test_unknown_names_are_never_guessed():
    bands = [Band("band_1"), Band("b2"), Band("Band 3"), Band("NIR1")]
    assert all(r.bands == () for r in resolve_roles(bands).values())


def test_two_bands_at_the_same_rule_are_ambiguous():
    found = resolve_role([Band("a", common_name="red"), Band("b", common_name="red")], "red")
    assert found.ambiguous
    assert found.bands == ("a", "b")


def test_product_status_names_the_missing_roles():
    statuses = {s.id: s for s in product_status([Band("B04"), Band("B03"), Band("B02")])}
    assert statuses["true_color"].available
    assert not statuses["ndvi"].available
    assert statuses["ndvi"].missing == ("nir",)
    assert statuses["false_color"].missing == ("nir",)
    assert statuses["band"].available and statuses["band"].missing == ()


# --- the tables mirror the viewer's ---


def test_tables_match_the_viewer_source():
    source = (ROOT / "js/shared/products.js").read_text(encoding="utf-8")
    viewer_products = re.findall(
        r"\{ id: '(\w+)', shader: \d+, name: '([^']+)', needs: (\[[^\]]*\]|null) \}", source
    )
    parsed = tuple(
        (i, name, tuple(re.findall(r"'(\w+)'", needs)) if needs != "null" else ())
        for i, name, needs in viewer_products
    )
    assert parsed == PRODUCTS
    names = re.search(r"const SENTINEL2_NAMES = \{([^}]*)\}", source)
    assert names is not None
    viewer_names = dict(re.findall(r"(\w+): '(\w+)'", names.group(1)))
    assert {k: v for k, v in viewer_names.items() if k in PRODUCT_ROLES} == SENTINEL2_NAMES
    assert re.search(r"DTYPE_MAX = \{ uint8: 255, uint16: 65535 \}", source)
    assert re.search(r"toPhysical\(max, band\) <= 10\b", source)


@pytest.mark.parametrize(
    ("dtype", "band", "applies"),
    [
        ("uint16", Band("B04", scale=1e-4, offset=0.0), False),  # reflectance 0..6.5535
        ("uint16", Band("x"), True),  # raw counts up to 65535
        ("uint16", Band("t", scale=0.01, offset=-273.15), True),  # kelvin-like
        ("uint8", Band("r"), True),  # 0..255 unscaled
        ("uint8", Band("r", scale=0.001), False),
        ("float32", Band("x", scale=1e-4), True),
        ("int16", Band("x", scale=1e-4), True),
    ],
)
def test_display_limits_apply(dtype, band, applies):
    assert display_limits_apply(dtype, band) is applies


# --- assignment ---


def test_parse_roles_accepts_repeats_and_commas():
    assert parse_roles(["B04=red,B08=nir", "x=green"]) == {
        "B04": "red",
        "B08": "nir",
        "x": "green",
    }
    assert parse_roles([" a = red ,"]) == {"a": "red"}


@pytest.mark.parametrize("text", ["B04", "=red", "B04=", "a=red,a=blue"])
def test_parse_roles_rejects_malformed_and_duplicates(text):
    with pytest.raises(ValueError, match=r"NAME=ROLE|two roles"):
        parse_roles([text])


def test_assign_roles_sets_replaces_and_clears():
    bands = (Band("a", common_name="blue"), Band("b"), Band("c", common_name="nir", units="u"))
    updated = assign_roles(bands, {"a": "red", "b": "green", "c": "none"})
    assert [b.common_name for b in updated] == ["red", "green", None]
    assert updated[2].units == "u"
    assert bands[0].common_name == "blue"


def test_assign_roles_refuses_unknown_band_unknown_role_and_duplicates():
    bands = (Band("a"), Band("b"))
    with pytest.raises(ValueError, match="no band named 'zz'"):
        assign_roles(bands, {"zz": "red"})
    with pytest.raises(ValueError, match="'rd' is not a STAC eo common name"):
        assign_roles(bands, {"a": "rd"})
    with pytest.raises(ValueError, match="would all be red"):
        assign_roles(bands, {"a": "red", "b": "red"})


def test_an_explicit_common_name_outranks_a_band_named_like_the_role():
    updated = assign_roles((Band("red"), Band("b")), {"b": "red"})
    assert resolve_role(updated, "red").bands == ("b",)


# --- rewriting a store ---


def _snapshot(path: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(path)): p.read_bytes()
        for p in sorted(path.rglob("*"))
        if p.is_file() and p.name != "zarr.json"
    }


def test_set_band_roles_changes_only_the_root_and_validates(tmp_path):
    path = _store(tmp_path / "s", ["b2", "b3", "b4", "b5"], shard=True)
    before_root = json.loads((path / "zarr.json").read_text())
    before_files = _snapshot(path)
    before_bands = _stored_bands(path)

    after = set_band_roles(path, {"b2": "blue", "b3": "green", "b4": "red", "b5": "nir"})

    assert [b.common_name for b in after] == ["blue", "green", "red", "nir"]
    assert [b.common_name for b in _stored_bands(path)] == ["blue", "green", "red", "nir"]
    assert [(b.name, b.scale, b.offset, b.units) for b in after] == [
        (b.name, b.scale, b.offset, b.units) for b in before_bands
    ]
    assert _snapshot(path) == before_files  # data, masks and every level's zarr.json
    assert schema.validate(path) == []
    root = json.loads((path / "zarr.json").read_text())
    assert root["consolidated_metadata"] is not None
    assert root["consolidated_metadata"] == before_root["consolidated_metadata"]
    ours = root["attributes"]["chronozarr"]
    theirs = before_root["attributes"]["chronozarr"]
    assert {k: v for k, v in ours.items() if k != "bands"} == {
        k: v for k, v in theirs.items() if k != "bands"
    }
    assert {k: v for k, v in root["attributes"].items() if k != "chronozarr"} == {
        k: v for k, v in before_root["attributes"].items() if k != "chronozarr"
    }


def test_set_band_roles_makes_the_roles_resolve(tmp_path):
    path = _store(tmp_path / "s", ["b2", "b3", "b4", "b5"])
    assert not {s.id: s for s in product_status(_stored_bands(path))}["ndvi"].available
    set_band_roles(path, {"b4": "red", "b5": "nir"})
    assert {s.id: s for s in product_status(_stored_bands(path))}["ndvi"].available


def test_set_band_roles_clears_and_a_repeat_changes_nothing(tmp_path):
    path = _store(tmp_path / "s", ["b2", "b3"])
    set_band_roles(path, {"b2": "blue"})
    root = (path / "zarr.json").read_bytes()
    set_band_roles(path, {"b2": "blue"})
    assert (path / "zarr.json").read_bytes() == root
    set_band_roles(path, {"b2": "none"})
    assert _stored_bands(path)[0].common_name is None


def test_set_band_roles_refusals_leave_the_store_alone(tmp_path):
    path = _store(tmp_path / "s", ["a", "b"])
    root = (path / "zarr.json").read_bytes()
    with pytest.raises(ValueError, match="no band named"):
        set_band_roles(path, {"nope": "red"})
    with pytest.raises(ValueError, match="not a local directory"):
        set_band_roles("https://example.org/store", {"a": "red"})
    assert (path / "zarr.json").read_bytes() == root
    document = json.loads(root)
    document["attributes"]["chronozarr"]["spec_version"] = "0.0.1"
    (path / "zarr.json").write_text(json.dumps(document))
    with pytest.raises(ValueError, match="does not validate"):
        set_band_roles(path, {"a": "red"})


# --- encode and convert ---


@pytest.fixture
def zarr_input(tmp_path):
    path = tmp_path / "input.zarr"
    make_da(make_truth(3, 2, 40, 50), ["b4", "b8"]).to_dataset(name="reflectance").to_zarr(
        path, zarr_format=2, consolidated=False
    )
    return path


def test_encode_band_role_writes_common_names(tmp_path, zarr_input):
    out = tmp_path / "out"
    _run("encode", str(zarr_input), str(out), "--chunk-size", "16", "--band-role", "b4=red,b8=nir")
    assert [b.common_name for b in _stored_bands(out)] == ["red", "nir"]
    assert schema.validate(out) == []


def test_encode_band_role_error_writes_nothing(tmp_path, zarr_input):
    out = tmp_path / "out"
    result = _run("encode", str(zarr_input), str(out), "--band-role", "zz=red", ok=False)
    assert "no band named 'zz'" in result.output
    assert not out.exists()
    assert (
        "NAME=ROLE"
        in _run("encode", str(zarr_input), str(out), "--band-role", "b4", ok=False).output
    )


def test_convert_band_role_sets_a_common_name(tmp_path):
    pytest.importorskip("rasterio")
    da = make_da(make_truth(3, 2, 40, 50), ["b4", "b8"])
    source = tmp_path / "in.zarr"
    da.assign_coords(
        y=TRANSFORM[5] + TRANSFORM[4] * (np.arange(40) + 0.5),
        x=TRANSFORM[2] + TRANSFORM[0] * (np.arange(50) + 0.5),
    ).to_dataset(name="reflectance").to_zarr(source, zarr_format=2, consolidated=True)
    out = tmp_path / "out"
    _run(
        "convert", str(source), str(out), "--variable", "reflectance", "--crs", CRS,
        "--chunk-size", "16", "--band-role", "b8=nir",
    )  # fmt: skip
    assert [b.common_name for b in _stored_bands(out)] == [None, "nir"]


def test_convert_band_role_is_checked_before_any_pixel_is_read(tmp_path, zarr_input):
    pytest.importorskip("rasterio")
    out = tmp_path / "out"
    result = _run(
        "convert", str(zarr_input), str(out), "--variable", "reflectance", "--crs", CRS,
        "--band-role", "zz=red", "--dry-run", ok=False,
    )  # fmt: skip
    assert "no band named 'zz'" in result.output
    assert not out.exists()


# --- the bands command ---


def test_bands_lists_roles_and_products(tmp_path):
    path = _store(tmp_path / "s", ["B04", "B03", "B02", "weird"])
    text = _run("bands", str(path)).output
    assert re.search(r"B04\s+-\s+red \(Sentinel-2 name\)", text)
    assert re.search(r"weird\s+-\s+-\s", text)
    assert re.search(r"True color\s+available", text)
    assert re.search(r"NDVI\s+needs nir", text)


def test_bands_dry_run_writes_nothing_and_set_writes(tmp_path):
    path = _store(tmp_path / "s", ["B04", "B03", "B02", "weird"])
    root = (path / "zarr.json").read_bytes()
    dry = _run("bands", str(path), "--band-role", "weird=nir", "--dry-run").output
    assert "dry run" in dry and re.search(r"NDVI\s+available", dry)
    assert (path / "zarr.json").read_bytes() == root
    done = _run("bands", str(path), "--band-role", "weird=nir").output
    assert re.search(r"NDVI\s+available", done)
    assert _stored_bands(path)[3].common_name == "nir"


def test_bands_warns_on_ambiguity_and_refuses_to_create_it(tmp_path):
    path = _store(tmp_path / "s", ["red", "Red", "x"])
    result = CliRunner().invoke(main, ["bands", str(path)])
    assert result.exit_code == 0
    assert "red, Red all answer to red" in result.output
    refused = _run("bands", str(path), "--band-role", "x=red,red=red", ok=False)
    assert "would all be red" in refused.output


def test_bands_cannot_write_a_url(tmp_path):
    path = _store(tmp_path / "s", ["a"])
    server = serve_store(path)
    try:
        result = _run("bands", server.url, "--band-role", "a=red", ok=False)
        assert "not a local directory" in result.output
        assert re.search(r"a\s+-", _run("bands", server.url).output)
    finally:
        server.close()


# --- viewer URLs ---


def test_viewer_url_carries_the_initial_view():
    url = viewer_url(
        "https://h.example/s", t=4, product="band", band="B 4", range=(0.0, 0.00012345)
    )
    params = parse_qs(urlsplit(url).query)
    assert params == {
        "store": ["https://h.example/s"],
        "t": ["4"],
        "p": ["band"],
        "b": ["B 4"],
        "r": ["0,0.00012345"],
    }
    assert viewer_url("https://h.example/s") == (
        "https://chronozarr.org/demo/?store=https%3A%2F%2Fh.example%2Fs"
    )


@pytest.mark.parametrize("limits", [(1, 1), (2, 1), (0, float("inf")), (float("nan"), 1)])
def test_viewer_url_rejects_bad_limits(limits):
    with pytest.raises(ValueError, match="finite increasing"):
        viewer_url("https://h.example/s", range=limits)
    with pytest.raises(ValueError, match="timestep index"):
        viewer_url("https://h.example/s", t=-1)


@pytest.fixture
def hosted(tmp_path):
    path = tmp_path / "hosted"
    # uint16 with a reflectance-like band (1e-4) and one in counts
    da = make_da(make_truth(3, 3, 40, 50), ["B04", "B08", "counts"])
    chronozarr.encode(
        da,
        path,
        chunk_size=16,
        bands=[
            Band("B04", scale=1e-4, offset=0.0, units="reflectance"),
            Band("B08", scale=1e-4, offset=0.0, units="reflectance"),
            Band("counts", scale=1.0, offset=0.0),
        ],
    )
    server = serve_store(path)
    yield server.url
    server.close()


def test_link_prints_a_checked_url(hosted):
    url = _run(
        "link",
        hosted,
        "--product",
        "band",
        "--band",
        "counts",
        "--range",
        "100,5000",
        "--time",
        "2",
    ).output.strip()
    params = parse_qs(urlsplit(url).query)
    assert (params["p"], params["b"], params["r"], params["t"]) == (
        ["band"],
        ["counts"],
        ["100,5000"],
        ["2"],
    )
    assert _run("link", hosted, "--product", "ndvi").output.startswith(
        "https://chronozarr.org/demo/"
    )


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--product", "ndwi"], "ndwi needs green"),
        (["--product", "sepia"], "is not a product"),
        (["--band", "nope"], "is not a band"),
        (["--time", "3"], "outside 0..2"),
        (["--range", "1,2"], "add --product band"),
        (["--product", "band", "--range", "1,2"], "fixed limits"),  # first band is reflectance
        (["--product", "band", "--band", "counts", "--range", "5,5"], "finite increasing"),
        (["--product", "band", "--band", "counts", "--range", "1"], "2 comma-separated"),
    ],
)
def test_link_refuses_what_the_viewer_would_ignore(hosted, args, message):
    assert message in _run("link", hosted, *args, ok=False).output


def test_link_needs_a_hosted_store(tmp_path):
    assert "not an http(s) URL" in _run("link", str(tmp_path), ok=False).output
