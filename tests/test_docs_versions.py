"""Local candidate guides select local inputs; stable installation instructions stay released."""

from __future__ import annotations

import json
import re
import shlex
import tomllib

import pytest
from scripts.trial.quickstart_commands import build_plan

from tests.test_docs import REPO, SITE_SOURCES, USER_DOCS, _commands

pytestmark = pytest.mark.unit

DEVELOPMENT_GUIDES = (
    "convert.md",
    "python.md",
    "cli.md",
    "viewer.md",
    "troubleshooting.md",
    "viewer-distribution.md",
)


@pytest.mark.parametrize("guide", DEVELOPMENT_GUIDES)
def test_development_guides_select_a_source_install_and_link_the_stable_version(guide):
    version = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"]
    text = (REPO / "docs" / guide).read_text()
    introduction = text.split("\n## ", 1)[0]
    assert f"Local release candidate — {version}, unpublished" in introduction
    assert re.search(r"\]\(development\.md(?:#[^)]+)?\)", introduction)
    assert f"https://github.com/chronozarr/chronozarr/blob/v0.4.1/docs/{guide}" in introduction


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


def test_development_guides_cannot_silently_install_the_registry_release():
    for guide in (*DEVELOPMENT_GUIDES, "development.md"):
        for command in _commands((REPO / "docs" / guide).read_text()):
            words = shlex.split(command)
            installing = words[:3] == ["uv", "tool", "install"] or words[:2] == ["uv", "add"]
            installing |= words[:2] == ["npm", "install"]
            installing |= any(words[i : i + 2] == ["pip", "install"] for i in range(len(words)))
            if installing:
                assert not any(
                    re.fullmatch(r"chronozarr(?:\[[^\]]+\])?(?:(?:==|@)[^ ]+)?", word)
                    for word in words
                ), f"{guide} installs the registry package: {command}"


def test_candidate_artifact_instructions_install_exact_local_versions():
    version = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"]
    npm = json.loads((REPO / "js/package.json").read_text())["version"]
    text = (REPO / "docs/development.md").read_text()
    commands = [shlex.split(command) for command in _commands(text)]
    wheel = f"/absolute/path/to/rc-artifacts/chronozarr-{version}-py3-none-any.whl"
    selector = f"{wheel}[geo,netcdf,cftime]"
    assert ["uv", "tool", "install", "--python", "3.13", "--force", selector] in commands
    assert ["uv", "add", selector] in commands
    tarball = f"/absolute/path/to/rc-artifacts/chronozarr-{npm}.tgz"
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
    assert "has not been published, tagged or deployed" in text
    assert f"Python version is `{version}`" in text
    assert f"npm version is `{npm}`" in text


def test_candidate_notes_record_the_scope_and_all_sixteen_repairs():
    version = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"]
    npm = json.loads((REPO / "js/package.json").read_text())["version"]
    text = (REPO / f"docs/release-{version}.md").read_text()
    assert "**Local candidate only, unpublished.**" in text
    assert f"Python `{version}` and npm `{npm}`" in text
    assert "04395f4" in text
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


def test_user_docs_never_install_the_absent_cftime_extra_from_041():
    absent_extra = re.compile(r"chronozarr\[[^\]]*\bcftime\b[^\]]*\]==0\.4\.1")
    for path in USER_DOCS:
        assert not absent_extra.search(path.read_text()), f"{path.name}: 0.4.1 has no cftime extra"


def test_stable_quickstart_still_builds_the_released_clean_room_plan():
    text = (REPO / "docs/quickstart.md").read_text()
    plan = build_plan(text, "docs/quickstart.md")
    assert plan["chronozarr_version"] == "0.4.1"
    assert "Released package — 0.4.1" in text
    assert "https://github.com/chronozarr/chronozarr/tree/v0.4.1/docs" in text
    unavailable = {
        "--bands-are-dates",
        "--calendar-to-gregorian",
        "--colormap",
        "--lonlat",
        "--mosaic-tiles",
        "--offset",
        "--scale",
        "--xy",
        "--zoom",
    }
    for step in plan["steps"]:
        flags = {
            word.split("=", 1)[0] for word in shlex.split(step["text"]) if word.startswith("--")
        }
        assert not flags & unavailable
    assert len(plan["steps"]) == 6
    assert [step["long_running"] for step in plan["steps"]] == [False] * 4 + [True] * 2


def test_development_viewer_instructions_build_this_checkout_with_the_quarantine():
    text = (REPO / "docs/viewer-distribution.md").read_text()
    commands = [shlex.split(command) for command in _commands(text)]
    assert ["npm", "ci", "--prefix", "js", "--min-release-age=7"] in commands
    copies = [words for words in commands if words[:2] == ["node", "js/support/viewer-bundle.mjs"]]
    assert copies and all("--store" in words for words in copies)
    assert (REPO / copies[0][1]).is_file()
    assert not any(words[:2] == ["npm", "install"] for words in commands)
    viewer = (REPO / "docs/viewer.md").read_text()
    assert "Its deployed build can differ from this checkout" in viewer


def test_the_shared_development_route_is_published_and_in_navigation():
    sources = SITE_SOURCES.findall((REPO / "site/scripts/sync-content.mjs").read_text())
    assert ("docs/development.md", "guides/development") in sources
    version = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"]
    assert (f"docs/release-{version}.md", f"reference/release-{version}") in sources
    navigation = (REPO / "site/vocs.config.tsx").read_text()
    assert "link: '/guides/development'" in navigation
    assert f"link: '/reference/release-{version}'" in navigation
