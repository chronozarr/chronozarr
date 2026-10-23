"""Release guides, install commands and local artifacts agree on the current version."""

from __future__ import annotations

import json
import re
import shlex
import tomllib

import pytest
from scripts.trial.quickstart_commands import build_plan

from tests.test_docs import REPO, SITE_SOURCES, USER_DOCS, _commands

pytestmark = pytest.mark.unit

RELEASE_GUIDES = (
    "convert.md",
    "python.md",
    "cli.md",
    "viewer.md",
    "troubleshooting.md",
    "viewer-distribution.md",
)


@pytest.mark.parametrize("guide", RELEASE_GUIDES)
def test_release_guides_select_the_matching_installation_and_release_notes(guide):
    version = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"]
    text = (REPO / "docs" / guide).read_text()
    introduction = text.split("\n## ", 1)[0]
    assert f"Package version — {version}." in introduction
    assert "Local release candidate" not in introduction
    assert re.search(
        r"\]\((?:quickstart|viewer-distribution|development)\.md(?:#[^)]+)?\)", introduction
    )
    assert f"release-{version}.md" in introduction or guide in (
        "viewer.md",
        "viewer-distribution.md",
    )


def test_shared_development_install_uses_local_source_for_the_tool_and_python_project():
    text = (REPO / "docs/development.md").read_text()
    commands = [shlex.split(command) for command in _commands(text)]
    (tool,) = (
        words
        for words in commands
        if words[:3] == ["uv", "tool", "install"] and words[-1].startswith(".[")
    )
    assert tool[tool.index("--python") + 1] == "3.13"
    assert "--force" in tool
    (project,) = (
        words for words in commands if words[:2] == ["uv", "add"] and "--editable" in words
    )
    assert "--editable" in project
    extras = tomllib.loads((REPO / "pyproject.toml").read_text())["project"][
        "optional-dependencies"
    ]
    for selector, expected_path in (
        (tool[-1], "."),
        (project[-1], "/absolute/path/to/chronozarr"),
    ):
        matched = re.fullmatch(r"([^\[]+)\[([^\]]+)\]", selector)
        assert matched is not None, f"a source path with extras is required: {selector}"
        assert matched[1] == expected_path
        requested = set(matched[2].split(","))
        assert {"geo", "netcdf", "cftime"} <= requested <= set(extras)
    assert ["uv", "run", "chronozarr", "--version"] in commands
    assert ["uv", "run", "python", "analyze.py"] in commands
    assert ["cd", "/absolute/path/to/chronozarr"] in commands
    assert not any(words[:2] == ["git", "clone"] for words in commands)


def test_local_source_installation_uses_local_inputs():
    for command in _commands((REPO / "docs/development.md").read_text()):
        words = shlex.split(command)
        installing = words[:3] == ["uv", "tool", "install"] or words[:2] == ["uv", "add"]
        installing |= words[:2] == ["npm", "install"]
        installing |= any(words[i : i + 2] == ["pip", "install"] for i in range(len(words)))
        if installing:
            assert not any(
                re.fullmatch(r"chronozarr(?:\[[^\]]+\])?(?:(?:==|@)[^ ]+)?", word)
                for word in words
            ), f"development.md installs the registry package: {command}"


def test_artifact_instructions_install_exact_local_versions():
    version = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"]
    npm = json.loads((REPO / "js/package.json").read_text())["version"]
    text = (REPO / "docs/development.md").read_text()
    commands = [shlex.split(command) for command in _commands(text)]
    wheel = f"/absolute/path/to/release-artifacts/chronozarr-{version}-py3-none-any.whl"
    selector = f"{wheel}[geo,netcdf,cftime]"
    assert ["uv", "tool", "install", "--python", "3.13", "--force", selector] in commands
    assert ["uv", "add", selector] in commands
    tarball = f"/absolute/path/to/release-artifacts/chronozarr-{npm}.tgz"
    assert ["npm", "install", tarball, "--min-release-age=7"] in commands
    assert ["npm", "ls", "chronozarr"] in commands
    assert [
        "npm",
        "exec",
        "--",
        "chronozarr-viewer",
        "published",
        "--store",
        "./store",
    ] in commands
    assert f"chronozarr-{version}.tar.gz" in text
    assert "Installing a local artifact does not publish it" in text
    assert f"Python and npm **{version}**" in text
    assert f"`npm ls` must show version `{npm}`" in text


def test_release_notes_record_the_scope_and_all_sixteen_repairs():
    version = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"]
    npm = json.loads((REPO / "js/package.json").read_text())["version"]
    text = (REPO / f"docs/release-{version}.md").read_text()
    assert version == npm
    assert f"Python and npm **{version}**" in text
    assert "63610f8" in text
    assert set(re.findall(r"^\| (\d+) \|", text, re.MULTILINE)) == {str(n) for n in range(1, 17)}
    for feature in (
        "--bands-are-dates",
        "--mosaic-tiles",
        "--calendar-to-gregorian",
        "--scale",
        "--offset",
    ):
        assert feature in text
    assert "EPSG CRS, spec `0.3.0`" in text
    assert "Other CRS, spec `0.4.0`" in text
    assert "does not update the hosted demo" in text
    assert all(name in text for name in ("release-checks.md", "manifest.json", "SHA256SUMS"))


def test_candidate_record_keeps_its_unpublished_scope_and_frozen_installation():
    text = (REPO / "docs/release-0.5.0rc1.md").read_text()
    assert "**Local candidate only, unpublished.**" in text
    assert "Python `0.5.0rc1` and npm `0.5.0-rc.1`" in text
    assert "ef3dbfcbb514971603c1cfd3ae06cd894a68f489/docs/development.md" in text


def test_user_docs_never_install_the_absent_cftime_extra_from_041():
    absent_extra = re.compile(r"chronozarr\[[^\]]*\bcftime\b[^\]]*\]==0\.4\.1")
    for path in USER_DOCS:
        assert not absent_extra.search(path.read_text()), f"{path.name}: 0.4.1 has no cftime extra"


def test_quickstart_builds_the_matching_clean_room_plan():
    text = (REPO / "docs/quickstart.md").read_text()
    plan = build_plan(text, "docs/quickstart.md")
    version = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"]
    assert plan["chronozarr_version"] == version
    assert f"Package version — {version}." in text
    assert len(plan["steps"]) == 6
    assert [step["long_running"] for step in plan["steps"]] == [False] * 4 + [True] * 2


def test_viewer_instructions_copy_the_package_and_preserve_the_source_build_route():
    text = (REPO / "docs/viewer-distribution.md").read_text()
    commands = [shlex.split(command) for command in _commands(text)]
    assert ["npm", "ci", "--prefix", "js", "--min-release-age=7"] in commands
    copies = [words for words in commands if words[:2] == ["node", "js/support/viewer-bundle.mjs"]]
    assert copies and all("--store" in words for words in copies)
    assert (REPO / copies[0][1]).is_file()
    version = json.loads((REPO / "js/package.json").read_text())["version"]
    assert ["npm", "install", f"chronozarr@{version}"] in commands
    assert [
        "npm",
        "exec",
        "--",
        "chronozarr-viewer",
        "published",
        "--store",
        "./store",
    ] in commands
    viewer = (REPO / "docs/viewer.md").read_text()
    assert "Installing the Python package does not update the hosted demo" in viewer


def test_the_shared_development_route_is_published_and_in_navigation():
    sources = SITE_SOURCES.findall((REPO / "site/scripts/sync-content.mjs").read_text())
    assert ("docs/development.md", "guides/development") in sources
    version = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"]
    assert (f"docs/release-{version}.md", f"reference/release-{version}") in sources
    navigation = (REPO / "site/vocs.config.tsx").read_text()
    assert "link: '/guides/development'" in navigation
    assert f"link: '/reference/release-{version}'" in navigation
