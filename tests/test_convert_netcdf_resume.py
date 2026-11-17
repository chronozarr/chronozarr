"""Interrupted local NetCDF imports can only reuse staging from the same input revision."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import xarray as xr

import chronozarr
from chronozarr.convert import convert
from tests.synthetic import CRS, TRANSFORM, make_times

pytestmark = pytest.mark.unit

pytest.importorskip("rasterio")
h5netcdf = pytest.importorskip("h5netcdf")

OPTIONS: dict[str, Any] = {
    "variable": "reflectance",
    "nodata": None,
    "chunk_size": 8,
    "read_ahead": 1,
}
SHAPE = (8, 8)


class InterruptedConversion(Exception):
    pass


def _write_netcdf(path: Path, start: int, count: int, pixel: int) -> None:
    a, _, c, _, e, f = TRANSFORM
    dataset = xr.Dataset(
        {"reflectance": (("time", "y", "x"), np.full((count, *SHAPE), pixel, dtype=np.uint16))},
        coords={
            "time": make_times(start + count)[start:],
            "x": c + a * (np.arange(SHAPE[1]) + 0.5),
            "y": f + e * (np.arange(SHAPE[0]) + 0.5),
        },
        attrs={"crs": CRS},
    )
    dataset.to_netcdf(path, engine="h5netcdf")


def _interrupted_stage(source: Path, out: Path) -> Path:
    def interrupt(done: int, _total: int) -> None:
        if done == 1:
            raise InterruptedConversion

    with pytest.raises(InterruptedConversion):
        convert(source, out, progress=interrupt, **OPTIONS)
    work = out.with_name(f"{out.name}.convert-work")
    assert not out.exists()
    assert sorted(p.name for p in work.glob("t*.npy")) == ["t000000.npy"]
    return work


def _pixels(out: Path) -> list[int]:
    return chronozarr.open_store(out).to_xarray().values[:, 0, 0, 0].tolist()


@pytest.mark.parametrize("series", [False, True], ids=["one-file", "series"])
@pytest.mark.parametrize("update", ["replacement", "in-place"])
def test_resume_refuses_changed_local_netcdf(tmp_path, series, update):
    source = tmp_path / "input.nc"
    first = source
    if series:
        source = tmp_path / "series"
        source.mkdir()
        first = source / "01.nc"
        _write_netcdf(source / "02.nc", 1, 1, 100)
    count = 1 if series else 2
    _write_netcdf(first, 0, count, 100)
    out = tmp_path / "store"
    work = _interrupted_stage(source, out)
    before = first.stat()

    if update == "replacement":
        replacement = tmp_path / "replacement.nc"
        _write_netcdf(replacement, 0, count, 900)
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        replacement.replace(first)
    else:
        with h5netcdf.File(first, "r+") as f:
            f.variables["reflectance"][:] = np.uint16(900)
        # On Windows ctime is the birth time, so keep the new mtime for in-place writes.
        mtime = before.st_mtime_ns if os.name != "nt" else before.st_mtime_ns + 2_000_000_000
        os.utime(first, ns=(before.st_atime_ns, mtime))
        assert first.stat().st_ino == before.st_ino
    assert first.stat().st_size == before.st_size
    if update == "replacement" or os.name != "nt":
        assert first.stat().st_mtime_ns == before.st_mtime_ns

    with pytest.raises(ValueError, match="without --resume in a fresh work directory") as error:
        convert(source, out, resume=True, **OPTIONS)
    assert "Keep the staged timesteps" in str(error.value)
    assert not out.exists()
    np.testing.assert_array_equal(np.load(work / "t000000.npy"), 100)

    report = convert(source, out, work_dir=tmp_path / "fresh", **OPTIONS)
    assert (report.n_reused, report.n_staged) == (0, 2)
    assert _pixels(out) == [900, 100 if series else 900]
    np.testing.assert_array_equal(np.load(work / "t000000.npy"), 100)


def test_unchanged_single_netcdf_resumes(tmp_path):
    source = tmp_path / "input.nc"
    _write_netcdf(source, 0, 2, 100)
    out = tmp_path / "store"
    work = _interrupted_stage(source, out)

    report = convert(source, out, resume=True, **OPTIONS)
    assert (report.n_reused, report.n_staged) == (1, 1)
    assert _pixels(out) == [100, 100]
    assert not work.exists()


def test_resume_refuses_netcdf_staged_without_file_identity(tmp_path):
    source = tmp_path / "input.nc"
    _write_netcdf(source, 0, 2, 100)
    out = tmp_path / "store"
    work = _interrupted_stage(source, out)
    plan_file = work / "plan.json"
    old_plan = json.loads(plan_file.read_text())["plan"]
    old_plan.pop("local_files", None)
    digest = hashlib.sha256(json.dumps(old_plan, sort_keys=True).encode()).hexdigest()
    plan_file.write_text(json.dumps({"sha256": digest, "plan": old_plan}))

    with pytest.raises(ValueError, match="without --resume in a fresh work directory"):
        convert(source, out, resume=True, **OPTIONS)
    assert not out.exists()
    np.testing.assert_array_equal(np.load(work / "t000000.npy"), 100)


@pytest.mark.parametrize("when", ["on-plan", "after-staging"])
def test_convert_refuses_netcdf_replaced_after_planning(tmp_path, when):
    source = tmp_path / "input.nc"
    replacement = tmp_path / "replacement.nc"
    _write_netcdf(source, 0, 2, 100)
    _write_netcdf(replacement, 0, 2, 900)
    out = tmp_path / "store"

    def replace_after_plan(_plan) -> None:
        replacement.replace(source)

    def replace_after_staging(done: int, total: int) -> None:
        if done == total:
            replacement.replace(source)

    options: dict[str, Any] = (
        {"on_plan": replace_after_plan}
        if when == "on-plan"
        else {"progress": replace_after_staging}
    )
    with pytest.raises(ValueError, match="source file changed during conversion") as error:
        convert(source, out, **OPTIONS, **options)
    assert source.name in str(error.value)
    assert not out.exists()
    if when == "after-staging":
        notes = "\n".join(error.value.__notes__)
        assert "fresh work directory with --work-dir" in notes
        assert "--resume" not in notes
        np.testing.assert_array_equal(np.load(tmp_path / "store.convert-work/t000001.npy"), 100)
