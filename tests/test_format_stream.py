"""Streaming encode: iterable and dask input, bounded memory, temp file hygiene."""

from __future__ import annotations

import errno
import importlib
import os
import shutil
import stat
import struct
import subprocess
import sys
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from itertools import count
from pathlib import Path
from threading import Barrier, Event
from typing import Any

import numpy as np
import pytest
import xarray as xr

import chronozarr
from chronozarr import schema
from tests.narrow import required
from tests.synthetic import CRS, TRANSFORM, make_da, make_times, make_truth

pytestmark = pytest.mark.unit

CS = 8


def _tree(path: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(path)): p.read_bytes() for p in sorted(path.rglob("*")) if p.is_file()
    }


def _stream_kwargs(truth: np.ndarray) -> dict[str, Any]:
    return {
        "times": make_times(truth.shape[0]),
        "bands": [f"b{i}" for i in range(truth.shape[1])],
        "crs": CRS,
        "transform": TRANSFORM,
    }


def _spill_dirs(parent: Path) -> list[Path]:
    return [p for p in parent.iterdir() if "spill" in p.name]


@pytest.mark.parametrize("shard", [True, False], ids=["sharded", "unsharded"])
def test_iterable_input_writes_the_same_store_as_a_dataarray(tmp_path, shard):
    truth = make_truth(5, 2, 29, 21)
    mask = (truth[:, 0] > 0).astype(np.uint8)
    coverage = (mask * 4).astype(np.uint8)
    kwargs: dict[str, Any] = {
        "chunk_size": CS,
        "shard": shard,
        "shard_time": 2 if shard else None,
        "provenance": {"sources": ["x"], "composite": "median", "gap_fill": "none"},
    }
    da = make_da(truth, ["b0", "b1"])
    chronozarr.encode(da, tmp_path / "array", mask=mask, coverage=coverage, **kwargs)
    chronozarr.encode(
        iter(truth),
        tmp_path / "iter",
        mask=iter(mask),
        coverage=iter(coverage),
        **_stream_kwargs(truth),
        **kwargs,
    )
    assert _tree(tmp_path / "iter") == _tree(tmp_path / "array")
    assert chronozarr.validate(tmp_path / "iter") == []
    store = chronozarr.open_store(tmp_path / "iter")
    assert np.array_equal(store.to_xarray().values, truth)
    assert np.array_equal(required(store.read_mask(3)), mask[3])


def test_generator_timesteps_are_consumed_once_in_order(tmp_path):
    truth = make_truth(4, 1, 20, 20)
    pulled: list[int] = []

    def steps():
        for t in range(4):
            pulled.append(t)
            yield truth[t].copy()

    chronozarr.encode(steps(), tmp_path / "s", chunk_size=CS, **_stream_kwargs(truth))
    assert pulled == [0, 1, 2, 3]
    assert np.array_equal(chronozarr.open_store(tmp_path / "s").to_xarray().values, truth)


def test_dask_backed_input_is_encoded_cell_by_cell(tmp_path):
    da_dask = pytest.importorskip("dask.array")
    truth = make_truth(4, 2, 29, 21)
    da = make_da(truth, ["b0", "b1"])
    lazy = da.copy(data=da_dask.from_array(truth, chunks=(1, 2, CS, CS)))
    chronozarr.encode(lazy, tmp_path / "lazy", chunk_size=CS)
    chronozarr.encode(da, tmp_path / "eager", chunk_size=CS)
    assert _tree(tmp_path / "lazy") == _tree(tmp_path / "eager")


def test_workers_does_not_change_the_output(tmp_path):
    truth = make_truth(4, 1, 29, 21)
    trees = []
    for workers in (1, 3):
        out = tmp_path / f"w{workers}"
        chronozarr.encode(make_da(truth), out, chunk_size=CS, workers=workers)
        trees.append(_tree(out))
    assert trees[0] == trees[1]


# --- temp files and failures ------------------------------------------------------------------


def test_spill_files_are_removed_after_success(tmp_path):
    truth = make_truth(3, 1, 20, 20)
    chronozarr.encode(iter(truth), tmp_path / "s", chunk_size=CS, **_stream_kwargs(truth))
    assert _spill_dirs(tmp_path) == []


def test_spill_dir_is_used_and_cleaned(tmp_path):
    truth = make_truth(3, 1, 20, 20)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    seen: list[int] = []

    def steps():
        for t in range(3):
            seen.append(len(list(scratch.iterdir())))
            yield truth[t]

    chronozarr.encode(
        steps(), tmp_path / "s", chunk_size=CS, spill_dir=scratch, **_stream_kwargs(truth)
    )
    assert seen[1:] == [1, 1]  # timestep 0 is peeked before the spill directory is made
    assert list(scratch.iterdir()) == []
    assert _spill_dirs(tmp_path) == []


def test_failing_iterator_leaves_no_store_and_no_spill_files(tmp_path):
    truth = make_truth(4, 1, 20, 20)

    def steps():
        yield truth[0]
        yield truth[1]
        raise RuntimeError("download failed at timestep 2")

    with pytest.raises(RuntimeError, match="download failed at timestep 2"):
        chronozarr.encode(steps(), tmp_path / "s", chunk_size=CS, **_stream_kwargs(truth))
    assert not (tmp_path / "s").exists()
    assert _spill_dirs(tmp_path) == []


_EMPTY_DESTINATION = pytest.param(
    True,
    id="empty",
    marks=pytest.mark.skipif(os.name == "nt", reason="Windows rename requires an absent output"),
)


@pytest.mark.parametrize("empty_destination", [False, _EMPTY_DESTINATION])
@pytest.mark.parametrize("iterator_fails", [False, True], ids=["write", "spill_failure"])
def test_encode_preserves_a_store_created_during_spill(
    tmp_path, empty_destination, iterator_fails
):
    path = tmp_path / "s"
    if empty_destination:
        path.mkdir()
    truth = make_truth(2, 1, 8, 8)
    foreign_tree = {}

    def steps():
        yield truth[0]
        xr.Dataset({"foreign": ("x", np.array([23, 42], dtype=np.uint16))}).to_zarr(
            path, mode="w", zarr_format=3, consolidated=False
        )
        foreign_tree.update(_tree(path))
        if iterator_fails:
            raise RuntimeError("next timestep failed")
        yield truth[1]

    error = RuntimeError if iterator_fails else FileExistsError
    message = "next timestep failed" if iterator_fails else "stores are immutable"
    with pytest.raises(error, match=message):
        chronozarr.encode(steps(), path, chunk_size=CS, **_stream_kwargs(truth))

    assert foreign_tree and _tree(path) == foreign_tree
    assert _spill_dirs(tmp_path) == []


@pytest.mark.parametrize("empty_destination", [False, _EMPTY_DESTINATION])
def test_competing_encodes_publish_the_destination_once(tmp_path, empty_destination, monkeypatch):
    path = tmp_path / "s"
    if empty_destination:
        path.mkdir()
    truth = make_truth(2, 1, 8, 8)
    candidates = [truth, truth + 1000]
    right_before_spilling = Barrier(2)
    second_consolidating = Event()
    first_finished = Event()
    attempts = count()
    consolidate = importlib.import_module("zarr").consolidate_metadata

    def consolidate_in_order(*args, **kwargs):
        if next(attempts) == 0:
            # If both encodes can change the warning filter at once, make the later scope
            # stay open until the first has already restored the original filter.
            second_consolidating.wait(timeout=1)
        else:
            second_consolidating.set()
            assert first_finished.wait(10)
        return consolidate(*args, **kwargs)

    monkeypatch.setattr("zarr.consolidate_metadata", consolidate_in_order)

    def encode_frames(frames):
        def steps():
            yield frames[0]
            # Both calls passed the first check of the empty destination.
            right_before_spilling.wait(timeout=10)
            yield frames[1]

        try:
            return chronozarr.encode(steps(), path, chunk_size=CS, **_stream_kwargs(frames))
        finally:
            first_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(encode_frames, candidate) for candidate in candidates]
        winners = []
        for job, candidate in zip(jobs, candidates, strict=True):
            try:
                job.result()
            except FileExistsError:
                continue
            winners.append(candidate)

    assert len(winners) == 1
    assert chronozarr.validate(path) == []
    np.testing.assert_array_equal(chronozarr.open_store(path).to_xarray().values, winners[0])
    assert _spill_dirs(tmp_path) == []


@pytest.mark.parametrize("replace_directory", [False, True], ids=["root", "directory"])
def test_encode_preserves_a_replacement_when_root_finalization_fails(
    tmp_path, monkeypatch, replace_directory
):
    path = tmp_path / "s"
    replacement = tmp_path / "replacement"
    xr.Dataset({"foreign": ("x", np.array([23, 42], dtype=np.uint16))}).to_zarr(
        replacement, mode="w", zarr_format=3, consolidated=False
    )
    foreign_tree = _tree(replacement)

    def replace_output(_):
        if replace_directory:
            if path.exists():
                path.rename(tmp_path / "original")
            replacement.rename(path)
        else:
            # Publish a new root and array in the same directory.
            path.mkdir(exist_ok=True)
            (replacement / "foreign").rename(path / "foreign")
            (replacement / "zarr.json").replace(path / "zarr.json")
        raise RuntimeError("root finalization failed")

    monkeypatch.setattr(schema.RootAttrs, "to_attrs", replace_output)
    with pytest.raises(RuntimeError, match="root finalization failed"):
        chronozarr.encode(make_da(make_truth(2, 1, 8, 8)), path, chunk_size=CS)

    assert all((path / name).read_bytes() == value for name, value in foreign_tree.items())


def test_encode_never_writes_chunks_into_a_replacement_store(tmp_path, monkeypatch):
    path = tmp_path / "s"
    replacement = tmp_path / "replacement"
    truth = make_truth(2, 1, 8, 8)
    chronozarr.encode(make_da(truth + 1000), replacement, chunk_size=CS)
    foreign_tree = _tree(replacement)

    encode_module = importlib.import_module("chronozarr.encode")
    write_cell = encode_module._encode_cell

    def install_replacement_then_write(*args, **kwargs):
        # The writer has opened its group and built its array handles.
        if path.exists():
            path.rename(tmp_path / "original")
        replacement.rename(path)
        return write_cell(*args, **kwargs)

    monkeypatch.setattr(encode_module, "_encode_cell", install_replacement_then_write)
    with pytest.raises(FileExistsError, match="stores are immutable"):
        chronozarr.encode(make_da(truth), path, chunk_size=CS, workers=1)

    assert _tree(path) == foreign_tree


@pytest.mark.parametrize(
    "collision",
    [
        "directory",
        "file",
        pytest.param(
            "symlink",
            marks=pytest.mark.skipif(
                os.name == "nt", reason="creating a Windows symlink can require extra privileges"
            ),
        ),
    ],
)
def test_encode_rename_does_not_replace_a_competing_destination(tmp_path, monkeypatch, collision):
    path = tmp_path / "s"
    foreign = tmp_path / "foreign"
    truth = make_truth(2, 1, 8, 8)
    chronozarr.encode(make_da(truth + 1000), foreign, chunk_size=CS)
    foreign_tree = _tree(foreign)
    rename = os.rename

    def publish_competitor_then_rename(src, dst, *args, **kwargs):
        if Path(dst) == path:
            if collision == "symlink":
                path.symlink_to(foreign, target_is_directory=True)
            elif collision == "file":
                path.write_bytes(b"foreign file")
            else:
                rename(foreign, path)
        return rename(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "rename", publish_competitor_then_rename)
    with pytest.raises(FileExistsError, match="stores are immutable"):
        chronozarr.encode(make_da(truth), path, chunk_size=CS)

    if collision == "symlink":
        assert path.is_symlink() and path.resolve() == foreign
    if collision == "file":
        assert path.read_bytes() == b"foreign file"
    else:
        assert _tree(path) == foreign_tree


def test_encode_cleanup_preserves_a_store_published_during_cleanup(tmp_path, monkeypatch):
    path = tmp_path / "s"
    replacement = tmp_path / "replacement"
    truth = make_truth(2, 1, 8, 8)
    chronozarr.encode(make_da(truth + 1000), replacement, chunk_size=CS)
    foreign_tree = _tree(replacement)
    failed = False
    remove_tree = shutil.rmtree

    def fail(_):
        nonlocal failed
        failed = True
        raise RuntimeError("root finalization failed")

    def install_replacement_then_remove(target, *args, **kwargs):
        if failed and replacement.exists():
            if path.exists():
                path.rename(tmp_path / "original")
            replacement.rename(path)
        return remove_tree(target, *args, **kwargs)

    monkeypatch.setattr(schema.RootAttrs, "to_attrs", fail)
    monkeypatch.setattr(shutil, "rmtree", install_replacement_then_remove)
    with pytest.raises(RuntimeError, match="root finalization failed"):
        chronozarr.encode(make_da(truth), path, chunk_size=CS)

    assert _tree(path) == foreign_tree


@pytest.mark.parametrize("empty_destination", [False, _EMPTY_DESTINATION])
def test_encode_leaves_no_output_when_collecting_its_report_fails(
    tmp_path, monkeypatch, empty_destination
):
    path = tmp_path / "s"
    if empty_destination:
        path.mkdir()

    def fail(_):
        raise OSError("report size calculation failed")

    encode_module = importlib.import_module("chronozarr.encode")
    monkeypatch.setattr(encode_module, "_tree_stats", fail)
    with pytest.raises(OSError, match="report size calculation failed"):
        chronozarr.encode(make_da(make_truth(2, 1, 8, 8)), path, chunk_size=CS)

    assert list(tmp_path.iterdir()) == ([path] if empty_destination else [])
    if empty_destination:
        assert list(path.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="Windows rename requires an absent output")
def test_encode_refuses_a_symlink_retargeted_during_spill(tmp_path):
    path = tmp_path / "s"
    first_target = tmp_path / "empty"
    first_target.mkdir()
    path.symlink_to(first_target, target_is_directory=True)
    replacement = tmp_path / "replacement"
    truth = make_truth(2, 1, 8, 8)
    chronozarr.encode(make_da(truth + 1000), replacement, chunk_size=CS)
    foreign_tree = _tree(replacement)

    def steps():
        yield truth[0]
        path.unlink()
        path.symlink_to(replacement, target_is_directory=True)
        yield truth[1]

    with pytest.raises(FileExistsError, match="output destination changed"):
        chronozarr.encode(steps(), path, chunk_size=CS, **_stream_kwargs(truth))

    assert path.resolve() == replacement.resolve()
    assert list(first_target.iterdir()) == []
    assert _tree(replacement) == foreign_tree


@pytest.mark.skipif(os.name == "nt", reason="Windows rename requires an absent output")
def test_encode_through_a_stable_output_symlink_preserves_target_permissions(tmp_path):
    path = tmp_path / "s"
    target = tmp_path / "target"
    target.mkdir()
    target.chmod(0o750)
    path.symlink_to(target, target_is_directory=True)

    truth = make_truth(2, 1, 8, 8)
    chronozarr.encode(make_da(truth), path, chunk_size=CS)

    assert path.is_symlink()
    assert stat.S_IMODE(target.stat().st_mode) == 0o750
    np.testing.assert_array_equal(chronozarr.open_store(path).to_xarray().values, truth)


@pytest.mark.skipif(os.name == "nt", reason="Windows rename requires an absent output")
@pytest.mark.parametrize("mode", [0o700, 0o555], ids=["owner-only", "read-only"])
def test_encode_honors_permissions_tightened_during_spill(tmp_path, mode, monkeypatch):
    if mode == 0o555 and os.geteuid() == 0:
        pytest.skip("root can write inside read-only directories")
    path = tmp_path / "s"
    path.mkdir(mode=0o755)
    truth = make_truth(2, 1, 8, 8)

    def steps():
        yield truth[0]
        path.chmod(mode)
        yield truth[1]

    def encode():
        return chronozarr.encode(steps(), path, chunk_size=CS, **_stream_kwargs(truth))

    if mode == 0o555:

        def check_permissions_before_writing(*args, **kwargs):
            pytest.fail("started writing despite the destination becoming read-only")

        encode_module = importlib.import_module("chronozarr.encode")
        monkeypatch.setattr(encode_module, "_write_store", check_permissions_before_writing)
        with pytest.raises(PermissionError, match="output directory must be writable"):
            encode()
        assert list(path.iterdir()) == []
    else:
        encode()
        np.testing.assert_array_equal(chronozarr.open_store(path).to_xarray().values, truth)
    assert stat.S_IMODE(path.stat().st_mode) == mode


@pytest.mark.skipif(os.name == "nt", reason="Windows rename requires an absent output")
def test_encode_refuses_permissions_changed_after_the_private_store_is_prepared(
    tmp_path, monkeypatch
):
    path = tmp_path / "s"
    path.mkdir(mode=0o755)
    encode_module = importlib.import_module("chronozarr.encode")
    write_store = encode_module._write_store

    def tighten_after_writing(*args, **kwargs):
        report = write_store(*args, **kwargs)
        path.chmod(0o700)
        return report

    monkeypatch.setattr(encode_module, "_write_store", tighten_after_writing)
    with pytest.raises(FileExistsError, match="output destination changed"):
        chronozarr.encode(make_da(make_truth(2, 1, 8, 8)), path, chunk_size=CS)
    assert stat.S_IMODE(path.stat().st_mode) == 0o700
    assert list(tmp_path.iterdir()) == [path]
    assert list(path.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="Windows rename requires an absent output")
def test_encode_needs_write_access_to_the_destination_parent(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root can write inside read-only directories")
    parent = tmp_path / "protected"
    parent.mkdir()
    path = parent / "s"
    path.mkdir()
    parent.chmod(0o555)

    try:
        with pytest.raises(
            PermissionError, match="atomic publication needs a writable destination parent"
        ):
            chronozarr.encode(make_da(make_truth(2, 1, 8, 8)), path, chunk_size=CS)
    finally:
        parent.chmod(0o700)
    assert list(parent.iterdir()) == [path]
    assert list(path.iterdir()) == []


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS extended ACLs")
def test_encode_preserves_a_darwin_directory_acl_and_its_inheritance(tmp_path):
    path = tmp_path / "s"
    path.mkdir(mode=0o750)
    rule = "everyone allow readsecurity,file_inherit,directory_inherit"
    subprocess.run(["/bin/chmod", "+a", rule, str(path)], check=True)

    def acl(directory: Path) -> list[str]:
        result = subprocess.run(
            ["/bin/ls", "-lde", str(directory)], check=True, capture_output=True, text=True
        )
        return result.stdout.splitlines()[1:]

    expected = acl(path)
    chronozarr.encode(make_da(make_truth(2, 1, 8, 8)), path, chunk_size=CS)
    assert acl(path) == expected
    assert any("inherited allow readsecurity" in line for line in acl(path / "0"))


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS extended ACLs")
def test_encode_refuses_the_darwin_acl_of_an_unwritable_empty_destination(tmp_path, monkeypatch):
    path = tmp_path / "s"
    path.mkdir()
    subprocess.run(["/bin/chmod", "+a", "everyone deny add_subdirectory", str(path)], check=True)
    if os.access(path, os.W_OK):
        pytest.skip("the effective user can write despite the ACL")

    def check_permissions_before_writing(*args, **kwargs):
        pytest.fail("started writing despite the destination ACL")

    encode_module = importlib.import_module("chronozarr.encode")
    monkeypatch.setattr(encode_module, "_write_store", check_permissions_before_writing)
    with pytest.raises(PermissionError, match="output directory must be writable"):
        chronozarr.encode(make_da(make_truth(2, 1, 8, 8)), path, chunk_size=CS)
    assert list(path.iterdir()) == []
    assert not os.access(path, os.W_OK)


def test_encode_preserves_linux_access_and_default_acls(tmp_path):
    if sys.platform != "linux":
        pytest.skip("Linux POSIX ACL xattrs")
    path = tmp_path / "s"
    path.mkdir(mode=0o750)
    # Linux's version 2 ACL xattr: owner rwx, named user r-x, group r-x, mask r-x, other none.
    acl = struct.pack("<I", 2) + b"".join(
        struct.pack("<HHI", tag, mode, uid)
        for tag, mode, uid in [
            (1, 7, 0xFFFFFFFF),
            (2, 5, 12345),
            (4, 5, 0xFFFFFFFF),
            (16, 5, 0xFFFFFFFF),
            (32, 0, 0xFFFFFFFF),
        ]
    )
    attributes = ("system.posix_acl_access", "system.posix_acl_default")
    try:
        for name in attributes:
            os.setxattr(path, name, acl)
    except OSError as error:
        if error.errno != errno.ENOTSUP:
            raise
        pytest.skip("the fixture filesystem has no POSIX ACL support")

    chronozarr.encode(make_da(make_truth(2, 1, 8, 8)), path, chunk_size=CS)
    for directory in (path, path / "0"):
        for name in attributes:
            assert os.getxattr(directory, name) == acl


@pytest.mark.skipif(os.name != "nt", reason="Windows directory publication")
def test_encode_requires_an_absent_destination_on_windows(tmp_path):
    path = tmp_path / "s"
    path.mkdir()

    with pytest.raises(FileExistsError, match="on Windows"):
        chronozarr.encode(make_da(make_truth(2, 1, 8, 8)), path, chunk_size=CS)

    assert list(path.iterdir()) == []


@pytest.mark.parametrize("empty_destination", [False, _EMPTY_DESTINATION])
def test_encode_failure_during_consolidation_cleans_its_own_store(
    tmp_path, monkeypatch, empty_destination
):
    path = tmp_path / "s"
    if empty_destination:
        path.mkdir()

    def fail(_):
        raise RuntimeError("consolidation failed")

    monkeypatch.setattr("zarr.consolidate_metadata", fail)
    with pytest.raises(RuntimeError, match="consolidation failed"):
        chronozarr.encode(make_da(make_truth(2, 1, 8, 8)), path, chunk_size=CS)
    if empty_destination:
        assert list(path.iterdir()) == []
    else:
        assert not path.exists()


@pytest.mark.skipif(os.name == "nt", reason="Windows rename requires an absent output")
def test_encode_can_spill_on_the_filesystem_of_an_initially_empty_destination(tmp_path):
    path = tmp_path / "s"
    path.mkdir()
    truth = make_truth(2, 1, 8, 8)

    chronozarr.encode(iter(truth), path, chunk_size=CS, spill_dir=path, **_stream_kwargs(truth))

    assert chronozarr.validate(path) == []
    np.testing.assert_array_equal(chronozarr.open_store(path).to_xarray().values, truth)
    assert _spill_dirs(path) == []


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda steps: steps[:2], "the input has 2 timesteps but times has 4"),
        (lambda steps: [*steps, steps[0]], "more than the 4 timesteps"),
        (lambda steps: [steps[0], steps[1][:, :5], *steps[2:]], "timestep 1 is uint16"),
        (lambda steps: [steps[0], steps[1].astype(np.int16), *steps[2:]], "timestep 1 is int16"),
        (lambda steps: [], "empty input"),
    ],
)
def test_iterable_shape_dtype_and_count_are_checked(tmp_path, mutate, message):
    truth = make_truth(4, 1, 20, 20)
    with pytest.raises(ValueError, match=message):
        chronozarr.encode(
            iter(mutate(list(truth))), tmp_path / "s", chunk_size=CS, **_stream_kwargs(truth)
        )
    assert not (tmp_path / "s").exists()
    assert _spill_dirs(tmp_path) == []


def test_iterable_input_needs_times_crs_and_transform(tmp_path):
    truth = make_truth(2, 1, 8, 8)
    kwargs = _stream_kwargs(truth)
    for missing, message in (
        ("times", "needs times="),
        ("crs", "no crs"),
        ("transform", "no transform"),
    ):
        args = {k: v for k, v in kwargs.items() if k != missing}
        with pytest.raises(ValueError, match=message):
            chronozarr.encode(iter(truth), tmp_path / "s", chunk_size=CS, **args)


def test_a_dataarray_rejects_times_keyword(tmp_path):
    with pytest.raises(ValueError, match="times= is only for iterable input"):
        chronozarr.encode(
            make_da(make_truth(2, 1, 8, 8)),
            tmp_path / "s",
            chunk_size=CS,
            times=make_times(2),
        )


def test_short_mask_iterator_is_reported(tmp_path):
    truth = make_truth(3, 1, 8, 8)
    mask = np.ones((2, 8, 8), dtype=np.uint8)
    with pytest.raises(ValueError, match="mask ended at timestep 2"):
        chronozarr.encode(
            iter(truth), tmp_path / "s", chunk_size=CS, mask=iter(mask), **_stream_kwargs(truth)
        )


# --- memory -----------------------------------------------------------------------------------


def test_peak_memory_is_a_fraction_of_the_raster(tmp_path):
    n_time, n_band, size, cs = 24, 2, 1024, 128
    full_bytes = n_time * n_band * size * size * 2
    rng = np.random.default_rng(1)

    def steps():
        base = rng.integers(100, 4000, (n_band, size, size)).astype(np.uint16)
        for t in range(n_time):
            yield (base + t * 7).astype(np.uint16)

    stream_kwargs: dict[str, Any] = {
        **_stream_kwargs(np.empty((n_time, n_band))),
        "bands": ["a", "b"],
    }
    tracemalloc.start()
    try:
        chronozarr.encode(steps(), tmp_path / "s", chunk_size=cs, workers=2, **stream_kwargs)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < full_bytes / 3, f"peak {peak / 1e6:.1f} MB vs raster {full_bytes / 1e6:.1f} MB"
    store = chronozarr.open_store(tmp_path / "s")
    assert store.levels[0].shape == (n_time, n_band, size, size)


def test_dataarray_encode_stays_bounded_beyond_its_input(tmp_path):
    n_time, n_band, size, cs = 24, 2, 1024, 128
    full_bytes = n_time * n_band * size * size * 2
    truth = (
        np.random.default_rng(2)
        .integers(100, 4000, (n_time, n_band, size, size))
        .astype(np.uint16)
    )
    da = xr.DataArray(
        truth,
        dims=("time", "band", "y", "x"),
        coords={"time": make_times(n_time), "band": ["a", "b"]},
        attrs={"crs": CRS, "transform": TRANSFORM},
    )
    tracemalloc.start()
    try:
        chronozarr.encode(da, tmp_path / "s", chunk_size=cs, workers=2)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < full_bytes / 3, f"peak {peak / 1e6:.1f} MB vs raster {full_bytes / 1e6:.1f} MB"
