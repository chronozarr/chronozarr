"""The bring-your-data bundle is self-contained and works when hosted under a subpath."""

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
# src="/x" or href="/x", but not protocol-relative "//host/x".
ROOT_ABSOLUTE = re.compile(r"""\b(?:src|href)=["'](/(?!/)[^"']*)["']""")
ENTRY_PAGES = ("index.html", "demo/index.html", "examples/embed.html")


def load_bundle_module():
    spec = importlib.util.spec_from_file_location(
        "bundle", REPO / "examples/bring_your_data/bundle.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def bundle(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("bring_your_data")
    store = root / "store"
    build_store(store, make_truth(2, 2, 64, 64), shard=False)
    output = root / "bundle"
    load_bundle_module().build_bundle(store, output)
    return output


def test_every_relative_import_in_the_bundle_resolves_inside_it(bundle):
    missing = []
    for script in bundle.rglob("*.js"):
        text = script.read_text(encoding="utf-8", errors="ignore")
        for pattern in IMPORTS:
            for target in pattern.findall(text):
                resolved = (script.parent / target).resolve()
                if not resolved.is_file() or not resolved.is_relative_to(bundle.resolve()):
                    missing.append(f"{script.relative_to(bundle)} imports {target}")
    assert not missing, "\n".join(missing)


def test_entry_pages_have_no_root_absolute_links(bundle):
    # A root-absolute link resolves outside the bundle when it is hosted at /<name>/.
    found = [
        f"{page} links {target}"
        for page in ENTRY_PAGES
        for target in ROOT_ABSOLUTE.findall((bundle / page).read_text())
    ]
    assert not found, "\n".join(found)
