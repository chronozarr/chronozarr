"""Copy a validated store and viewer into a new, independently hosted static directory."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import chronozarr

REPO = Path(__file__).resolve().parents[2]


def build_bundle(store: Path, output: Path) -> None:
    store, output = store.resolve(), output.resolve()
    if output.exists():
        raise FileExistsError(f"Output already exists: {output}; choose a new directory")
    if output.is_relative_to(store):
        raise ValueError("Output must be outside the input store")
    errors = chronozarr.validate(store)
    if errors:
        raise ValueError("Invalid store:\n" + "\n".join(errors))
    # Validate assets before copying potentially large data. No build or npm install is needed.
    modules = ("demo", "shared", "chronozarr", "maplibre", "vendor")
    for name in modules:
        if not (REPO / "js" / name).is_dir():
            raise FileNotFoundError(
                f"Missing js/{name}; run this example from a complete checkout"
            )
    output.mkdir(parents=True)
    shutil.copytree(store, output / "store")
    for name in modules:
        shutil.copytree(REPO / "js" / name, output / name, ignore=shutil.ignore_patterns("verify"))
    shutil.copyfile(REPO / "js/favicon.svg", output / "favicon.svg")
    # Resolve URLs at runtime so the same bundle works at a domain root or under a subpath.
    (output / "index.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>chronozarr viewer</title>\n'
        '<script>const viewer = new URL("demo/index.html", location.href);\n'
        'viewer.searchParams.set("store", new URL("store", location.href).href);\n'
        "location.replace(viewer.href);</script>\n"
    )
    (output / "examples").mkdir()
    host = (REPO / "js/examples/embed.html").read_text()
    start = host.index("  const DEFAULT_STORE = ")
    end = host.index(";", start) + 1
    host = (
        host[:start]
        + '  const DEFAULT_STORE = new URL("../store", location.href).href;'
        + host[end:]
    )
    (output / "examples/embed.html").write_text(host)
    # The full viewer must not fall back to somebody else's data on an error.
    (output / "demo/catalog.json").write_text("[]\n")
    objects = [p for p in (output / "store").rglob("*") if p.is_file()]
    package = json.loads((REPO / "js/package.json").read_text())
    report = {
        "reader_version": package["version"],
        "store_objects": len(objects),
        "store_bytes": sum(p.stat().st_size for p in objects),
        "viewer": "index.html",
        "embed_example": "examples/embed.html",
        "note": "Storage inventory, not a performance benchmark. Data was copied unchanged.",
    }
    (output / "bundle.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"output": str(output), **report}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("store", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    build_bundle(args.store, args.output)
