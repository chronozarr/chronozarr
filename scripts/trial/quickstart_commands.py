"""Turn the commands printed on docs/quickstart.md into ordered steps for the clean-room CI job.

The job runs what a newcomer copies from the page, so this parser reads the page and nothing
else: every ``bash`` fence is one step, in page order, with its text unchanged. It fails loudly
(StructureError, exit 1) when the page stops looking the way the job expects, so a docs edit
cannot silently shrink what the job tests:

- a fence with a language the job does not know (only bash, text and python are known);
- a command other than ``uv`` or ``chronozarr`` in a bash fence: decide how CI runs it first;
- a missing, repeated or reordered install, export-cog, dry-run convert, convert, share or
  preview command;
- a share or preview command that shares its fence with another command;
- install pins that name more than one chronozarr version.

One alternative route is skipped, and the plan says so: a bash fence whose introducing paragraph
contains the word "instead" (the pip install: "To install with pip instead, ..."). The word
"or" is not the marker: the paragraph before `chronozarr preview` contains ", or to check a
store before you share it", and that command is a step of its own, not an alternative.

``share`` and ``preview`` never return, so they are marked long running with a readiness check:
the ``open:`` link they print names the store, and ``<store>/zarr.json`` must answer 200.

Usage: quickstart_commands.py docs/quickstart.md [--out steps.json]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

KNOWN_LANGUAGES = frozenset({"bash", "text", "python"})
ALLOWED_COMMANDS = frozenset({"uv", "chronozarr"})
ALTERNATIVE_MARKER = re.compile(r"\binstead\b", re.IGNORECASE)
LONG_RUNNING = re.compile(r"^chronozarr\s+(share|preview)\b")
VERSION_PIN = re.compile(r"chronozarr(?:\[[A-Za-z0-9_,-]+\])?==([0-9][0-9A-Za-z.+!-]*)")

OPEN_FENCE = re.compile(r"^\s*```\s*(\S*)\s*$")
HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$")

# The page's commands, in the order the page gives them. Each must occur exactly once.
REQUIRED_COMMANDS = (
    ("install", re.compile(r"^uv tool install\b")),
    ("export-cog", re.compile(r"^chronozarr export-cog\b")),
    ("dry-run convert", re.compile(r"^chronozarr convert\b.*--dry-run\b")),
    ("convert", re.compile(r"^chronozarr convert\b(?!.*--dry-run\b)")),
    ("share", re.compile(r"^chronozarr share\b")),
    ("preview", re.compile(r"^chronozarr preview\b")),
)

# share's tunnel link usually takes 30 s; its own wait allows up to 90 s after cloudflared starts.
SHARE_READY = {
    "kind": "open-link",
    "scheme": "https",
    "host_suffix": ".trycloudflare.com",
    "timeout_seconds": 180,
}
PREVIEW_READY = {
    "kind": "open-link",
    "scheme": "http",
    "host": "127.0.0.1",
    "timeout_seconds": 60,
}


class StructureError(Exception):
    """The page no longer has the structure the clean-room job was written against."""


@dataclass(frozen=True)
class Block:
    language: str
    text: str
    line: int  # 1-based line of the opening fence
    heading: str  # nearest heading above the fence, "" before the first
    intro: str  # the paragraph just above the fence; "" when a heading or another fence is


@dataclass(frozen=True)
class Step:
    index: int
    heading: str
    label: str
    text: str
    long_running: bool
    ready: dict[str, Any] | None


def parse_blocks(markdown: str) -> list[Block]:
    """Every fenced block in page order, with its heading and the paragraph that introduces it."""
    blocks: list[Block] = []
    heading = ""
    paragraph: list[str] = []
    last_paragraph = ""
    intro = ""
    fence_line = 0
    language = ""
    body: list[str] = []
    inside = False

    for number, line in enumerate(markdown.splitlines(), start=1):
        if inside:
            if line.strip() == "```":
                blocks.append(Block(language, "\n".join(body), fence_line, heading, intro))
                inside = False
                last_paragraph = ""
            else:
                body.append(line)
            continue
        if line.lstrip().startswith("~~~"):
            raise StructureError(f"line {number}: tilde fences are not supported by this parser")
        opened = OPEN_FENCE.match(line)
        if line.lstrip().startswith("```") and not opened:
            raise StructureError(f"line {number}: unreadable fence {line.strip()!r}")
        if opened:
            intro = "\n".join(paragraph) if paragraph else last_paragraph
            paragraph = []
            language, fence_line, body, inside = opened.group(1), number, [], True
            if not language:
                raise StructureError(f"line {number}: a fenced block with no language")
            if language not in KNOWN_LANGUAGES:
                raise StructureError(
                    f"line {number}: fence language {language!r} is not one of "
                    f"{sorted(KNOWN_LANGUAGES)}; teach quickstart_commands.py how to run it"
                )
            continue
        match = HEADING.match(line)
        if match:
            heading, paragraph, last_paragraph = match.group(1), [], ""
        elif not line.strip():
            if paragraph:
                last_paragraph = "\n".join(paragraph)
                paragraph = []
        else:
            paragraph.append(line)
    if inside:
        raise StructureError(f"line {fence_line}: a fence that is never closed")
    return blocks


def command_lines(text: str) -> list[str]:
    """The lines of a bash block that run something (blank lines and comments dropped)."""
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def check_commands(block: Block) -> list[str]:
    lines = command_lines(block.text)
    if not lines:
        raise StructureError(f"line {block.line}: an empty bash block")
    for line in lines:
        word = line.split(None, 1)[0]
        if word not in ALLOWED_COMMANDS:
            raise StructureError(
                f"line {block.line} ({block.heading}): the page now runs {word!r} "
                f"({line!r}); the job knows only {sorted(ALLOWED_COMMANDS)}. Decide how the "
                "clean room runs it, then extend ALLOWED_COMMANDS"
            )
    return lines


def classify(lines: list[str], block: Block) -> tuple[bool, dict[str, Any] | None]:
    """Whether the block never returns, and how to tell that it is ready."""
    long_running = [line for line in lines if LONG_RUNNING.match(line)]
    if not long_running:
        return False, None
    if len(lines) != 1:
        raise StructureError(
            f"line {block.line} ({block.heading}): {long_running[0]!r} runs until Ctrl-C, so it "
            "needs a fence of its own; the job stops it after the link answers"
        )
    ready = SHARE_READY if lines[0].startswith("chronozarr share") else PREVIEW_READY
    return True, dict(ready)


def check_required(steps: list[Step]) -> None:
    """Each required command occurs once, and in the page's order."""
    positions: list[int] = []
    for name, pattern in REQUIRED_COMMANDS:
        found = [
            step.index
            for step in steps
            for line in command_lines(step.text)
            if pattern.match(line)
        ]
        if len(found) != 1:
            raise StructureError(
                f"expected one {name!r} command on the page, found {len(found)}; the page's "
                "structure changed, so update quickstart_commands.py with it"
            )
        positions.append(found[0])
    if positions != sorted(positions):
        raise StructureError(
            "the page's commands are no longer in the order install, export-cog, dry-run "
            f"convert, convert, share, preview (steps {positions})"
        )


def pinned_version(blocks: list[Block]) -> str:
    """The one chronozarr version the page's install commands name, pip route included."""
    versions = {
        match.group(1)
        for block in blocks
        if block.language == "bash"
        for match in VERSION_PIN.finditer(block.text)
    }
    if len(versions) != 1:
        raise StructureError(
            f"the page's install commands pin {sorted(versions) or 'no chronozarr version'}; "
            "they must pin exactly one"
        )
    return versions.pop()


def build_plan(markdown: str, source: str) -> dict[str, Any]:
    blocks = parse_blocks(markdown)
    steps: list[Step] = []
    skipped: list[dict[str, Any]] = []
    not_run: list[dict[str, Any]] = []
    for block in blocks:
        if block.language == "python":
            not_run.append(
                {
                    "heading": block.heading,
                    "line": block.line,
                    "language": "python",
                    "reason": "a Python block; the job runs bash blocks only",
                    "text": block.text,
                }
            )
            continue
        if block.language != "bash":
            continue
        if ALTERNATIVE_MARKER.search(block.intro):
            skipped.append(
                {
                    "heading": block.heading,
                    "line": block.line,
                    "reason": 'the page introduces it with "instead": an alternative route',
                    "text": block.text,
                }
            )
            continue
        lines = check_commands(block)
        long_running, ready = classify(lines, block)
        index = len(steps) + 1
        steps.append(
            Step(
                index=index,
                heading=block.heading,
                label=f"{index:02d} {lines[0]}",
                text=block.text,
                long_running=long_running,
                ready=ready,
            )
        )
    check_required(steps)
    return {
        "source": source,
        "chronozarr_version": pinned_version(blocks),
        "steps": [asdict(step) for step in steps],
        "skipped": skipped,
        "not_run": not_run,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("page", type=Path, help="docs/quickstart.md")
    parser.add_argument("--out", type=Path, help="write the steps JSON here instead of stdout")
    args = parser.parse_args(argv)
    try:
        plan = build_plan(args.page.read_text(encoding="utf-8"), str(args.page))
    except StructureError as exc:
        print(f"quickstart_commands: {args.page}: {exc}", file=sys.stderr)
        return 1
    for item in plan["skipped"]:
        first = item["text"].splitlines()[0]
        print(
            f"skipped {args.page}:{item['line']} ({item['heading']}): {first} -- {item['reason']}",
            file=sys.stderr,
        )
    for item in plan["not_run"]:
        print(
            f"not run {args.page}:{item['line']} ({item['heading']}): {item['reason']}",
            file=sys.stderr,
        )
    rendered = json.dumps(plan, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
