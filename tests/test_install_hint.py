"""The install commands in messages about a missing extra."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

import pytest

from chronozarr import _install

pytestmark = pytest.mark.unit


def test_the_hint_pins_the_installed_version():
    pin = version("chronozarr")
    hint = _install.extra_hint("s3")
    assert f"uv tool install --python 3.13 'chronozarr[geo,s3]=={pin}'" in hint
    assert f"uv add 'chronozarr[s3]=={pin}'" in hint
    assert f"'chronozarr[s3]=={pin}'` with pip" in hint


def test_the_hint_has_no_pin_when_chronozarr_is_not_installed(monkeypatch):
    def not_installed(name: str) -> str:
        raise PackageNotFoundError(name)

    monkeypatch.setattr(_install, "version", not_installed)
    hint = _install.extra_hint("s3")
    assert "uv tool install --python 3.13 'chronozarr[geo,s3]'" in hint
    assert "==" not in hint
