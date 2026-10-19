"""Merge the hand-written classes (classes.json) into the results file; check them against runs.

    uv run --no-project python scripts/corpus/finalize_results.py

For each dataset and version it records the first-run class, the outcome of the verified fix chain
(read from the last stage's record) and the counts. It refuses to write when a claimed "OK after
fix" is not what the last fix record shows.
"""

from __future__ import annotations

import collections
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CORPUS = Path(
    os.environ.get("CHRONOZARR_CORPUS", Path(__file__).resolve().parents[2] / "data" / "corpus")
)
RESULTS = CORPUS / "results-2026-10-16.json"


def main() -> int:
    doc = json.loads(RESULTS.read_text())
    classes = {
        k: v
        for k, v in json.loads((HERE / "classes.json").read_text()).items()
        if not k.startswith("_")
    }
    manifest = {e["id"]: e for e in json.loads((HERE / "manifest.json").read_text())}
    records = doc["records"]
    table = {}
    problems = []
    for id, entry in classes.items():
        row = {
            "first_run": {},
            "after_fix": {},
            "fix": entry["fix"],
            "minutes": entry["minutes"],
            "first_message": entry["first_message"],
            "chain": entry["chain"],
        }
        for version in ("A", "B"):
            first = records[f"{id}|{version}"]
            row["first_run"][version] = entry["first_run"]
            if first["auto_class"] in ("OK", "WRONG", "CRASH", "UNCHECKED"):
                problems.append(
                    f"{id}|{version}: first run auto class {first['auto_class']}, "
                    "expected a refusal"
                )
            if not entry["fix_records"]:
                row["after_fix"][version] = entry["after_fix"]
                continue
            last = entry["fix_records"][-1]
            candidates = [f"{id}|{version}|{last}", f"{id}|{version}x|{last}"]
            rec = next((records[c] for c in candidates if c in records), None)
            if rec is None:
                problems.append(f"{id}|{version}: no record for the last fix stage {last}")
                continue
            row["after_fix"][version] = rec["auto_class"]
            if rec["auto_class"] != entry["after_fix"]:
                problems.append(
                    f"{id}|{version}: claimed {entry['after_fix']} after fix, "
                    f"run shows {rec['auto_class']}"
                )
        row["n_times"] = manifest[id]["n_times"]
        table[id] = row
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    summary = {}
    for version in ("A", "B"):
        first_run = collections.Counter(r["first_run"][version] for r in table.values())
        after_fix = collections.Counter(r["after_fix"][version] for r in table.values())
        summary[version] = {
            "first_run": dict(first_run),
            "after_fix": dict(after_fix),
            "pass_rate_first_run_OK": f"{first_run.get('OK', 0)}/{len(table)}",
        }
    doc["classification"] = table
    doc["summary"] = summary
    doc["notes"] = classes_notes()
    RESULTS.write_text(json.dumps(doc, indent=1) + "\n")
    print(json.dumps(summary, indent=1))
    return 0


def classes_notes() -> dict:
    rules = json.loads((HERE / "classes.json").read_text())["_rules"]
    return rules


if __name__ == "__main__":
    sys.exit(main())
