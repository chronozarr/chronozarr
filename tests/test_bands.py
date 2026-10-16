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
    POLARISATION_PAIRS,
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
from chronozarr.view import COLORMAPS, serve_store, viewer_url
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
    assert statuses["dual_pol"].missing == ("copol", "crosspol")


# --- SAR roles: js/test/dual-pol.test.js holds the same cases ---


def _pair(*bands: Band) -> tuple[str | None, str | None]:
    """The names of the bands that answer to copol and crosspol (None where no band does)."""
    resolved = resolve_roles(list(bands))
    copol, crosspol = resolved["copol"].bands, resolved["crosspol"].bands
    return (copol[0] if copol else None, crosspol[0] if crosspol else None)


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        (("VV", "VH"), ("VV", "VH")),
        (("VH", "VV"), ("VV", "VH")),
        (("HH", "HV"), ("HH", "HV")),
        (("mask_x", "hv", "Hh"), ("Hh", "hv")),
        (("HH", "HV", "VH", "VV"), ("VV", "VH")),  # quad-pol: VV with VH wins
        (("VV", "VH", "HH", "HV"), ("VV", "VH")),
        (("HH", "HV", "VV"), ("HH", "HV")),  # no VH: HH with HV
        (("HH", "HV", "VH"), ("HH", "HV")),  # no VV: HH with HV
        (("VV", "HV"), (None, None)),  # VV is never paired with HV
        (("HH", "VH"), (None, None)),  # nor HH with VH
        (("VV",), (None, None)),
        (("VH", "HV"), (None, None)),
        (("VV", "HH"), (None, None)),
    ],
)
def test_sar_roles_are_a_whole_pair_by_name(names, expected):
    assert _pair(*(Band(name) for name in names)) == expected


def test_sar_roles_follow_a_declared_common_name_first():
    declared = [Band("a", common_name="copol"), Band("b", common_name="crosspol"), Band("VV")]
    assert _pair(*declared, Band("VH")) == ("a", "b")
    assert resolve_role(declared, "copol").source == "common_name"
    # The role that is not declared is found by name; a name needs its whole pair.
    assert _pair(Band("x", common_name="copol"), Band("VV"), Band("VH")) == ("x", "VH")
    assert _pair(Band("x", common_name="copol"), Band("HV")) == ("x", None)
    assert _pair(Band("z", common_name="crosspol"), Band("VV"), Band("VH")) == ("VV", "z")
    # A band with another common_name is not matched by its name.
    assert _pair(Band("VV", common_name="red"), Band("VH")) == (None, None)
    assert resolve_role([Band("VV"), Band("VH")], "copol").source == "name"


def test_dual_pol_is_available_for_a_pair_and_for_no_other_store():
    available = {
        names: {s.id: s for s in product_status([Band(n) for n in names])}["dual_pol"].available
        for names in [("VV", "VH"), ("HH", "HV"), ("VV", "HV"), ("B04", "B08"), ("B2",)]
    }
    assert available == {
        ("VV", "VH"): True,
        ("HH", "HV"): True,
        ("VV", "HV"): False,
        ("B04", "B08"): False,
        ("B2",): False,
    }


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
    pairs = re.search(r"const POLARISATION_PAIRS = \[(.*)\];", source)
    assert pairs is not None
    viewer_pairs = tuple(tuple(m) for m in re.findall(r"\['(\w+)', '(\w+)'\]", pairs.group(1)))
    assert viewer_pairs == POLARISATION_PAIRS
    assert re.search(r"DTYPE_MAX = \{ uint8: 255, uint16: 65535 \}", source)
    assert re.search(r"toPhysical\(max, band\) <= 10\b", source)


def test_colormaps_match_the_viewer_and_the_player():
    viewer = (ROOT / "js/shared/colormaps.js").read_text(encoding="utf-8")
    assert tuple(re.findall(r"\{\s*id: '(\w+)',", viewer)) == COLORMAPS
    player = (ROOT / "src/chronozarr/player.js").read_text(encoding="utf-8")
    listed = re.search(r"const COLORMAPS = \[(.*)\];", player)
    assert listed is not None
    assert tuple(re.findall(r"\['(\w+)', '\w+'\]", listed.group(1))) == COLORMAPS


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
    assert re.search(r"true_color\s+True color\s+available", text), "the id that link takes"
    assert re.search(r"NDVI\s+needs nir", text)
    assert re.search(r"dual_pol\s+Dual-pol composite\s+needs copol, crosspol", text)


def test_bands_shows_the_sar_roles_and_the_composite(tmp_path):
    path = _store(tmp_path / "s", ["HH", "HV"])
    text = _run("bands", str(path)).output
    assert re.search(r"HH\s+-\s+copol \(name\)", text)
    assert re.search(r"HV\s+-\s+crosspol \(name\)", text)
    assert re.search(r"dual_pol\s+Dual-pol composite\s+available", text)
    assert re.search(r"true_color\s+True color\s+needs red, green, blue", text)


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
        "https://h.example/s",
        t=4,
        product="band",
        band="B 4",
        range=(0.0, 0.00012345),
        colormap="viridis",
    )
    params = parse_qs(urlsplit(url).query)
    assert params == {
        "store": ["https://h.example/s"],
        "t": ["4"],
        "p": ["band"],
        "b": ["B 4"],
        "r": ["0,0.00012345"],
        "cm": ["viridis"],
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


@pytest.mark.parametrize("colormap", ["jet", "Viridis", ""])
def test_viewer_url_rejects_an_unknown_colormap(colormap):
    with pytest.raises(ValueError, match="gray, viridis, magma, turbo"):
        viewer_url("https://h.example/s", product="band", colormap=colormap)


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
        "--colormap",
        "magma",
        "--time",
        "2",
    ).stdout.strip()
    params = parse_qs(urlsplit(url).query)
    assert (params["p"], params["b"], params["r"], params["cm"], params["t"]) == (
        ["band"],
        ["counts"],
        ["100,5000"],
        ["magma"],
        ["2"],
    )
    plain = _run("link", hosted, "--product", "ndvi")
    assert plain.stdout.startswith("https://chronozarr.org/demo/")
    assert f"chronozarr doctor {hosted}" in plain.stderr, "the browser read is left to doctor"


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--product", "ndwi"], "ndwi needs green"),
        (["--product", "dual_pol"], "dual_pol needs copol, crosspol"),
        (["--product", "sepia"], "is not a product"),
        (["--band", "nope"], "is not a band"),
        (["--time", "3"], "outside 0..2"),
        (["--range", "1,2"], "add --product band"),
        (["--product", "band", "--range", "1,2"], "fixed limits"),  # first band is reflectance
        (["--product", "band", "--band", "counts", "--range", "5,5"], "finite increasing"),
        (["--product", "band", "--band", "counts", "--range", "1"], "2 comma-separated"),
        (["--colormap", "viridis"], "add --product band"),
        (["--product", "ndvi", "--colormap", "viridis"], "add --product band"),
        (["--product", "band", "--colormap", "jet"], "'jet' is not one of"),
    ],
)
def test_link_refuses_what_the_viewer_would_ignore(hosted, args, message):
    assert message in _run("link", hosted, *args, ok=False).output


@pytest.mark.parametrize("args", [["--colormap", "viridis"], ["--range", "1,2"]])
def test_link_reports_a_usage_error_before_reading_the_store(args):
    # nothing listens on port 9 (discard): a read would fail with a connection error instead
    refused = _run("link", "http://127.0.0.1:9/nope", *args, ok=False).output
    assert "add --product band" in refused


def test_link_opens_a_sar_store_on_the_composite(tmp_path):
    server = serve_store(_store(tmp_path / "sar", ["VV", "VH"]))
    try:
        url = _run("link", server.url, "--product", "dual_pol").stdout.strip()
        assert parse_qs(urlsplit(url).query)["p"] == ["dual_pol"]
    finally:
        server.close()


def test_link_needs_a_hosted_store(tmp_path):
    refused = _run("link", str(tmp_path), ok=False).output
    assert "not an http(s) URL" in refused
    assert f"chronozarr preview {tmp_path}" in refused
