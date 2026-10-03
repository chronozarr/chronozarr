"""Check first-party Python/JS dependencies using the checked-in Sentrux rules.

Python imports include deferred imports. JS imports and re-exports are static ESM
statements; runtime computed imports are outside this check. Vendored JS is excluded.
Only the rules implemented here are accepted, so adding an unsupported rule fails CI.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import re
import sys
import tomllib
from pathlib import Path

JS_IMPORT = re.compile(
    r"^\s*(?:import\s+(?:[^;]*?\bfrom\s+)?|export\s+[^;]*?\bfrom\s+)"
    r"['\"]([^'\"]+)['\"]",
    re.MULTILINE,
)


JS_COMMENTS = re.compile(
    r"\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`|"
    r"(?P<comment>//[^\n]*|/\*[\s\S]*?\*/)",
)


def javascript_source(text: str) -> str:
    """Blank comments while preserving quoted import paths and line positions."""
    return JS_COMMENTS.sub(
        lambda match: (
            re.sub(r"[^\n]", " ", match.group())
            if match.group("comment") is not None
            else match.group()
        ),
        text,
    )


def module_name(path: Path, root: Path) -> str:
    parts = path.relative_to(root / "src").with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def python_import_names(
    node: ast.AST, package: tuple[str, ...], modules: dict[str, str]
) -> list[str]:
    """Resolve one Python import, preferring actual submodules over parent symbols."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if not isinstance(node, ast.ImportFrom):
        return []
    base = node.module or ""
    if node.level:
        base = ".".join((*package[: len(package) - node.level + 1], base)).rstrip(".")
    return [
        f"{base}.{alias.name}" if f"{base}.{alias.name}" in modules else base
        for alias in node.names
    ]


def python_dependencies(path: Path, root: Path, modules: dict[str, str]) -> set[str]:
    package = path.relative_to(root / "src").with_suffix("").parts[:-1]
    tree = ast.parse(path.read_text(), filename=str(path))
    names = {
        name for node in ast.walk(tree) for name in python_import_names(node, package, modules)
    }
    return {modules[name] for name in names if name in modules}


def javascript_dependencies(path: Path, paths: dict[Path, str]) -> set[str]:
    names = [match.group(1) for match in JS_IMPORT.finditer(javascript_source(path.read_text()))]
    targets = {(path.parent / name).resolve() for name in names if name.startswith(".")}
    return {paths[target] for target in targets if target in paths}


def dependency_graph(root: Path) -> dict[str, set[str]]:
    python_files = sorted((root / "src/chronozarr").rglob("*.py"))
    javascript_files = sorted(
        p
        for p in (root / "js").rglob("*.js")
        if not {"vendor", "node_modules"}.intersection(p.relative_to(root).parts)
    )
    paths = {p.resolve(): p.relative_to(root).as_posix() for p in python_files + javascript_files}
    modules = {module_name(p, root): paths[p.resolve()] for p in python_files}
    graph = {paths[p.resolve()]: python_dependencies(p, root, modules) for p in python_files}
    graph.update({paths[p.resolve()]: javascript_dependencies(p, paths) for p in javascript_files})
    return graph


def cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Return cyclic strongly connected components, counted once each."""
    indices: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    active: set[str] = set()
    found: list[list[str]] = []

    def visit(node: str) -> None:
        indices[node] = low[node] = len(indices)
        stack.append(node)
        active.add(node)
        for target in sorted(graph[node]):
            if target not in indices:
                visit(target)
                low[node] = min(low[node], low[target])
            elif target in active:
                low[node] = min(low[node], indices[target])
        if low[node] == indices[node]:
            component = []
            while True:
                target = stack.pop()
                active.remove(target)
                component.append(target)
                if target == node:
                    break
            if len(component) > 1 or node in graph[node]:
                found.append(sorted(component))

    for node in sorted(graph):
        if node not in indices:
            visit(node)
    return found


def validate_boundary(rule: dict) -> None:
    if set(rule) != {"from", "to", "reason"}:
        raise ValueError("Boundaries require from, to, and reason fields")
    if not all(isinstance(value, str) and value for value in rule.values()):
        raise ValueError("Boundary fields must be nonempty strings")


def validate_layer(rule: dict) -> None:
    if set(rule) != {"name", "paths", "order"}:
        raise ValueError("Layers require name, paths, and order fields")
    if not isinstance(rule["name"], str) or type(rule["order"]) is not int:
        raise ValueError("Layer name and order must be a string and integer")
    if not isinstance(rule["paths"], list):
        raise ValueError("Layer paths must be a list")
    if not all(isinstance(path, str) for path in rule["paths"]):
        raise ValueError("Layer paths must contain strings")


def load_rules(root: Path) -> dict:
    rules = tomllib.loads((root / ".sentrux/rules.toml").read_text())
    if set(rules) - {"constraints", "boundaries", "layers"}:
        raise ValueError("Unsupported top-level architecture rule")
    constraints = rules.get("constraints", {})
    if set(constraints) - {"max_cycles"}:
        raise ValueError("Unsupported architecture constraint")
    maximum = constraints.get("max_cycles")
    if maximum is not None and (type(maximum) is not int or maximum < 0):
        raise ValueError("max_cycles must be a nonnegative integer")
    for rule in rules.get("boundaries", []):
        validate_boundary(rule)
    for rule in rules.get("layers", []):
        validate_layer(rule)
    if not rules.get("boundaries") and not rules.get("layers") and maximum is None:
        raise ValueError("No architecture rules configured")
    return rules


def boundary_problems(source: str, target: str, boundaries: list[dict]) -> list[str]:
    return [
        f"{source} -> {target}: {rule['reason']}"
        for rule in boundaries
        if fnmatch.fnmatchcase(source, rule["from"]) and fnmatch.fnmatchcase(target, rule["to"])
    ]


def matching_layers(path: str, layers: list[dict]) -> list[dict]:
    return [
        layer
        for layer in layers
        if any(fnmatch.fnmatchcase(path, pattern) for pattern in layer["paths"])
    ]


def layer_problems(source: str, target: str, layers: list[dict]) -> list[str]:
    source_layers = matching_layers(source, layers)
    target_layers = matching_layers(target, layers)
    return [
        f"{source} -> {target}: upward layer dependency"
        for upper in source_layers
        for lower in target_layers
        if upper["order"] < lower["order"]
    ]


def check(root: Path) -> list[str]:
    rules = load_rules(root)
    graph = dependency_graph(root)
    if not graph:
        raise ValueError("No first-party source files found")
    problems = []
    for source, targets in sorted(graph.items()):
        for target in sorted(targets):
            problems.extend(boundary_problems(source, target, rules.get("boundaries", [])))
            problems.extend(layer_problems(source, target, rules.get("layers", [])))
    maximum = rules.get("constraints", {}).get("max_cycles")
    found = cycles(graph)
    if maximum is not None and len(found) > maximum:
        problems.extend("Dependency cycle: " + ", ".join(component) for component in found)
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        problems = check(args.root.resolve())
    except (ValueError, OSError, SyntaxError) as error:
        print(f"Architecture check failed: {error}", file=sys.stderr)
        return 1
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    print("Architecture rules pass (first-party Python and static JavaScript imports).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
