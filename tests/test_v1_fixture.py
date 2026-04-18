"""Regression tests against the canonical v1 fixture.

These tests read from a pre-built v1 store (tests/fixtures/v1_canonical/)
and verify that decode produces the exact expected values. If these tests
break, it means the on-disk format or decode logic changed in an
incompatible way.

To regenerate the fixture after an intentional format change:
    uv run python tests/generate_v1_fixture.py
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from spacetime.encode.v1 import decode_v1

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "v1_canonical"
STORE_DIR = FIXTURE_DIR / "store"


@pytest.mark.unit
def test_fixture_exists():
    """Fixture store and expected values must be present."""
    assert STORE_DIR.is_dir(), f"Fixture store not found at {STORE_DIR}"
    assert (FIXTURE_DIR / "expected.npz").is_file(), "expected.npz not found"
    assert (STORE_DIR / "manifest.json").is_file(), "manifest.json not found"


@pytest.mark.unit
def test_manifest_version():
    """Manifest declares version 1.0.0."""
    with open(STORE_DIR / "manifest.json") as f:
        manifest = json.load(f)
    assert manifest["version"] == "1.0.0"


@pytest.mark.unit
def test_manifest_invariants():
    """Manifest satisfies v1 invariants: dtype uint16, star-delta encoding, nodata 0."""
    with open(STORE_DIR / "manifest.json") as f:
        manifest = json.load(f)
    assert manifest["dtype"] == "uint16"
    assert manifest["nodata"] == 0
    assert manifest["temporal"]["encoding"] == "star-delta"
    assert manifest["compressor"] == "zstd"
    assert len(manifest["temporal"]["anchor_indices"]) > 0


@pytest.mark.unit
def test_lossless_roundtrip():
    """Decode each month at LOD 0 and verify exact match against saved expected values."""
    expected = np.load(FIXTURE_DIR / "expected.npz")
    with open(STORE_DIR / "manifest.json") as f:
        manifest = json.load(f)

    for month_idx in range(len(manifest["months"])):
        decoded = decode_v1(STORE_DIR, lod=0, chunk_id="r000_c000", month_index=month_idx)
        key = f"month_{month_idx}"
        assert key in expected, f"Missing expected data for {key}"
        assert np.array_equal(decoded, expected[key]), (
            f"Month {month_idx} decoded values do not match fixture. "
            "If the format changed intentionally, regenerate with: "
            "uv run python tests/generate_v1_fixture.py"
        )


@pytest.mark.unit
def test_lossless_vs_input():
    """Decoded values exactly match the original input data."""
    input_data = np.load(FIXTURE_DIR / "input.npz")
    with open(STORE_DIR / "manifest.json") as f:
        manifest = json.load(f)

    for month_idx, month in enumerate(manifest["months"]):
        decoded = decode_v1(STORE_DIR, lod=0, chunk_id="r000_c000", month_index=month_idx)
        input_key = f"input_{month}"
        assert input_key in input_data, f"Missing input data for {input_key}"
        assert np.array_equal(decoded, input_data[input_key]), (
            f"Lossless invariant violated: decoded month {month} differs from input"
        )


@pytest.mark.unit
def test_anchor_direct_read():
    """Anchor months decode without delta reconstruction (direct uint16 read)."""
    with open(STORE_DIR / "manifest.json") as f:
        manifest = json.load(f)

    anchor_indices = manifest["temporal"]["anchor_indices"]
    expected = np.load(FIXTURE_DIR / "expected.npz")

    for anchor_idx in anchor_indices:
        decoded = decode_v1(STORE_DIR, lod=0, chunk_id="r000_c000", month_index=anchor_idx)
        assert decoded.dtype == np.uint16
        assert np.array_equal(decoded, expected[f"month_{anchor_idx}"])


@pytest.mark.unit
def test_delta_reconstruction():
    """Delta months produce correct values via anchor + delta."""
    with open(STORE_DIR / "manifest.json") as f:
        manifest = json.load(f)

    delta_reference = manifest["temporal"]["delta_reference"]
    expected = np.load(FIXTURE_DIR / "expected.npz")

    for month_str, _anchor_idx in delta_reference.items():
        month_idx = int(month_str)
        decoded = decode_v1(STORE_DIR, lod=0, chunk_id="r000_c000", month_index=month_idx)
        assert decoded.dtype == np.uint16
        assert np.array_equal(decoded, expected[f"month_{month_idx}"])


@pytest.mark.unit
def test_pyramid_exists():
    """All declared LOD levels have chunk directories on disk."""
    with open(STORE_DIR / "manifest.json") as f:
        manifest = json.load(f)

    for lod_meta in manifest["lods"]:
        lod = lod_meta["level"]
        lod_dir = STORE_DIR / "lod" / str(lod) / "chunks"
        assert lod_dir.is_dir(), f"LOD {lod} chunks directory missing"
        chunk_count = len(list(lod_dir.iterdir()))
        expected_count = lod_meta["grid_rows"] * lod_meta["grid_cols"]
        assert chunk_count == expected_count, (
            f"LOD {lod}: expected {expected_count} chunks, found {chunk_count}"
        )
