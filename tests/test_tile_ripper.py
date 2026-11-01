"""Real-writer-shaped Tile-Ripper source joins and native-value preservation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import cast

import numpy as np
import pytest
import zarr

import chronozarr
from chronozarr.tile_ripper import _time_ns

pytestmark = pytest.mark.unit

TRANSFORM = [500000.0, 30.0, 0.0, 9100000.0, 0.0, -30.0]
CRS = "EPSG:32718"
SHAPE = (1, 4, 3)
POLICY = "hls_fmask_clear"
DATES = ["2025-07-03T15:10:00.123456789Z", "2025-07-08T15:10:00Z"]


def _array(group, name, values, dims, *, fill=None, **attrs):
    a = group.create_array(
        name,
        data=np.asarray(values),
        chunks=np.shape(values),
        fill_value=fill,
        compressor=None,
        config={"write_empty_chunks": True},
    )
    a.attrs.update({"_ARRAY_DIMENSIONS": dims, **attrs})
    return a


def fixture(path: Path, *, s2=False, dtype="int16", times=2):
    root = zarr.open_group(str(path), mode="w", zarr_format=2)
    root.attrs["status"] = "complete"
    records, sources, truth = [], [], []
    for t, date in enumerate(DATES[:times]):
        step = []
        for b, name in enumerate(["B04", "B08", "Fmask"]):
            source = {
                "asset_id": f"item{t}-{name}",
                "item_id": f"item{t}",
                "collection": "sentinel-2-c1-l2a" if s2 else "HLSS30",
                "requested_band": name,
                "datetime": date,
                "href": f"https://example.test/item{t}/{name}.tif",
                "acquisition": {"processing_baseline": "05.13"},
                "processing": "S30",
                "product_version": "2.0",
            }
            if s2:
                source["object_identity"] = {"etag": f'"etag-{t}-{name}"', "bytes": 1234}
            else:
                source["object_version"] = f'"etag-{t}-{name}"'
            group_name = f"observations/{len(records):06}"
            group = root.require_group(group_name)
            group.attrs.update(
                {
                    "asset_id": source["asset_id"],
                    "datetime": date,
                    "source": source,
                    "crs": CRS,
                    "transform": TRANSFORM,
                }
            )
            is_qa = name == "Fmask"
            raw = (
                np.zeros(SHAPE, dtype="uint8")
                if is_qa
                else (np.arange(12).reshape(SHAPE) + 100 * t + 10 * b + 1).astype(dtype)
            )
            scale = 1.0 if is_qa else 0.0001 * (b + 1)
            offset = 0.0 if is_qa else -0.1 * b
            fill = 255 if is_qa else None
            _array(
                group,
                "values",
                raw,
                ["time", "y", "x"],
                fill=fill,
                scale_factor=scale,
                add_offset=offset,
                units="1",
                grid_mapping="spatial_ref",
            )
            aoi = np.ones(SHAPE, dtype="uint8")
            aoi[:, -1] = 0
            valid = aoi.copy()
            valid[0, 0, b % 2] = 0
            for mask_name, data in [("valid", valid), ("aoi_mask", aoi)]:
                _array(group, mask_name, data, ["time", "y", "x"], fill=0)
            for axis, count, origin, delta in [
                ("x", 3, TRANSFORM[0], 30),
                ("y", 4, TRANSFORM[3], -30),
            ]:
                _array(group, axis, origin + (np.arange(count) + 0.5) * delta, [axis])
            _array(
                group,
                "spatial_ref",
                np.asarray(0, dtype="int32"),
                [],
                crs=CRS,
                GeoTransform=" ".join(map(str, TRANSFORM)),
            )
            _array(
                group,
                "time",
                np.array([_time_ns(date, "fixture")], dtype="int64"),
                ["time"],
                units="nanoseconds since 1970-01-01 00:00:00",
            )
            record = {
                "file": ".",
                "group": group_name,
                "dataset": "values",
                "asset_id": source["asset_id"],
                "datetime": date,
                "dtype": str(raw.dtype),
                "nodata": fill,
                "native_grid": {"crs": CRS, "transform": TRANSFORM},
                "dimensions": {"names": ["time", "y", "x"], "shape": list(SHAPE)},
            }
            if is_qa:
                qa = valid.copy()
                qa[0, 1, 1] = 0
                _array(
                    group,
                    f"quality_{POLICY}",
                    qa,
                    ["time", "y", "x"],
                    fill=0,
                    quality_policy=POLICY,
                )
                record["derived_masks"] = [{"dataset": f"quality_{POLICY}", "policy": POLICY}]
            else:
                step.append(raw[0])
            records.append(record)
            sources.append(source)
        truth.append(np.stack(step))
    manifest = {
        "document_type": "acquisition_result",
        "schema_version": 3,
        "status": "complete",
        "format": "zarr_v2",
        "outputs": records,
        "sources": sources,
    }
    (path / "manifest.json").write_text(json.dumps(manifest))
    return np.stack(truth)


def _hashes(path):
    return {
        str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in path.rglob("*")
        if p.is_file()
    }


def _edit_json(path, edit):
    value = json.loads(path.read_text())
    edit(value)
    path.write_text(json.dumps(value))


def test_separate_hls_qa_joins_without_casting_or_a_stacked_copy(tmp_path):
    source, out = tmp_path / "source", tmp_path / "out"
    truth = fixture(source)
    before = _hashes(source)
    report = chronozarr.convert_tile_ripper(
        source, out, bands=["B04", "B08"], quality_policy=POLICY, chunk_size=2, workers=1
    )
    assert report.levels[0].shape == (2, 2, 4, 3)
    store = chronozarr.open_store(out)
    for t in range(2):
        np.testing.assert_array_equal(store.read(t), truth[t])
        expected = np.ones((4, 3), dtype="uint8")
        expected[-1] = 0
        expected[0, :2] = 0
        expected[1, 1] = 0
        np.testing.assert_array_equal(store.read_mask(t), expected)
    assert [(b.scale, b.offset) for b in store.attrs.bands] == [(0.0001, -0.0), (0.0002, -0.1)]
    provenance = store.attrs.provenance
    assert provenance is not None
    notes = json.loads(provenance["notes"])
    assert notes["quality_policy"] == POLICY
    assert len(notes["observations"]) == 6
    assert (
        notes["manifest_sha256"]
        == hashlib.sha256((source / "manifest.json").read_bytes()).hexdigest()
    )
    assert all(o["source"]["object_version"] for o in notes["observations"])
    assert _hashes(source) == before
    assert not list(tmp_path.glob(".*-spill-*"))
    assert chronozarr.validate(out) == []


def test_earth_search_identity_schema_survives(tmp_path):
    source, out = tmp_path / "source", tmp_path / "out"
    fixture(source, s2=True)
    chronozarr.convert_tile_ripper(source, out, bands=["B04"], chunk_size=2, workers=1)
    provenance = chronozarr.open_store(out).attrs.provenance
    assert provenance is not None
    notes = json.loads(provenance["notes"])
    identities = [o["source"]["object_identity"] for o in notes["observations"]]
    assert identities == [
        {"etag": '"etag-0-B04"', "bytes": 1234},
        {"etag": '"etag-1-B04"', "bytes": 1234},
    ]
    assert all("object_version" not in o["source"] for o in notes["observations"])


@pytest.mark.parametrize("kind", ["missing_qa", "wrong_item", "processing", "grid"])
def test_missing_or_mismatched_quality_is_refused(tmp_path, kind):
    source, out = tmp_path / "source", tmp_path / "out"
    fixture(source)
    path = source / "observations/000002/.zattrs"
    manifest_path = source / "manifest.json"
    if kind == "missing_qa":
        _edit_json(manifest_path, lambda m: m["outputs"][2].pop("derived_masks"))
    elif kind in ("wrong_item", "processing"):
        key = "item_id" if kind == "wrong_item" else "processing"
        _edit_json(path, lambda a: a["source"].update({key: "another"}))
        _edit_json(manifest_path, lambda m: m["sources"][2].update({key: "another"}))
    else:
        _edit_json(path, lambda a: a.update({"transform": [500030.0, *TRANSFORM[1:]]}))
        _edit_json(
            manifest_path,
            lambda m: m["outputs"][2]["native_grid"].update(
                {"transform": [500030.0, *TRANSFORM[1:]]}
            ),
        )
    with pytest.raises(ValueError, match=r"QA observation|coordinates disagree|grids disagree"):
        chronozarr.convert_tile_ripper(source, out, bands=["B04", "B08"], quality_policy=POLICY)
    assert not out.exists()


def test_calibration_change_is_refused_per_band(tmp_path):
    source, out = tmp_path / "source", tmp_path / "out"
    fixture(source)
    _edit_json(
        source / "observations/000003/values/.zattrs", lambda a: a.update({"add_offset": 0.5})
    )
    with pytest.raises(ValueError, match="calibration/units changed for band B04"):
        chronozarr.convert_tile_ripper(source, out, bands=["B04", "B08"])
    assert not out.exists()


def test_duplicate_scene_at_one_instant_is_refused(tmp_path):
    source, out = tmp_path / "source", tmp_path / "out"
    fixture(source)
    # Different per-band item identities at one instant cannot assemble one frame.
    _edit_json(
        source / "observations/000001/.zattrs",
        lambda a: a["source"].update({"item_id": "another"}),
    )
    _edit_json(source / "manifest.json", lambda m: m["sources"][1].update({"item_id": "another"}))
    with pytest.raises(ValueError, match="ambiguous observations"):
        chronozarr.convert_tile_ripper(source, out, bands=["B04", "B08"])


def test_missing_band_cannot_shrink_the_requested_band_set(tmp_path):
    source, out = tmp_path / "source", tmp_path / "out"
    fixture(source)
    _edit_json(source / "manifest.json", lambda m: m["outputs"].pop(4))
    with pytest.raises(ValueError, match=r"missing requested bands.*B08"):
        chronozarr.convert_tile_ripper(source, out, bands=["B04", "B08"])


def test_missing_every_requested_band_at_a_source_date_does_not_drop_that_date(tmp_path):
    source, out = tmp_path / "source", tmp_path / "out"
    fixture(source)
    _edit_json(source / "manifest.json", lambda m: m["outputs"].pop(3))
    with pytest.raises(ValueError, match=r"missing requested bands.*B04"):
        chronozarr.convert_tile_ripper(source, out, bands=["B04"])
    assert not out.exists()


@pytest.mark.parametrize("kind", ["dtype", "source", "coordinate", "chunk"])
def test_corrupt_input_is_refused_with_context(tmp_path, kind):
    source, out = tmp_path / "source", tmp_path / "out"
    fixture(source)
    group = source / "observations/000000"
    if kind == "dtype":
        _edit_json(group / "values/.zarray", lambda a: a.update({"dtype": "<u2"}))
    elif kind == "source":
        _edit_json(group / ".zattrs", lambda a: a.pop("source"))
    elif kind == "coordinate":
        array = zarr.open_array(str(group / "x"), mode="r+")
        array[0] = float(np.asarray(array[0]).item()) + 0.1
    else:
        (group / "values/0.0.0").write_bytes(b"not a decoded int16 chunk")
    with pytest.raises(ValueError, match="observations/000000"):
        chronozarr.convert_tile_ripper(source, out, bands=["B04"])
    assert not out.exists()


def test_missing_uninitialized_chunk_is_refused(tmp_path):
    source, out = tmp_path / "source", tmp_path / "out"
    fixture(source)
    (source / "observations/000000/values/0.0.0").unlink()
    with pytest.raises(ValueError, match="missing uninitialized source chunk"):
        chronozarr.convert_tile_ripper(source, out, bands=["B04"])


def test_nan_payloads_are_preserved_and_never_valid(tmp_path):
    source, out = tmp_path / "source", tmp_path / "out"
    fixture(source, dtype="float32", times=1)
    array = zarr.open_array(str(source / "observations/000000/values"), mode="r+")
    data = np.asarray(array[:])
    data[0, 0, 0] = np.array([0x7FC01234], dtype="uint32").view("float32")[0]
    array[:] = data
    chronozarr.convert_tile_ripper(source, out, bands=["B04"], chunk_size=2, workers=1)
    store = chronozarr.open_store(out)
    assert store.read(0).tobytes() == data.tobytes()
    mask = store.read_mask(0)
    assert mask is not None
    assert mask[0, 0] == 0


@pytest.mark.parametrize("finite_policy", [False, True])
def test_valid_infinities_change_only_under_explicit_finite_quality_policy(
    tmp_path, finite_policy
):
    source, out = tmp_path / "source", tmp_path / "out"
    fixture(source, dtype="float32", times=1)
    group = zarr.open_group(str(source / "observations/000000"), mode="r+")
    values = group["values"]
    assert isinstance(values, zarr.Array)
    data = np.asarray(values[:])
    data[0, 1, 0] = np.inf
    data[0, 1, 2] = -np.inf
    values[:] = data
    policy = "opera_rtc_finite"
    mask_name = f"quality_{policy}"
    valid_array = group["valid"]
    assert isinstance(valid_array, zarr.Array)
    valid = np.asarray(valid_array[:])
    accepted = (np.isfinite(data) & (valid != 0)).astype("uint8")
    _array(group, mask_name, accepted, ["time", "y", "x"], fill=0, quality_policy=policy)
    _edit_json(
        source / "manifest.json",
        lambda m: m["outputs"][0].update(
            {"derived_masks": [{"dataset": mask_name, "policy": policy}]}
        ),
    )
    chronozarr.convert_tile_ripper(
        source,
        out,
        bands=["B04"],
        quality_policy=policy if finite_policy else None,
        chunk_size=4,
        workers=1,
    )
    store = chronozarr.open_store(out)
    assert store.read(0).tobytes() == data.tobytes()
    mask = store.read_mask(0)
    assert mask is not None
    assert mask[1, 0] == (0 if finite_policy else 1)
    assert mask[1, 2] == (0 if finite_policy else 1)


def test_budget_and_destination_checks_happen_before_encoding(tmp_path):
    source = tmp_path / "source"
    fixture(source)
    with pytest.raises(ValueError, match="max_timestep_bytes"):
        chronozarr.convert_tile_ripper(
            source, tmp_path / "out", bands=["B04"], max_timestep_bytes=32
        )
    before = _hashes(source)
    with pytest.raises(ValueError, match="outside the input"):
        chronozarr.convert_tile_ripper(source, source / "nested", bands=["B04"])
    assert _hashes(source) == before


def test_source_change_during_read_never_publishes_output(tmp_path, monkeypatch):
    import chronozarr.tile_ripper as adapter

    source, out = tmp_path / "source", tmp_path / "out"
    fixture(source)
    read = adapter._read
    changed = False

    def mutating_read(array, where, **kwargs):
        nonlocal changed
        value = read(array, where, **kwargs)
        if not changed:
            changed = True
            with (source / "manifest.json").open("a") as handle:
                handle.write(" ")
        return value

    monkeypatch.setattr(adapter, "_read", mutating_read)
    with pytest.raises(ValueError, match="input changed during import"):
        chronozarr.convert_tile_ripper(source, out, bands=["B04"], chunk_size=2, workers=1)
    assert not out.exists()


def test_collection_cannot_change_between_timesteps(tmp_path):
    source, out = tmp_path / "source", tmp_path / "out"
    fixture(source)
    _edit_json(
        source / "observations/000003/.zattrs",
        lambda a: a["source"].update({"collection": "another-collection"}),
    )
    _edit_json(
        source / "manifest.json",
        lambda m: m["sources"][3].update({"collection": "another-collection"}),
    )
    with pytest.raises(ValueError, match="different collection values"):
        chronozarr.convert_tile_ripper(source, out, bands=["B04"])
    assert not out.exists()


@pytest.mark.parametrize("kind", ["aoi", "valid"])
def test_qa_validity_and_aoi_are_checked_as_well_as_derived_quality(tmp_path, kind):
    source, out = tmp_path / "source", tmp_path / "out"
    fixture(source)
    name = "aoi_mask" if kind == "aoi" else "valid"
    array = zarr.open_array(str(source / f"observations/000002/{name}"), mode="r+")
    array[0, 1, 0] = 0
    with pytest.raises(ValueError, match=r"QA AOI mask differs|quality mask includes invalid QA"):
        chronozarr.convert_tile_ripper(source, out, bands=["B04"], quality_policy=POLICY)
    assert not out.exists()


def test_encoded_source_payload_is_admitted_before_reading_it(tmp_path):
    source, out = tmp_path / "source", tmp_path / "out"
    fixture(source)
    (source / "observations/000000/values/0.0.0").write_bytes(b"x" * 2048)
    with pytest.raises(ValueError, match="encoded source chunk exceeds max_timestep_bytes"):
        chronozarr.convert_tile_ripper(source, out, bands=["B04"], max_timestep_bytes=1024)
    assert not out.exists()


def test_each_native_value_plane_is_read_once(tmp_path, monkeypatch):
    import chronozarr.tile_ripper as adapter

    source, out = tmp_path / "source", tmp_path / "out"
    fixture(source)
    read = adapter._read
    calls = []

    def counted_read(array, where, **kwargs):
        if where.endswith("/values"):
            calls.append(where)
        return read(array, where, **kwargs)

    monkeypatch.setattr(adapter, "_read", counted_read)
    chronozarr.convert_tile_ripper(
        source, out, bands=["B04", "B08"], quality_policy=POLICY, chunk_size=2, workers=1
    )
    assert calls == [
        "observations/000000/values",
        "observations/000001/values",
        "observations/000003/values",
        "observations/000004/values",
    ]


@pytest.mark.parametrize("kind", ["record", "source", "attributes", "dimensions", "grid"])
def test_malformed_json_has_a_contextual_value_error(tmp_path, kind):
    source, out = tmp_path / "source", tmp_path / "out"
    fixture(source)
    if kind == "record":
        _edit_json(source / "manifest.json", lambda m: m["outputs"].__setitem__(0, []))
    elif kind == "source":
        _edit_json(source / "manifest.json", lambda m: m["sources"][0].update({"asset_id": {}}))
    elif kind == "attributes":
        (source / "observations/000000/.zattrs").write_text("[]")
    else:
        key = "dimensions" if kind == "dimensions" else "native_grid"
        _edit_json(source / "manifest.json", lambda m: m["outputs"][0].update({key: []}))
    with pytest.raises(ValueError, match=r"manifest|observations/000000"):
        chronozarr.convert_tile_ripper(source, out, bands=["B04"])
    assert not out.exists()


@pytest.mark.parametrize("chunks", [(1, 2, 3), (2, 8, 9)])
def test_plane_reads_are_aligned_single_chunk_selections(chunks):
    from chronozarr.tile_ripper import _read

    class ChunkChecked:
        shape = (1, 5, 7)
        dtype = np.dtype("float32")

        def __init__(self):
            self.chunks = chunks
            self.values = np.arange(35, dtype=self.dtype).reshape(self.shape)
            self.selections = []

        def __getitem__(self, selection):
            t, ys, xs = selection
            assert t == 0
            for axis, part in [(1, ys), (2, xs)]:
                assert part.start % self.chunks[axis] == 0
                assert part.start // self.chunks[axis] == (part.stop - 1) // self.chunks[axis]
            self.selections.append(selection)
            return self.values[selection]

    array = ChunkChecked()
    np.testing.assert_array_equal(_read(cast(zarr.Array, array), "fixture"), array.values[0])
    assert len(array.selections) == (9 if chunks == (1, 2, 3) else 1)


@pytest.mark.parametrize("chunk", [3, 11])
def test_coordinate_checks_read_one_aligned_chunk_including_padding(chunk):
    from chronozarr.tile_ripper import _check_axis

    class ChunkChecked:
        shape = (7,)
        chunks = (chunk,)

        def __init__(self):
            self.values = 9100000 + (np.arange(7, dtype="float64") + 0.5) * -30
            self.selections = []

        def __getitem__(self, selection):
            assert selection.start % chunk == 0
            assert selection.start // chunk == (selection.stop - 1) // chunk
            self.selections.append(selection)
            return self.values[selection]

    array = ChunkChecked()
    _check_axis(cast(zarr.Array, array), 9100000, -30, "fixture/y")
    assert len(array.selections) == (3 if chunk == 3 else 1)


def test_padded_qa_chunks_reserve_decode_and_result_scratch_before_reads(tmp_path):
    source, out = tmp_path / "source", tmp_path / "out"
    fixture(source)
    path = source / f"observations/000002/quality_{POLICY}/.zarray"
    _edit_json(path, lambda metadata: metadata.update({"chunks": [2, 8, 8]}))
    (path.parent / "0.0.0").write_bytes(bytes(128))
    # A padded chunk is 128 bytes, below this cap on its own; its decode/result
    # scratch is not, and must fail admission before invoking the decoder.
    with pytest.raises(ValueError, match="source chunk decode/result scratch"):
        chronozarr.convert_tile_ripper(
            source, out, bands=["B04"], quality_policy=POLICY, max_timestep_bytes=400
        )
    assert not out.exists()


@pytest.mark.parametrize(
    "change",
    [
        {"compressor": {"id": "zlib", "level": 1}},
        {"filters": [{"id": "delta", "dtype": "<i2"}]},
        {"order": "F"},
    ],
)
def test_compressed_filtered_or_fortran_chunks_are_refused_before_decode(tmp_path, change):
    source, out = tmp_path / "source", tmp_path / "out"
    fixture(source)
    path = source / "observations/000000/values/.zarray"
    _edit_json(path, lambda metadata: metadata.update(change))
    with pytest.raises(ValueError, match=r"uncompressed C-order|filters are outside"):
        chronozarr.convert_tile_ripper(source, out, bands=["B04"])
    assert not out.exists()
