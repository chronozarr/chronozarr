"""Distinguish absent local stores from existing non-stores without creating files."""

from pathlib import Path

import pytest
import xarray as xr
from zarr.storage import LocalStore

import chronozarr
from chronozarr.backend import ChronozarrBackendEntrypoint

pytestmark = pytest.mark.unit


def _problem(api, source):
    if api == "validate":
        problems = chronozarr.validate(source)
        assert len(problems) == 1
        return problems[0]
    with pytest.raises(chronozarr.SchemaError) as caught:
        if api == "xarray":
            xr.open_dataset(source, engine=ChronozarrBackendEntrypoint)
        else:
            chronozarr.open_store(source)
    return str(caught.value)


@pytest.mark.parametrize("api", ["open_store", "xarray", "validate"])
@pytest.mark.parametrize("source_kind", ["string", "path", "absolute", "local_store"])
def test_missing_local_store_identifies_the_path_and_where_it_was_looked_for(
    tmp_path, monkeypatch, api, source_kind
):
    working = tmp_path / "analysis"
    working.mkdir()
    monkeypatch.chdir(working)
    missing = Path("my_store")
    source = {
        "string": str(missing),
        "path": missing,
        "absolute": working / missing,
        "local_store": LocalStore(missing, read_only=True),
    }[source_kind]
    message = _problem(api, source)
    assert "my_store" in message
    assert "does not exist" in message
    assert str(working) in message
    assert "path" in message
    assert not missing.exists(), "Opening a missing store must not create its directory"
    assert list(working.iterdir()) == []


@pytest.mark.parametrize("api", ["open_store", "xarray", "validate"])
def test_existing_non_store_is_not_misreported_as_a_missing_path(tmp_path, api):
    empty = tmp_path / "empty"
    empty.mkdir()
    message = _problem(api, empty)
    assert "no Zarr v3 group found" in message
    assert "does not exist" not in message
    assert list(empty.iterdir()) == []
