"""The install commands in messages about a missing extra."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError

import pytest

from chronozarr import _install

pytestmark = pytest.mark.unit

REPOSITORY = "git+https://github.com/chronozarr/chronozarr"


def _installed(monkeypatch: pytest.MonkeyPatch, installed: str) -> None:
    monkeypatch.setattr(_install, "version", lambda name: installed)


@pytest.mark.parametrize("released", ["0.4.1", "0.5.0", "0.5.0rc1", "0.4.1.post1"])
def test_a_released_version_is_pinned(monkeypatch, released):
    _installed(monkeypatch, released)
    hint = _install.extra_hint("s3")
    assert f"uv tool install --python 3.13 'chronozarr[geo,s3]=={released}'" in hint
    assert f"uv add 'chronozarr[s3]=={released}'" in hint
    assert f"'chronozarr[s3]=={released}'` with pip" in hint
    assert "uv sync --extra s3" in hint
    assert "development version" not in hint


def test_the_hint_has_no_pin_when_chronozarr_is_not_installed(monkeypatch):
    def not_installed(name: str) -> str:
        raise PackageNotFoundError(name)

    monkeypatch.setattr(_install, "version", not_installed)
    hint = _install.extra_hint("s3")
    assert "uv tool install --python 3.13 'chronozarr[geo,s3]'" in hint
    assert "==" not in hint
    assert "development version" not in hint


@pytest.mark.parametrize(
    "development",
    ["0.5.0.dev0", "0.5.0.dev12", "0.5.0+g1a2b3c4", "0.5.0.dev0+g1a2b3c4.d20261019"],
)
def test_a_development_version_is_not_pinned_and_installs_from_the_repository(
    monkeypatch, development
):
    """No release carries a development version, so `==0.5.0.dev0` would fail to resolve, and the
    latest release can lack the extra and the fixes of the code that is running."""
    _installed(monkeypatch, development)
    hint = _install.extra_hint("cftime")
    assert "==" not in hint
    assert f"chronozarr {development} is a development version" in hint
    assert f"uv tool install --python 3.13 'chronozarr[geo,cftime] @ {REPOSITORY}'" in hint
    assert f"uv add 'chronozarr[cftime] @ {REPOSITORY}'" in hint
    assert f"pip install 'chronozarr[cftime] @ {REPOSITORY}'" in hint
    assert "uv sync --extra cftime" in hint


def test_the_geo_extra_is_not_listed_twice_in_a_development_hint(monkeypatch):
    _installed(monkeypatch, "0.5.0.dev0")
    hint = _install.extra_hint("geo")
    assert f"uv tool install --python 3.13 'chronozarr[geo] @ {REPOSITORY}'" in hint
