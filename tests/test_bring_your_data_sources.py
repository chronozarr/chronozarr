"""Source fetch and audit examples use current ingest and conversion semantics, offline."""

from __future__ import annotations

import csv
import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pytest

rasterio = pytest.importorskip("rasterio")
pytest.importorskip("planetary_computer")
pytest.importorskip("pystac_client")

from chronozarr.convert import convert  # noqa: E402
from tests.test_ingest_mosaic import BBOX, EPSG, _grid, scene, write_tif  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples/sentinel2_pc"))
import catalog  # noqa: E402
import mosaic  # noqa: E402

pytestmark = pytest.mark.unit
REPO = Path(__file__).resolve().parents[1]


def load_example(relative: str):
    path = REPO / "examples/bring_your_data" / relative
    spec = importlib.util.spec_from_file_location("source_example", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("relative", ["fetch_sample.py", "extended/fetch.py", "large/fetch.py"])
def test_sample_fetchers_write_manifests_from_local_scene_assets(tmp_path, monkeypatch, relative):
    module = load_example(relative)
    grid = _grid()
    hrefs = {
        band: write_tif(
            tmp_path / f"{band}.tif",
            np.full((grid.height, grid.width), 2000 + i * 100, dtype=np.uint16),
            grid.transform,
            EPSG,
        )
        for i, band in enumerate(catalog.REQUIRED_BANDS)
    }
    hrefs["SCL"] = write_tif(
        tmp_path / "SCL.tif",
        np.full((grid.height, grid.width), 4, dtype=np.uint8),
        grid.transform,
        EPSG,
    )
    refs = [scene(f"scene-{m}", date(2020, m, 1), hrefs) for m in range(1, 13)]
    by_month = {f"2020-{m:02d}": [refs[m - 1]] for m in range(1, 13)}
    monkeypatch.setattr(catalog, "search_scenes_by_month", lambda *a, **kw: by_month)
    monkeypatch.setattr(module.pc, "sign", str)
    monkeypatch.setattr(mosaic, "sign_href", str)
    monkeypatch.setattr(module, "BBOX", BBOX)
    monkeypatch.setattr(module, "EPSG", EPSG)
    # The large variant pins the twelve IDs from an earlier run. Supply a local equivalent.
    project = tmp_path / "project"
    pinned = project / "examples/bring_your_data/extended/results/source.json"
    pinned.parent.mkdir(parents=True)
    pinned.write_text(json.dumps({"scenes": [{"id": ref.item_id} for ref in refs]}))
    monkeypatch.setattr(module, "REPO", project)
    output = tmp_path / "observations"
    module.fetch_sample(output)
    with (output / "observations.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == (3 if relative == "fetch_sample.py" else 12)
    for row in rows:
        with rasterio.open(output / row["uri"]) as source:
            assert source.shape == (grid.height, grid.width)
            assert source.crs.to_epsg() == EPSG
            assert source.scales == (0.0001,) * 4
            np.testing.assert_array_equal(source.read()[:, 0, 0], [1000, 1100, 1200, 1300])
            assert (source.dataset_mask() == 255).all()
    provenance = json.loads((output / "source.json").read_text())
    assert len(provenance["scenes"]) == len(rows)


@pytest.mark.parametrize(
    "dates",
    [
        ["2024-02-01", "2024-01-01"],
        ["2024-01-02T00:00:00+02:00", "2024-01-01T23:00:00Z"],
    ],
)
def test_audit_helpers_match_converter_order_and_utc_times(tmp_path, dates):
    grid = _grid()
    files = []
    for i in range(2):
        path = tmp_path / f"frame-{i}.tif"
        with rasterio.open(
            path,
            "w",
            driver="GTiff",
            height=2,
            width=2,
            count=1,
            dtype="uint16",
            crs=f"EPSG:{EPSG}",
            transform=grid.transform,
        ) as dest:
            dest.write(np.full((1, 2, 2), 100 + i * 100, dtype=np.uint16))
            dest.write_mask(np.array([[255, 255], [255, 0]], dtype=np.uint8))
            dest.scales = (0.01,)
        files.append(path)
    manifest = tmp_path / "observations.csv"
    with manifest.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["uri", "datetime"])
        writer.writeheader()
        writer.writerows(
            {"uri": path.name, "datetime": when} for path, when in zip(files, dates, strict=True)
        )
    store, plain = tmp_path / "store", tmp_path / "plain"
    convert(manifest, store, n_lods=1)
    convert(manifest, plain, n_lods=1)
    verification = tmp_path / "verification.json"
    load_example("verify.py").verify(manifest, store, plain, verification)
    assert json.loads(verification.read_text())["checks"]["stored_values"] == "exact"
    comparison = tmp_path / "comparison.json"
    load_example("compare_local.py").compare(manifest, store, plain, comparison, repeats=1)
    assert json.loads(comparison.read_text())["all_values_and_masks_match"]
