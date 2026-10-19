"""The Crs value object: proj v0.1's precedence and CRS equality with and without pyproj."""

from __future__ import annotations

import json

import pytest

from chronozarr import crs as crs_module
from chronozarr.crs import Crs, same_crs
from tests.crs_samples import LCC_WKT2

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def pyproj_crs():
    """The pyproj CRS class; the tests that need a real EPSG definition skip without it."""
    pyproj = pytest.importorskip("pyproj")
    return pyproj.CRS


def test_projjson_wins_over_the_code_and_wkt2_wins_over_projjson(pyproj_crs):
    utm31, utm32 = pyproj_crs.from_epsg(32631), pyproj_crs.from_epsg(32632)
    wkt2_31 = utm31.to_wkt("WKT2_2019")
    json_31, json_32 = json.loads(utm31.to_json()), json.loads(utm32.to_json())
    code_vs_json = Crs(code="EPSG:32631", projjson=json_32).resolved()
    assert code_vs_json == Crs(projjson=json_32)
    json_vs_wkt2 = Crs(wkt2=wkt2_31, projjson=json_32).resolved()
    assert json_vs_wkt2 == Crs(wkt2=wkt2_31)
    agreeing = Crs(code="EPSG:32631", wkt2=wkt2_31, projjson=json_31)
    assert agreeing.resolved() == agreeing
    assert agreeing.disagreements() == []


def test_without_pyproj_the_code_is_kept_because_nothing_proves_a_disagreement(
    monkeypatch, pyproj_crs
):
    other = pyproj_crs.from_epsg(32632).to_wkt("WKT2_2019")
    monkeypatch.setattr(crs_module, "_pyproj", lambda: None)
    both = Crs(code="EPSG:32631", wkt2=other)
    assert both.disagreements() == []
    assert both.resolved() == both
    assert both.to_string() == "EPSG:32631"


def test_same_code_is_the_same_crs_whatever_the_case_and_needs_no_pyproj(monkeypatch):
    monkeypatch.setattr(crs_module, "_pyproj", lambda: None)
    match = same_crs(Crs(code="EPSG:32631"), Crs(code="epsg:32631"))
    assert (match.same, match.method) == (True, "code")


def test_identical_wkt2_text_after_whitespace_is_the_same_crs(monkeypatch):
    monkeypatch.setattr(crs_module, "_pyproj", lambda: None)
    spaced = LCC_WKT2.replace(",", ",\n    ").replace("[", "[ ")
    match = same_crs(Crs(wkt2=LCC_WKT2), Crs(wkt2=spaced))
    assert (match.same, match.method) == (True, "wkt2 text")


def test_a_code_and_a_wkt2_cannot_be_compared_without_pyproj(monkeypatch):
    monkeypatch.setattr(crs_module, "_pyproj", lambda: None)
    match = same_crs(Crs(code="EPSG:32631"), Crs(wkt2=LCC_WKT2))
    assert (match.same, match.method) == (None, "unavailable")


def test_different_codes_differ_without_pyproj(monkeypatch):
    monkeypatch.setattr(crs_module, "_pyproj", lambda: None)
    match = same_crs(Crs(code="EPSG:32631"), Crs(code="EPSG:32632"))
    assert (match.same, match.method) == (False, "code")


def test_pyproj_compares_a_code_with_its_wkt2_and_aliases(pyproj_crs):
    utm = pyproj_crs.from_epsg(32631).to_wkt("WKT2_2019")
    assert same_crs(Crs(code="EPSG:32631"), Crs(wkt2=utm)) == crs_module.CrsMatch(True, "pyproj")
    assert same_crs(Crs(code="EPSG:32632"), Crs(wkt2=utm)) == crs_module.CrsMatch(False, "pyproj")
    assert same_crs(Crs(code="EPSG:3857"), Crs(code="EPSG:900913")).same is True


def test_pyproj_ignores_names_that_a_writer_chose(pyproj_crs):
    renamed = LCC_WKT2.replace('"unknown"', '"undefined"')
    assert same_crs(Crs(wkt2=LCC_WKT2), Crs(wkt2=renamed)) == crs_module.CrsMatch(True, "pyproj")


def test_a_crs_needs_a_representation():
    with pytest.raises(ValueError, match="needs a code, wkt2 or projjson"):
        Crs()


def test_to_string_prefers_the_code_then_the_wkt2_then_compact_json(pyproj_crs):
    projjson = {"type": "ProjectedCRS", "name": "Daymet LCC"}
    utm = pyproj_crs.from_epsg(32631).to_wkt("WKT2_2019")
    assert Crs(code="EPSG:32631", wkt2=utm).to_string() == "EPSG:32631"
    assert Crs(wkt2=LCC_WKT2, projjson=projjson).to_string() == LCC_WKT2
    assert Crs(projjson=projjson).to_string() == '{"name":"Daymet LCC","type":"ProjectedCRS"}'


def test_describe_is_short_for_every_representation(pyproj_crs):
    utm = pyproj_crs.from_epsg(32631).to_wkt("WKT2_2019")
    assert Crs(code="EPSG:32631", wkt2=utm).describe() == "EPSG:32631"
    assert Crs(wkt2=LCC_WKT2).describe() == 'WKT2 "unknown"'
    assert Crs(wkt2="not a definition").describe() == "WKT2"
    assert Crs(projjson={"name": "Daymet LCC"}).describe() == 'PROJJSON "Daymet LCC"'
    assert Crs(projjson={"type": "ProjectedCRS"}).describe() == "PROJJSON"


def test_from_string_keeps_a_code_with_its_authority_in_capitals(monkeypatch):
    monkeypatch.setattr("chronozarr.crs._pyproj", lambda: None)
    parsed = Crs.from_string(" epsg:32631 ")
    assert (parsed.code, parsed.epsg, parsed.wkt2) == ("EPSG:32631", 32631, None)
    assert Crs.from_string("ESRI:102003").epsg is None


def test_from_string_takes_wkt2_as_it_is_and_refuses_other_forms_without_pyproj(monkeypatch):
    monkeypatch.setattr("chronozarr.crs._pyproj", lambda: None)
    assert Crs.from_string(LCC_WKT2) == Crs(wkt2=LCC_WKT2)
    with pytest.raises(ValueError, match="install pyproj to read other forms"):
        Crs.from_string('PROJCS["old style WKT1",GEOGCS["x"]]')
    with pytest.raises(ValueError, match="install pyproj to read other forms"):
        Crs.from_string("+proj=utm +zone=31")


def test_with_wkt2_adds_the_definition_of_a_code_and_leaves_what_it_cannot(pyproj_crs):
    expected = pyproj_crs.from_epsg(32631).to_wkt("WKT2_2019")
    assert Crs(code="EPSG:32631").with_wkt2() == Crs(code="EPSG:32631", wkt2=expected)
    assert Crs(code="EPSG:999999").with_wkt2() == Crs(code="EPSG:999999")
    assert Crs(wkt2=LCC_WKT2).with_wkt2() == Crs(wkt2=LCC_WKT2)


def test_with_wkt2_does_nothing_without_pyproj(monkeypatch):
    monkeypatch.setattr("chronozarr.crs._pyproj", lambda: None)
    assert Crs(code="EPSG:32631").with_wkt2() == Crs(code="EPSG:32631")
