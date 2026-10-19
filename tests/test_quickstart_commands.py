"""scripts/trial/quickstart_commands.py: the quickstart page becomes the clean-room job's steps."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[1]


def load_parser():
    path = REPO / "scripts" / "trial" / "quickstart_commands.py"
    spec = importlib.util.spec_from_file_location("quickstart_commands", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve the module's annotations here
    spec.loader.exec_module(module)
    return module


qc = load_parser()

INSTALL = "uv tool install --python 3.13 'chronozarr[geo]==1.2.3'\nchronozarr --version"
EXPORT = "chronozarr export-cog https://example.org/demo sample --level 2"
DRY_RUN = "chronozarr convert sample my_store --dry-run"
CONVERT = "chronozarr convert sample my_store"
SHARE = "chronozarr share my_store"
PREVIEW = "chronozarr preview my_store"


def bash(text: str) -> str:
    return f"```bash\n{text}\n```"


def page(
    install: str = INSTALL,
    export: str = EXPORT,
    dry_run: str = DRY_RUN,
    convert: str = CONVERT,
    share: str = SHARE,
    preview: str = PREVIEW,
    extra: str = "",
) -> str:
    """A minimal page with the structure the job expects; each command can be replaced."""
    return f"""# Quickstart

## 1. Install

{bash(install)}

The first command puts the command on your `PATH`.

{extra}
## 2. Get a dated raster series

If you have no files at hand, export some:

{bash(export)}

## 3. Convert

Check the files first:

{bash(dry_run)}

```text
dry run: nothing was written
```

Then convert:

{bash(convert)}

## 4. Share

{bash(share)}

## 5. Preview

Use it when you have no `cloudflared`, when the data must stay here, or to check a store first.

{bash(preview)}
"""


PIP_ROUTE = f"""To install with pip instead, use an environment with Python 3.11 or newer:

{bash("python -m pip install 'chronozarr[geo]==1.2.3'")}
"""


def labels(plan: dict) -> list[str]:
    return [step["text"].splitlines()[0] for step in plan["steps"]]


def test_real_page_gives_the_documented_steps_in_order():
    source = REPO / "docs" / "quickstart.md"
    plan = qc.build_plan(source.read_text(encoding="utf-8"), str(source))

    commands = [step["text"].splitlines()[0].split()[:2] for step in plan["steps"]]
    assert commands == [
        ["uv", "tool"],
        ["chronozarr", "export-cog"],
        ["chronozarr", "convert"],
        ["chronozarr", "convert"],
        ["chronozarr", "share"],
        ["chronozarr", "preview"],
    ]
    assert plan["steps"][0]["text"].splitlines()[1] == "chronozarr --version"
    assert [step["long_running"] for step in plan["steps"]] == [False] * 4 + [True, True]
    assert plan["chronozarr_version"] in plan["steps"][0]["text"]
    assert [item["text"].split()[:3] for item in plan["skipped"]] == [["python", "-m", "pip"]]
    assert [item["language"] for item in plan["not_run"]] == ["python"]


def test_steps_carry_the_block_text_unchanged():
    plan = qc.build_plan(page(), "page.md")

    assert [step["text"] for step in plan["steps"]] == [
        INSTALL,
        EXPORT,
        DRY_RUN,
        CONVERT,
        SHARE,
        PREVIEW,
    ]
    assert [step["index"] for step in plan["steps"]] == [1, 2, 3, 4, 5, 6]
    assert plan["steps"][0]["label"] == "01 uv tool install --python 3.13 'chronozarr[geo]==1.2.3'"
    assert plan["steps"][1]["heading"] == "2. Get a dated raster series"
    assert plan["chronozarr_version"] == "1.2.3"


def test_share_and_preview_are_long_running_with_readiness_checks():
    plan = qc.build_plan(page(), "page.md")
    by_command = {step["text"]: step for step in plan["steps"]}

    share = by_command[SHARE]
    assert share["long_running"] is True
    assert share["ready"]["kind"] == "open-link"
    assert share["ready"]["scheme"] == "https"
    assert share["ready"]["host_suffix"] == ".trycloudflare.com"
    preview = by_command[PREVIEW]
    assert preview["long_running"] is True
    assert preview["ready"]["scheme"] == "http"
    assert preview["ready"]["host"] == "127.0.0.1"
    assert by_command[CONVERT]["ready"] is None
    assert by_command[CONVERT]["long_running"] is False


def test_the_alternative_route_is_skipped_and_named():
    plan = qc.build_plan(page(extra=PIP_ROUTE), "page.md")

    assert labels(plan)[0].startswith("uv tool install")
    assert "python -m pip install 'chronozarr[geo]==1.2.3'" not in [
        step["text"] for step in plan["steps"]
    ]
    assert len(plan["skipped"]) == 1
    skipped = plan["skipped"][0]
    assert skipped["heading"] == "1. Install"
    assert skipped["text"] == "python -m pip install 'chronozarr[geo]==1.2.3'"
    assert "instead" in skipped["reason"]


def test_the_word_or_is_not_an_alternative_marker():
    # The paragraph above `preview` says ", or to check a store first": still a step.
    plan = qc.build_plan(page(), "page.md")

    assert PREVIEW in [step["text"] for step in plan["steps"]]
    assert plan["skipped"] == []


def test_intro_is_the_paragraph_above_the_fence_and_resets_after_a_fence():
    blocks = qc.parse_blocks(
        "# T\n\n## A\n\n```bash\nuv x\n```\n\n```text\nout\n```\n\n"
        "Run this,\nthen that:\n\n```bash\nchronozarr y\n```\n"
    )

    assert [(b.language, b.intro, b.heading) for b in blocks] == [
        ("bash", "", "A"),
        ("text", "", "A"),
        ("bash", "Run this,\nthen that:", "A"),
    ]
    assert [b.line for b in blocks] == [5, 9, 16]


def test_hash_lines_inside_a_fence_are_not_headings():
    blocks = qc.parse_blocks("## Real\n\n```bash\n# a comment\nuv x\n```\n\n```text\nout\n```\n")

    assert [b.heading for b in blocks] == ["Real", "Real"]


def test_comment_lines_do_not_count_as_commands():
    plan = qc.build_plan(page(install="# put it on PATH\n" + INSTALL), "page.md")

    assert plan["steps"][0]["text"].startswith("# put it on PATH\nuv tool install")
    assert plan["steps"][0]["label"].startswith("01 uv tool install")


@pytest.mark.parametrize(
    ("markdown", "message"),
    [
        ("## A\n\n```sh\nuv x\n```\n", "'sh'"),
        ("## A\n\n```console\nuv x\n```\n", "'console'"),
        ("## A\n\n```\nuv x\n```\n", "no language"),
        ("## A\n\n```bash\nuv x\n", "never closed"),
        ("## A\n\n~~~bash\nuv x\n~~~\n", "tilde fences"),
        ("## A\n\n```bash title=x\nuv x\n```\n", "unreadable fence"),
    ],
)
def test_unrecognized_fences_fail_loudly(markdown, message):
    with pytest.raises(qc.StructureError, match=message):
        qc.parse_blocks(markdown)


def test_a_new_command_fails_until_the_job_knows_how_to_run_it():
    with pytest.raises(qc.StructureError, match="'brew'"):
        qc.build_plan(page(extra=bash("brew install cloudflared") + "\n"), "page.md")


def test_an_empty_bash_block_fails():
    with pytest.raises(qc.StructureError, match="empty bash block"):
        qc.build_plan(page(extra=bash("# nothing here") + "\n"), "page.md")


@pytest.mark.parametrize(
    ("override", "missing"),
    [
        ({"install": "chronozarr --version"}, "install"),
        ({"export": "chronozarr --version"}, "export-cog"),
        ({"dry_run": "chronozarr --version"}, "dry-run convert"),
        ({"share": "chronozarr --version"}, "share"),
        ({"preview": "chronozarr --version"}, "preview"),
    ],
)
def test_a_missing_required_command_fails(override, missing):
    with pytest.raises(qc.StructureError, match=rf"one '{missing}' command.*found 0"):
        qc.build_plan(page(**override), "page.md")


def test_a_repeated_command_fails():
    with pytest.raises(qc.StructureError, match=r"one 'convert' command.*found 2"):
        qc.build_plan(page(extra=bash(CONVERT) + "\n"), "page.md")


def test_commands_out_of_order_fail():
    # Moving the convert fence after share: same commands, different page.
    reordered = page(convert="chronozarr --version", share=CONVERT + "\n" + SHARE)

    with pytest.raises(qc.StructureError, match="needs a fence of its own"):
        qc.build_plan(reordered, "page.md")

    shuffled = page(dry_run=CONVERT, convert=DRY_RUN)
    with pytest.raises(qc.StructureError, match="no longer in the order"):
        qc.build_plan(shuffled, "page.md")


def test_share_and_preview_need_a_fence_of_their_own():
    with pytest.raises(qc.StructureError, match="runs until Ctrl-C"):
        qc.build_plan(page(preview="chronozarr --version\n" + PREVIEW), "page.md")


def test_two_pinned_versions_fail_even_when_one_is_in_a_skipped_block():
    pip_with_other_version = PIP_ROUTE.replace("1.2.3", "1.2.4")

    with pytest.raises(qc.StructureError, match=r"pin \['1.2.3', '1.2.4'\]"):
        qc.build_plan(page(extra=pip_with_other_version), "page.md")


def test_no_pinned_version_fails():
    with pytest.raises(qc.StructureError, match="no chronozarr version"):
        qc.build_plan(page(install="uv tool install --python 3.13 chronozarr"), "page.md")


def test_python_blocks_are_reported_not_run():
    plan = qc.build_plan(page(extra="```python\nimport xarray\n```\n"), "page.md")

    assert [item["text"] for item in plan["not_run"]] == ["import xarray"]
    assert "import xarray" not in [step["text"] for step in plan["steps"]]


def test_cli_writes_the_plan_and_names_what_it_skipped(tmp_path, capsys):
    source = tmp_path / "quickstart.md"
    source.write_text(page(extra=PIP_ROUTE), encoding="utf-8")
    out = tmp_path / "out" / "steps.json"

    assert qc.main([str(source), "--out", str(out)]) == 0

    plan = json.loads(out.read_text(encoding="utf-8"))
    assert plan["chronozarr_version"] == "1.2.3"
    assert len(plan["steps"]) == 6
    assert "skipped" in capsys.readouterr().err


def test_cli_prints_json_to_stdout_without_out(tmp_path, capsys):
    source = tmp_path / "quickstart.md"
    source.write_text(page(), encoding="utf-8")

    assert qc.main([str(source)]) == 0

    assert json.loads(capsys.readouterr().out)["chronozarr_version"] == "1.2.3"


def test_cli_exits_one_with_the_reason_when_the_structure_changed(tmp_path, capsys):
    source = tmp_path / "quickstart.md"
    source.write_text(page(share="chronozarr --version"), encoding="utf-8")

    assert qc.main([str(source)]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "one 'share' command" in captured.err
    assert str(source) in captured.err
