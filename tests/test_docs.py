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
import pytest

from chronozarr.cli import main

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[1]
USER_DOCS = sorted(
    [REPO / "README.md", *(REPO / "docs").glob("*.md"), *(REPO / "examples").glob("*/README.md")]
)
SHELL = {"", "bash", "sh", "shell", "console"}
SITE_SOURCES = re.compile(r"^\s*'([^']+\.md)': '([^']+)',", re.M)


def _shell_blocks(text: str) -> list[str]:
    """The bodies of fenced code blocks tagged as shell, or untagged."""
    blocks: list[str] = []
    body: list[str] | None = None
    shell = False
    for line in text.splitlines():
        fence = re.match(r"^\s*```(\S*)", line)
        if fence and body is None:
            body, shell = [], fence.group(1) in SHELL
        elif fence:
            if shell:
                blocks.append("\n".join(body))
            body = None
        elif body is not None:
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
    version = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    npm = json.loads((REPO / "js/package.json").read_text(encoding="utf-8"))["version"]
    assert npm == version, "pyproject.toml and js/package.json must release the same version"
    pin = re.compile(r"chronozarr(?:\[[^\]]*\])?(?:==|@)(\d+\.\d+\.\d+)")
    # The evidence log and release notes record the versions that were measured or released.
    records = {"evidence.md", *(p.name for p in USER_DOCS if p.name.startswith("release-"))}
    for path in (p for p in USER_DOCS if p.name not in records):
        for found in pin.findall(path.read_text(encoding="utf-8")):
            assert found == version, (
                f"{path.name} pins chronozarr {found}; the release is {version}"
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
