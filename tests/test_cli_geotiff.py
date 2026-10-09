"""`chronozarr encode` and `append` from a glob of GeoTIFFs carry band metadata and nodata."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from click.testing import CliRunner

import chronozarr
from chronozarr.cli import main
from chronozarr.schema import Band

pytestmark = pytest.mark.unit

rasterio = pytest.importorskip("rasterio")

HEIGHT, WIDTH = 20, 30
TRANSFORM = rasterio.transform.Affine(10.0, 0.0, 746090.0, 0.0, -10.0, 2540440.0)
Declared = dict[str, Any]  # descriptions, scales, offsets, units and nodata a test file declares
SENTINEL_2: Declared = {
    "descriptions": ("B02", "B03"),
    "scales": (0.0001, 0.0001),
    "offsets": (0.0, 0.0),
    "units": ("reflectance", "reflectance"),
}


def _run(*args: str):
    return CliRunner().invoke(main, list(args), catch_exceptions=False)


def _values(month: int) -> np.ndarray:
    """(band, y, x) uint16 with no zeros, so every pixel is valid under the default nodata 0."""
    rng = np.random.default_rng(month)
    return rng.integers(1, 5000, size=(2, HEIGHT, WIDTH), dtype=np.uint16)


def _write_month(
    directory: Path, month: int, declared: Declared | None = None, values: np.ndarray | None = None
) -> None:
    """One GeoTIFF named scene_2024-MM.tif; what `declared` leaves out the file leaves unset."""
    declared = declared or {}
    directory.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        directory / f"scene_2024-{month:02d}.tif",
        "w",
        driver="GTiff",
        height=HEIGHT,
        width=WIDTH,
        count=2,
        dtype="uint16",
        crs="EPSG:32718",
        transform=TRANSFORM,
        nodata=declared.get("nodata"),
    ) as dst:
        dst.write(_values(month) if values is None else values)
        for key in ("descriptions", "scales", "offsets", "units"):
            if key in declared:
                setattr(dst, key, declared[key])


def _write_months(directory: Path, months: range, declared: Declared | None = None) -> None:
    for month in months:
        _write_month(directory, month, declared)


def _digests(path: Path) -> dict[str, str]:
    return {
        str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(path.rglob("*"))
        if p.is_file()
    }


def _encode(tifs: Path, out: Path):
    return _run("encode", str(tifs / "*.tif"), str(out), "--chunk-size", "16")


def _append(store: Path, tifs: Path):
    return CliRunner().invoke(main, ["append", str(store), str(tifs / "*.tif")])


@pytest.fixture
def scaled_store(tmp_path):
    """Two Sentinel-2 style months (scale 0.0001 and units on both bands), as a store."""
    _write_months(tmp_path / "tifs", range(1, 3), SENTINEL_2)
    out = tmp_path / "store"
    result = _encode(tmp_path / "tifs", out)
    assert result.exit_code == 0, result.output
    return out


def test_encode_carries_band_description_scale_offset_and_units(tmp_path):
    declared: Declared = {
        "descriptions": ("B02", "B03"),
        "scales": (0.0001, 0.0002),
        "offsets": (0.0, -0.1),
        "units": ("reflectance", "DN"),
    }
    _write_months(tmp_path / "tifs", range(1, 4), declared)
    result = _encode(tmp_path / "tifs", tmp_path / "out")
    assert result.exit_code == 0, result.output

    store = chronozarr.open_store(tmp_path / "out")
    assert store.attrs.bands == (
        Band("B02", scale=0.0001, offset=0.0, units="reflectance"),
        Band("B03", scale=0.0002, offset=-0.1, units="DN"),
    )
    raw = _values(2).astype(np.float32)  # the second month
    expected = raw * np.array([0.0001, 0.0002], np.float32)[:, None, None]
    expected += np.array([0.0, -0.1], np.float32)[:, None, None]
    assert np.array_equal(store.read(t=1), _values(2))
    assert np.allclose(store.physical(t=1), expected, rtol=1e-6, atol=1e-6)


def test_encode_without_scale_offset_or_nodata_keeps_the_defaults(tmp_path):
    _write_months(tmp_path / "tifs", range(1, 3))
    result = _encode(tmp_path / "tifs", tmp_path / "out")
    assert result.exit_code == 0, result.output

    store = chronozarr.open_store(tmp_path / "out")
    assert store.attrs.bands == (
        Band("1", scale=1.0, offset=0.0),
        Band("2", scale=1.0, offset=0.0),
    )
    assert store.attrs.nodata == 0


MISMATCHES: dict[str, tuple[Declared, str, str]] = {
    "scale": ({"scales": (0.0001, 0.0002)}, "band 'B03' has scale 0.0002", "has 0.0001"),
    "offset": ({"offsets": (0.0, -0.1)}, "band 'B03' has offset -0.1", "has 0.0"),
    "units": ({"units": ("reflectance", "DN")}, "band 'B03' has units 'DN'", "has 'reflectance'"),
    "description": (
        {"descriptions": ("B02", "B8A")},
        "band 'B03' has description 'B8A'",
        "has 'B03'",
    ),
}


@pytest.mark.parametrize("field", MISMATCHES)
def test_encode_names_the_file_band_and_both_values_when_files_differ(tmp_path, field):
    odd, second_value, first_value = MISMATCHES[field]
    _write_month(tmp_path / "tifs", 1, SENTINEL_2)
    _write_month(tmp_path / "tifs", 2, {**SENTINEL_2, **odd})
    result = _encode(tmp_path / "tifs", tmp_path / "out")

    assert result.exit_code == 1
    assert "scene_2024-02.tif' " + second_value in result.output
    assert "scene_2024-01.tif' " + first_value in result.output
    assert not (tmp_path / "out").exists()


def test_encode_uses_the_nodata_every_file_declares(tmp_path):
    values = _values(1)
    values[0, 0, 0] = 65535
    values[0, 0, 1] = 0  # valid data once 0 is no longer the nodata
    for month in (1, 2):
        _write_month(tmp_path / "tifs", month, {**SENTINEL_2, "nodata": 65535}, values)
    result = _encode(tmp_path / "tifs", tmp_path / "out")
    assert result.exit_code == 0, result.output

    store = chronozarr.open_store(tmp_path / "out")
    assert store.attrs.nodata == 65535
    physical = store.physical(t=0)
    assert np.isnan(physical[0, 0, 0])
    assert physical[0, 0, 1] == 0.0


@pytest.mark.parametrize(
    ("second", "second_text"),
    [(65535, "declares nodata 65535"), (None, "declares no nodata")],
    ids=["different values", "declared in one file only"],
)
def test_encode_fails_when_files_declare_different_nodata(tmp_path, second, second_text):
    _write_month(tmp_path / "tifs", 1, {**SENTINEL_2, "nodata": 0})
    _write_month(tmp_path / "tifs", 2, {**SENTINEL_2, "nodata": second})
    result = _encode(tmp_path / "tifs", tmp_path / "out")

    assert result.exit_code == 1
    assert f"scene_2024-02.tif' band 'B02' {second_text}" in result.output
    assert "scene_2024-01.tif' band 'B02' declares nodata 0" in result.output
    assert not (tmp_path / "out").exists()


def test_append_continues_a_scaled_store_from_scaled_files(tmp_path, scaled_store):
    _write_month(tmp_path / "new", 3, SENTINEL_2)
    result = _append(scaled_store, tmp_path / "new")
    assert result.exit_code == 0, result.output

    store = chronozarr.open_store(scaled_store)
    assert len(store.times) == 3
    assert store.attrs.bands[0] == Band("B02", scale=0.0001, offset=0.0, units="reflectance")
    assert np.array_equal(store.read(t=2), _values(3))
    assert np.allclose(store.physical(t=2), _values(3) * np.float32(0.0001), rtol=1e-6)


def test_append_refuses_files_whose_scale_differs_from_the_store(tmp_path, scaled_store):
    _write_month(tmp_path / "new", 3, {**SENTINEL_2, "scales": (0.0001, 0.0002)})
    before = _digests(scaled_store)
    result = _append(scaled_store, tmp_path / "new")

    assert result.exit_code == 1
    assert "bands: the input has 'B03' {'scale': 0.0002" in result.output
    assert "the store has 'B03' {'scale': 0.0001" in result.output
    assert _digests(scaled_store) == before


def test_append_refuses_files_with_no_scale_to_a_scaled_store(tmp_path, scaled_store):
    _write_month(tmp_path / "new", 3, {"descriptions": ("B02", "B03")})
    before = _digests(scaled_store)
    result = _append(scaled_store, tmp_path / "new")

    assert result.exit_code == 1
    assert "bands: the input has 'B02'" in result.output
    assert "the store has 'B02' {'scale': 0.0001" in result.output
    assert _digests(scaled_store) == before


def test_append_refuses_files_that_declare_another_nodata_than_the_store(tmp_path, scaled_store):
    _write_month(tmp_path / "new", 3, {**SENTINEL_2, "nodata": 65535})
    before = _digests(scaled_store)
    result = _append(scaled_store, tmp_path / "new")

    assert result.exit_code == 1
    assert "the files declare nodata 65535; the store has nodata 0" in result.output
    assert _digests(scaled_store) == before


def test_append_accepts_files_that_declare_the_stores_nodata(tmp_path, scaled_store):
    _write_month(tmp_path / "new", 3, {**SENTINEL_2, "nodata": 0})
    result = _append(scaled_store, tmp_path / "new")
    assert result.exit_code == 0, result.output
    assert len(chronozarr.open_store(scaled_store).times) == 3
