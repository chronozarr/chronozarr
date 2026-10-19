"""NetCDF input in an environment without a NetCDF4 reader: the error names the netcdf extra.

The quickstart installs `chronozarr[geo]`, which has no NetCDF reader. Each case runs the command
line in a fresh interpreter in which the reader packages cannot be imported: xarray caches its
engine list and its import checks per process, so only a fresh one sees a newcomer's environment.
"""

from __future__ import annotations

import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

import pytest
import xarray as xr
from click.testing import CliRunner

from chronozarr.cli import main

pytestmark = pytest.mark.unit

HDF5_SIGNATURE = b"\x89HDF\r\n\x1a\n"
NETCDF3_SIGNATURE = b"CDF\x01"

_RUNNER = """
import sys

for name in sys.argv[1].split(","):
    sys.modules[name] = None  # `import name` raises ImportError
from chronozarr.cli import main

main(sys.argv[2:])
"""

# No reader at all: xarray lists netcdf4 and h5netcdf as matches whose dependencies are missing.
NO_READER = "netCDF4,h5netcdf,h5py,scipy"
# h5netcdf alone: xarray picks it, and it then fails to import h5py.
H5NETCDF_WITHOUT_H5PY = "netCDF4,h5py,scipy"


def _run(blocked: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", _RUNNER, blocked, *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _nc_file(tmp_path: Path, signature: bytes = HDF5_SIGNATURE) -> Path:
    path = tmp_path / "model.nc"
    path.write_bytes(signature + bytes(512))
    return path


def _assert_names_the_extra(result: subprocess.CompletedProcess[str], path: Path) -> None:
    pin = version("chronozarr")
    assert result.returncode == 1, result.stderr
    assert "Traceback" not in result.stderr
    assert str(path) in result.stderr
    assert "install the netcdf extra" in result.stderr
    assert f"uv tool install --python 3.13 'chronozarr[geo,netcdf]=={pin}'" in result.stderr
    assert f"uv add 'chronozarr[netcdf]=={pin}'" in result.stderr
    assert f"'chronozarr[netcdf]=={pin}'` with pip" in result.stderr
    assert "dependencies may not be installed" not in result.stderr


@pytest.mark.parametrize("blocked", [NO_READER, H5NETCDF_WITHOUT_H5PY], ids=["none", "no-h5py"])
def test_convert_names_the_netcdf_extra(tmp_path, blocked):
    if blocked == H5NETCDF_WITHOUT_H5PY:
        pytest.importorskip("h5netcdf")
    source = _nc_file(tmp_path)
    result = _run(
        blocked, "convert", str(source), str(tmp_path / "out"), "--variable", "tas", "--dry-run"
    )
    _assert_names_the_extra(result, source)


def test_encode_names_the_netcdf_extra(tmp_path):
    source = _nc_file(tmp_path)
    result = _run(NO_READER, "encode", str(source), str(tmp_path / "out"), "--variable", "tas")
    _assert_names_the_extra(result, source)


def test_append_names_the_netcdf_extra(tmp_path):
    source = _nc_file(tmp_path)
    store = tmp_path / "store"
    store.mkdir()
    result = _run(NO_READER, "append", str(store), str(source), "--variable", "tas")
    _assert_names_the_extra(result, source)


def test_a_file_that_is_not_netcdf_keeps_the_error_of_xarray(tmp_path):
    source = tmp_path / "page.nc"
    source.write_text("<html>Service unavailable</html>")
    result = _run(NO_READER, "convert", str(source), str(tmp_path / "out"), "--dry-run")
    assert result.returncode == 1
    assert "IO backends" in result.stderr
    assert "netcdf]" not in result.stderr


def test_a_netcdf3_file_keeps_the_error_of_xarray_that_names_scipy(tmp_path):
    """The extra reads NetCDF4 only; saying "install the netcdf extra" here would not help."""
    source = _nc_file(tmp_path, NETCDF3_SIGNATURE)
    result = _run(NO_READER, "convert", str(source), str(tmp_path / "out"), "--dry-run")
    assert result.returncode == 1
    assert "scipy" in result.stderr
    assert "netcdf]" not in result.stderr


def test_an_error_with_a_reader_installed_is_not_blamed_on_the_extra(tmp_path, monkeypatch):
    """A 360-day calendar without cftime makes xarray raise ValueError for a file that opens."""
    pytest.importorskip("h5netcdf")
    monkeypatch.setitem(sys.modules, "cftime", None)
    source = tmp_path / "model.nc"
    time = ("time", [0, 1], {"units": "days since 2000-01-01", "calendar": "360_day"})
    xr.Dataset({"tas": (("time",), [1.0, 2.0])}, coords={"time": time}).to_netcdf(
        source, engine="h5netcdf"
    )
    result = CliRunner().invoke(
        main,
        ["convert", str(source), str(tmp_path / "out"), "--variable", "tas", "--dry-run"],
        catch_exceptions=False,
    )
    assert result.exit_code == 1
    assert "unable to decode time units" in result.output
    assert "netcdf]" not in result.output
