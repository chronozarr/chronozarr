"""The user docs against the code: commands and options that exist, install pins that match the
release, the README quickstart against the full one, and CLI messages that name published pages."""

from __future__ import annotations

import ast
import json
import re
import shlex
import tomllib
from pathlib import Path

import click
import numpy as np
import pytest
import xarray as xr
from packaging.version import Version

import chronozarr
from chronozarr.cli import main
from chronozarr.schema import Band
from tests.synthetic import make_times

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[1]
USER_DOCS = sorted(
    [REPO / "README.md", *(REPO / "docs").glob("*.md"), *(REPO / "examples").glob("*/README.md")]
)
SHELL = {"", "bash", "sh", "shell", "console"}
SITE_SOURCES = re.compile(r"^\s*'([^']+\.md)': \{ route: '([^']+)'", re.M)


def _shell_blocks(text: str) -> list[str]:
    """The bodies of fenced code blocks tagged as shell, or untagged."""
    blocks: list[str] = []
    body: list[str] = []
    inside = shell = False
    for line in text.splitlines():
        fence = re.match(r"^\s*```(\S*)", line)
        if fence and not inside:
            body, inside, shell = [], True, fence.group(1) in SHELL
        elif fence:
            if shell:
                blocks.append("\n".join(body))
            inside = False
        elif inside:
            body.append(line)
    return blocks


def _commands(text: str) -> list[str]:
    """Logical lines of shell code blocks: continuations joined, comments dropped."""
    lines: list[str] = []
    for block in _shell_blocks(text):
        for line in block.replace("\\\n", " ").splitlines():
            line = line.split(" #", 1)[0].strip()
            if line and not line.startswith("#"):
                lines.append(line)
    return lines


def _invocations(path: Path) -> list[tuple[str, list[str]]]:
    """Each `chronozarr SUBCOMMAND ...` in the shell code of a document, as (subcommand, words)."""
    found = []
    for line in _commands(path.read_text(encoding="utf-8")):
        for part in re.split(r"\s*(?:&&|\|\||;|\|)\s*", line):
            try:
                words = shlex.split(part)
            except ValueError:
                continue
            for i, word in enumerate(words[:-1]):
                if word == "chronozarr" and words[i + 1] in main.commands:
                    found.append((words[i + 1], words[i + 2 :]))
                    break
    return found


def _options(command: click.Command) -> set[str]:
    names = {"--help"}
    for param in command.params:
        names.update(name for name in [*param.opts, *param.secondary_opts] if name.startswith("-"))
    return names


@pytest.mark.parametrize("path", USER_DOCS, ids=lambda p: str(p.relative_to(REPO)))
def test_documented_commands_and_options_exist(path: Path) -> None:
    for name, words in _invocations(path):
        known = _options(main.commands[name])
        flags = [w.split("=", 1)[0] for w in words if w.startswith("--")]
        unknown = sorted(set(flags) - known)
        assert not unknown, f"{path.name}: `chronozarr {name}` has no option {unknown}"


def test_the_docs_show_the_commands_people_need() -> None:
    documented = {name for path in USER_DOCS for name, _ in _invocations(path)}
    for name in ("convert", "preview", "share", "publish", "doctor", "link", "validate", "info"):
        assert name in documented, f"no user doc shows `chronozarr {name}`"


def test_install_pins_match_the_release() -> None:
    """The docs pin the release that PyPI has. A development version on main is the next one."""
    version = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    npm = json.loads((REPO / "js/package.json").read_text(encoding="utf-8"))["version"]
    # PEP 440 writes a development release 0.5.0.dev0; npm's semver writes it 0.5.0-dev.0.
    assert npm == re.sub(r"\.dev(\d+)$", r"-dev.\1", version), (
        f"pyproject.toml ({version}) and js/package.json ({npm}) must be the same version"
    )
    # Install pins, and the `chronozarr --version` output that the quickstart shows.
    pin = re.compile(r"chronozarr(?:\[[^\]]*\])?(?:==|@|, version )(\d+\.\d+\.\d+)")
    # The evidence log and release notes record the versions that were measured or released.
    records = {"evidence.md", *(p.name for p in USER_DOCS if p.name.startswith("release-"))}
    pinned: dict[str, list[str]] = {}
    for path in (p for p in USER_DOCS if p.name not in records):
        for found in pin.findall(path.read_text(encoding="utf-8")):
            pinned.setdefault(found, []).append(path.name)
    assert pinned, "no user doc pins a chronozarr version"
    assert len(pinned) == 1, f"the docs pin more than one release: {pinned}"
    release = Version(next(iter(pinned)))
    if Version(version).is_devrelease:
        assert Version(version) > release, (
            f"pyproject.toml is {version}, not after the release {release} that the docs pin"
        )
    else:
        assert Version(version) == release, (
            f"the docs pin chronozarr {release}; the release is {version}: {pinned}"
        )


def test_the_readme_quickstart_is_part_of_the_full_quickstart() -> None:
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Quickstart", 1)[1].split("\n## ", 1)[0]
    full = (REPO / "docs/quickstart.md").read_text(encoding="utf-8")
    commands = _commands(section)
    assert commands, "the README quickstart has no commands"
    for command in commands:
        assert command in full, f"README quickstart command not in docs/quickstart.md: {command}"


def _messages(path: Path) -> list[tuple[int, str]]:
    """String constants of a module that are not docstrings, with their line numbers."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def test_cli_messages_point_at_published_pages() -> None:
    """An installed package has no docs/ or deploy/ folder: messages name chronozarr.org pages."""
    sync = (REPO / "site/scripts/sync-content.mjs").read_text(encoding="utf-8")
    routes = {f"/{route}" for _, route in SITE_SOURCES.findall(sync)}
    redirects = json.loads((REPO / "site/redirects.json").read_text(encoding="utf-8"))
    repo_path = re.compile(r"(?<![\w/])(?:docs|deploy|spec)/[\w.-]+\.(?:md|json)\b")
    site_url = re.compile(r"https://chronozarr\.org(/[\w/.-]*)")
    for path in sorted((REPO / "src/chronozarr").glob("*.py")):
        for line, text in _messages(path):
            where = f"{path.name}:{line}"
            assert not repo_path.search(text), f"{where} names a repository file: {text!r}"
            for page in site_url.findall(text):
                page = page.split("#")[0].rstrip("./")
                if page.startswith(("/demo", "/maplibre")) or not page:
                    continue
                assert page in routes or page in redirects, f"{where}: no page {page}"


def test_every_site_source_exists() -> None:
    sync = (REPO / "site/scripts/sync-content.mjs").read_text(encoding="utf-8")
    sources = SITE_SOURCES.findall(sync)
    assert sources
    for source, _ in sources:
        assert (REPO / source).is_file(), f"site/scripts/sync-content.mjs lists missing {source}"


def test_the_agent_skill_cites_pages_and_options_that_exist() -> None:
    """SKILL.md names Markdown routes and CLI options in prose and tables, not only in code."""
    skill = (REPO / "skills/chronozarr/SKILL.md").read_text(encoding="utf-8")
    sync = (REPO / "site/scripts/sync-content.mjs").read_text(encoding="utf-8")
    routes = {route for _, route in SITE_SOURCES.findall(sync)}
    for route in re.findall(r"`/([\w/-]+)\.md(?:#[\w-]+)?`", skill):
        assert route in routes, f"SKILL.md cites /{route}.md; the site publishes no such page"
    for span in re.findall(r"`(chronozarr [^`]+)`", skill):
        words = span.split()
        if len(words) < 2 or words[1] not in main.commands:
            continue
        known = _options(main.commands[words[1]])
        unknown = sorted(set(re.findall(r"(?<![\w-])--[\w-]+", span)) - known)
        assert not unknown, f"SKILL.md: `chronozarr {words[1]}` has no option {unknown}"


def _python_blocks(text: str) -> list[str]:
    """The bodies of fenced code blocks tagged `python`."""
    blocks: list[str] = []
    body: list[str] = []
    inside = python = False
    for line in text.splitlines():
        fence = re.match(r"^\s*```(\S*)", line)
        if fence and not inside:
            body, inside, python = [], True, fence.group(1) == "python"
        elif fence:
            if python:
                blocks.append("\n".join(body))
            inside = False
        elif inside:
            body.append(line)
    return blocks


def _guide_store(path: Path, *, units: str | None) -> np.ndarray:
    """The store that docs/analyze.md and docs/quickstart.md describe: 12 months, four bands,
    692 x 690 pixels at 40 m, two pyramid levels."""
    rng = np.random.default_rng(3)
    rows, cols = np.mgrid[0:692, 0:690]
    truth = (1000 + 4 * rows + 3 * cols)[None, None] + rng.integers(0, 50, size=(12, 4, 692, 690))
    truth = truth.astype(np.uint16)
    names = ["B02", "B03", "B04", "B08"]
    array = xr.DataArray(
        truth,
        dims=("time", "band", "y", "x"),
        coords={"time": make_times(12), "band": names},
        attrs={"crs": "EPSG:32718", "transform": (40.0, 0.0, 485650.0, 0.0, -40.0, 9169880.0)},
    )
    bands = [Band(name, scale=0.0001, offset=0.0, units=units) for name in names]
    chronozarr.encode(array, path, bands=bands)
    return truth


def _run_python_blocks(doc: str, tmp_path: Path, monkeypatch, *, units: str | None):
    """Run the Python blocks of `docs/<doc>` in order, in a folder that holds `my_store`."""
    truth = _guide_store(tmp_path / "my_store", units=units)
    monkeypatch.chdir(tmp_path)
    blocks = _python_blocks((REPO / "docs" / doc).read_text(encoding="utf-8"))
    assert blocks, f"docs/{doc} has no Python examples"
    scope: dict[str, object] = {}
    for block in blocks:
        exec(compile(block, f"docs/{doc}", "exec"), scope)
    return scope, truth


@pytest.mark.parametrize("units", [None, "reflectance"], ids=["no-units", "units"])
def test_the_analyze_guide_python_examples_run(tmp_path, monkeypatch, units) -> None:
    """Each Python block of docs/analyze.md, against a store with and without units."""
    scope, truth = _run_python_blocks("analyze.md", tmp_path, monkeypatch, units=units)

    # The guide says `band_units` exists only when the source declares units.
    opened = xr.open_dataset("my_store", engine="chronozarr")
    assert ("band_units" in opened.coords) == (units is not None)
    if units is None:
        with pytest.raises(AttributeError):
            _ = opened.band_units
    series = scope["series"]
    # A single pixel, selected at the grid centre nearest to the point the guide names.
    assert series.dims == ("time",) and series.sizes["time"] == 12
    assert series.band.item() == "B08"
    assert abs(series.x.item() - 496000) <= 20 and abs(series.y.item() - 9160000) <= 20
    col = round((series.x.item() - 485670) / 40)
    row = round((9169860 - series.y.item()) / 40)
    np.testing.assert_allclose(series.values, truth[:, 3, row, col] * 0.0001, rtol=1e-5)
    assert scope["ndvi"].sizes["time"] == 12
    assert scope["overview"].attrs["lod"] == 1


def test_the_quickstart_python_example_runs(tmp_path, monkeypatch) -> None:
    scope, truth = _run_python_blocks("quickstart.md", tmp_path, monkeypatch, units="reflectance")
    assert scope["ds"].sizes["time"] == 12
    np.testing.assert_allclose(
        scope["ds"]["data"].sel(band="B08").isel(time=0, x=100, y=80).item(),
        truth[0, 3, 80, 100] * 0.0001,
        rtol=1e-5,
    )
