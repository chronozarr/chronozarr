"""Findings of the input compatibility check, collected over every source and grouped per file."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Problem:
    """One reason a source cannot be converted as given, and what the user can do about it."""

    uri: str
    detail: str
    fix: str | None = None


class PreflightError(ValueError):
    """The sources are not compatible. `problems` has every finding, in source order.

    The message lists them grouped by file, each with a suggested remedy. Nothing is written.
    """

    def __init__(self, problems: Sequence[Problem], n_sources: int) -> None:
        self.problems = tuple(problems)
        self.n_sources = n_sources
        super().__init__(render_problems(self.problems, n_sources))


def render_problems(problems: Sequence[Problem], n_sources: int) -> str:
    by_file: dict[str, list[Problem]] = {}
    for problem in problems:
        by_file.setdefault(problem.uri, []).append(problem)
    lines = [
        f"{len(problems)} problem(s) in {len(by_file)} of {n_sources} source file(s); "
        "nothing was written:"
    ]
    for uri, found in by_file.items():
        lines.append("")
        lines.append(uri)
        for problem in found:
            lines.append(f"  - {problem.detail}")
            if problem.fix:
                lines.append(f"    fix: {problem.fix}")
    return "\n".join(lines)
