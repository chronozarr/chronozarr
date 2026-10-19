"""Findings of the input compatibility check, collected over every source and grouped per file."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from chronozarr._convert_manifest import ManifestDraft


@dataclass(frozen=True)
class Problem:
    """One reason a source cannot be converted as given, and what the user can do about it."""

    uri: str
    detail: str
    fix: str | None = None


class PreflightError(ValueError):
    """The sources are not compatible. `problems` has every finding, in source order.

    The message lists them grouped by file, each with a suggested remedy. Nothing is written,
    except the manifest that `--write-manifest` asks for. `draft` is what was found about the
    files (dates, band names), for that manifest.
    """

    def __init__(
        self, problems: Sequence[Problem], n_sources: int, draft: ManifestDraft | None = None
    ) -> None:
        self.problems = tuple(problems)
        self.n_sources = n_sources
        self.draft = draft
        super().__init__(render_problems(self.problems, n_sources))

    def wrote_manifest(self, note: str) -> None:
        """Record that the manifest was written: the message then says only that no store was,
        and `note` follows it."""
        self.args = (render_problems(self.problems, self.n_sources, "no store was written"),)
        self.add_note(note)


def render_problems(
    problems: Sequence[Problem], n_sources: int, written: str = "nothing was written"
) -> str:
    by_file: dict[str, list[Problem]] = {}
    for problem in problems:
        by_file.setdefault(problem.uri, []).append(problem)
    lines = [
        f"{len(problems)} problem(s) in {len(by_file)} of {n_sources} source file(s); {written}:"
    ]
    for uri, found in by_file.items():
        lines.append("")
        lines.append(uri)
        for problem in found:
            lines.append(f"  - {problem.detail}")
            if problem.fix:
                lines.append(f"    fix: {problem.fix}")
    return "\n".join(lines)
