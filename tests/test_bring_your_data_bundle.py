"""The bring-your-data bundle is self-contained: every module its viewer imports is in it."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from tests.synthetic import build_store, make_truth

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[1]
# Static and dynamic imports, plus module-relative URLs (the decode worker, zarrita codecs).
# The dynamic forms fail only at runtime: demo/embed.js reaches maplibre/ through import()
# and swallows the error.
IMPORTS = (
    re.compile(r"""\b(?:from|import)\s*\(?\s*['"](\.{1,2}/[^'"]+)['"]"""),
    re.compile(r"""\bnew URL\(\s*['"](\.{1,2}/[^'"]+)['"]\s*,\s*import\.meta\.url"""),
)


def load_bundle_module():
    spec = importlib.util.spec_from_file_location(
        "bundle", REPO / "examples/bring_your_data/bundle.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_relative_import_in_the_bundle_resolves_inside_it(tmp_path):
    store = tmp_path / "store"
    build_store(store, make_truth(2, 2, 64, 64), shard=False)
    output = tmp_path / "bundle"
    load_bundle_module().build_bundle(store, output)
    missing = []
    for script in output.rglob("*.js"):
        text = script.read_text(encoding="utf-8", errors="ignore")
        for pattern in IMPORTS:
            for target in pattern.findall(text):
                resolved = (script.parent / target).resolve()
                if not resolved.is_file() or not resolved.is_relative_to(output.resolve()):
                    missing.append(f"{script.relative_to(output)} imports {target}")
    assert not missing, "\n".join(missing)
