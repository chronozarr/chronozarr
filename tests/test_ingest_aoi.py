"""AOIs for the Sentinel-2 ingest from a bounding box or a GeoJSON file (aoi_input.py)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "sentinel2_pc"))
import aoi_input

pytestmark = pytest.mark.unit

RIO_GRANDE = (-106.71, 35.06, -106.66, 35.11)


def _aois_yaml() -> dict:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load((Path(aoi_input.__file__).parent / "aois.yaml").read_text())["aois"]


# --- the UTM zone is chosen as aois.yaml chose it ---------------------------------------------


def test_utm_zone_reproduces_every_aoi_in_aois_yaml():
    aois = _aois_yaml()
    assert len(aois) >= 6
    for name, entry in aois.items():
        assert aoi_input.utm_epsg(tuple(entry["bbox"])) == entry["epsg"], name


@pytest.mark.parametrize(
    "bbox,epsg",
    [
        (RIO_GRANDE, 32613),
        ((-75.0, -7.6, -74.985, -7.585), 32718),  # south of the equator: 327xx
        ((-111.0, 10.0, -105.0, 12.0), 32613),  # centre on the zone edge -108: zone 13
        ((-1.0, -1.0, 1.0, 1.0), 32631),  # centre at 0 N: north; 0 E opens zone 31
        ((179.0, 10.0, 179.5, 11.0), 32660),
        ((-179.9, -10.0, -179.5, -9.0), 32701),
    ],
)
def test_utm_zone_of_the_box_centre(bbox, epsg):
    assert aoi_input.utm_epsg(bbox) == epsg


# --- --bbox -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "values",
    [
        ["-106.71", "35.06", "-106.66", "35.11"],
        ["-106.71,35.06,-106.66,35.11"],
    ],
)
def test_bbox_as_separate_numbers_or_one_comma_separated_value(values):
    assert aoi_input.parse_bbox(values) == RIO_GRANDE


@pytest.mark.parametrize(
    "values,message",
    [
        (["-106.71", "35.06", "-106.66"], "expected 4 numbers"),
        (["-106.71,35.06,-106.66,35.11,1"], "expected 4 numbers"),
        (["-106.71", "35.06", "-106.66", "north"], "could not convert"),
        (["nan", "35.06", "-106.66", "35.11"], "finite"),
        (["-106.66", "35.06", "-106.71", "35.11"], "west .* must be less than east"),
        (["-106.71", "35.06", "-106.71", "35.11"], "west .* must be less than east"),
        (["-106.71", "35.11", "-106.66", "35.06"], "south .* must be less than north"),
        (["-190", "35.06", "-106.66", "35.11"], "between -180 and 180"),
        (["-106.71", "35.06", "-106.66", "91"], "UTM zones"),
        (["-106.71", "-85", "-106.66", "-84"], "UTM zones"),
        (["170", "60", "-170", "62"], "antimeridian"),
        (["-170", "60", "170", "62"], "antimeridian"),
    ],
)
def test_bad_bbox_says_what_to_fix(values, message):
    with pytest.raises(ValueError, match=message):
        aoi_input.parse_bbox(values)


def test_bbox_aoi_has_the_shape_of_an_aois_yaml_entry():
    aoi = aoi_input.bbox_aoi("rio_grande", RIO_GRANDE)
    assert aoi == {"name": "rio_grande", "bbox": list(RIO_GRANDE), "epsg": 32613}
    yaml_entry = next(iter(_aois_yaml().values()))
    assert set(aoi) <= {*yaml_entry, "name"}


@pytest.mark.parametrize("name", ["rio_grande", "a", "2024-reach.v2", "A_b-c.d"])
def test_plain_names_are_accepted(name):
    assert aoi_input.validate_name(name) == name


@pytest.mark.parametrize("name", ["", "../x", "a/b", "-x", ".hidden", "my river", "rio\\grande"])
def test_names_that_are_not_plain_file_names_are_refused(name):
    with pytest.raises(ValueError, match="--name"):
        aoi_input.validate_name(name)


# --- --aoi-file -------------------------------------------------------------------------------

RING = [[-106.70, 35.07], [-106.66, 35.07], [-106.67, 35.11], [-106.71, 35.10], [-106.70, 35.07]]
RING_BBOX = (-106.71, 35.07, -106.66, 35.11)
POLYGON = {"type": "Polygon", "coordinates": [RING]}
FEATURE = {"type": "Feature", "properties": {}, "geometry": POLYGON}


def write_geojson(tmp_path: Path, document: dict, name: str = "reach.geojson") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(document))
    return path


@pytest.mark.parametrize(
    "document",
    [
        {"type": "FeatureCollection", "features": [FEATURE]},
        FEATURE,
        POLYGON,
        {"type": "MultiPolygon", "coordinates": [[RING]]},
        {"type": "GeometryCollection", "geometries": [{"type": "Polygon", "coordinates": [RING]}]},
        {"type": "Polygon", "coordinates": [[[x, y, 1500.0] for x, y in RING]]},  # with heights
    ],
    ids=["feature-collection", "feature", "polygon", "multipolygon", "collection", "heights"],
)
def test_the_aoi_is_the_bounding_box_of_the_geometry(tmp_path, document):
    assert aoi_input.bbox_of_geojson(write_geojson(tmp_path, document)) == RING_BBOX


def test_every_feature_of_a_collection_counts(tmp_path):
    collection = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-106.80, 35.00]}},
            {"type": "Feature", "geometry": {"type": "LineString", "coordinates": RING[:2]}},
            {"type": "Feature", "geometry": None},
        ],
    }
    bbox = aoi_input.bbox_of_geojson(write_geojson(tmp_path, collection))
    assert bbox == (-106.80, 35.00, -106.66, 35.07)


def test_the_file_name_names_the_aoi_unless_a_name_is_given(tmp_path):
    path = write_geojson(tmp_path, POLYGON)
    assert aoi_input.geojson_aoi(path) == {
        "name": "reach",
        "bbox": list(RING_BBOX),
        "epsg": 32613,
    }
    assert aoi_input.geojson_aoi(path, "bosque")["name"] == "bosque"


def test_a_file_name_that_is_not_a_plain_name_needs_name(tmp_path):
    path = write_geojson(tmp_path, POLYGON, "my reach.geojson")
    with pytest.raises(ValueError, match="--name"):
        aoi_input.geojson_aoi(path)
    assert aoi_input.geojson_aoi(path, "my_reach")["name"] == "my_reach"


def test_geojson_that_cannot_be_read_is_named(tmp_path):
    broken = tmp_path / "broken.geojson"
    broken.write_text('{"type": "Polygon", "coordinates": [[[1, 2')
    with pytest.raises(ValueError, match=r"cannot read GeoJSON .*broken\.geojson"):
        aoi_input.bbox_of_geojson(broken)
    with pytest.raises(ValueError, match="cannot read GeoJSON"):
        aoi_input.bbox_of_geojson(tmp_path / "missing.geojson")


@pytest.mark.parametrize(
    "document",
    [
        {"type": "FeatureCollection", "features": []},
        {"type": "Feature", "geometry": None},
        {"type": "Polygon", "coordinates": []},
        ["not", "geojson"],
    ],
)
def test_geojson_without_coordinates_is_refused(tmp_path, document):
    with pytest.raises(ValueError, match="no coordinates"):
        aoi_input.bbox_of_geojson(write_geojson(tmp_path, document))


def test_projected_coordinates_are_refused_as_not_longitude_latitude(tmp_path):
    utm = {"type": "Polygon", "coordinates": [[[350000.0, 3880000.0], [355000.0, 3885000.0]]]}
    with pytest.raises(ValueError, match="longitude, latitude"):
        aoi_input.bbox_of_geojson(write_geojson(tmp_path, utm))


def test_a_geometry_split_at_the_antimeridian_is_refused(tmp_path):
    east = [[179.5, 60.0], [180.0, 60.0], [180.0, 61.0], [179.5, 61.0], [179.5, 60.0]]
    west = [[-180.0, 60.0], [-179.5, 60.0], [-179.5, 61.0], [-180.0, 61.0], [-180.0, 60.0]]
    split = {"type": "MultiPolygon", "coordinates": [[east], [west]]}
    with pytest.raises(ValueError, match="antimeridian"):
        aoi_input.bbox_of_geojson(write_geojson(tmp_path, split))


def test_a_nan_longitude_is_refused(tmp_path):
    path = tmp_path / "nan.geojson"
    path.write_text('{"type": "Polygon", "coordinates": [[[NaN, 35.0], [-106.0, 36.0]]]}')
    with pytest.raises(ValueError, match="finite"):
        aoi_input.bbox_of_geojson(path)
