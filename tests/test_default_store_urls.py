"""Published store URLs shipped in src/ and js/ must use the data host and a v0.3 prefix."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parent.parent
DATA_HOST = "data.chronozarr.org"
V03_PREFIXES = ("/ucayali_santa_maria_v03", "/ucayali_santa_maria/png-v03")
SCANNED_SUFFIXES = {".py", ".js", ".mjs", ".html", ".json", ".md"}
SKIPPED_DIRS = {"node_modules", "vendor", "test", "e2e", "support", "__pycache__"}
URL_PATTERN = re.compile(r"https?://[^\s'\"`)<>\\,]+")
# Files that must each contribute at least one store URL, so the scan cannot go vacuous.
REQUIRED_SOURCES = (
    "src/chronozarr/leafmap.py",
    "js/demo/catalog.json",
    "js/maplibre/demo.js",
    "js/examples/embed.html",
)


def _is_store_url(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    return host.startswith("data.") and not host.endswith(("example.org", "example.com"))


def _store_urls() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for top in ("src", "js"):
        for path in sorted((ROOT / top).rglob("*")):
            relative = path.relative_to(ROOT)
            if not path.is_file() or path.suffix not in SCANNED_SUFFIXES:
                continue
            if SKIPPED_DIRS.intersection(relative.parts):
                continue
            text = path.read_text(encoding="utf-8")
            for url in URL_PATTERN.findall(text):
                if _is_store_url(url):
                    found.append((relative.as_posix(), url.rstrip("/.;")))
    return found


@pytest.mark.unit
def test_shipped_store_urls_use_data_host_and_v03_prefix() -> None:
    found = _store_urls()
    sources = {source for source, _ in found}
    missing = [name for name in REQUIRED_SOURCES if name not in sources]
    assert not missing, f"no store URL found in {missing}; update REQUIRED_SOURCES or the scan"

    stale = []
    for source, url in found:
        parts = urlsplit(url)
        if parts.hostname != DATA_HOST or not parts.path.startswith(V03_PREFIXES):
            stale.append(f"{source}: {url}")
    assert not stale, (
        f"store URLs must use {DATA_HOST} and a v0.3 prefix {V03_PREFIXES}:\n" + "\n".join(stale)
    )
