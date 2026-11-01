"""Corpus correctness follows source values and geometry, independent of longitude spelling."""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "corpus_runner_review", REPO / "scripts/corpus/run_corpus.py"
)
assert spec is not None and spec.loader is not None
corpus = importlib.util.module_from_spec(spec)
spec.loader.exec_module(corpus)


def correct_record():
    step = {"timed_out": False, "exit": 0, "stderr_tail": "", "stdout_tail": ""}
    return {
        "dry_run": step.copy(),
        "convert": step.copy(),
        "validate": step.copy(),
        "checks": {
            "dates_equal": True,
            "crs_equal": True,
            "x_beyond_180": True,
            "values": {
                "checked": True,
                "compared": 9,
                "n_mismatch": 0,
                "invalid_shown_as_valid": 0,
            },
            "range": {"store_outside": 0, "source_outside": 0},
        },
    }


@pytest.mark.parametrize("beyond", [False, True])
def test_longitude_convention_does_not_change_correctness(beyond):
    record = correct_record()
    record["checks"]["x_beyond_180"] = beyond
    assert corpus.auto_class(record) == "OK"


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        (None, "dates_equal", False),
        (None, "crs_equal", False),
        ("values", "n_mismatch", 1),
        ("values", "invalid_shown_as_valid", 1),
        ("values", "compared", 0),
        ("values", "checked", False),
        ("range", "store_outside", 1),
        ("range", "source_outside", 1),
    ],
)
def test_real_source_mismatches_still_fail(section, key, value):
    record = correct_record()
    target = record["checks"] if section is None else record["checks"][section]
    target[key] = value
    assert corpus.auto_class(record) == "WRONG"


def test_recording_policy_preserves_existing_evidence_and_the_callers_record(
    tmp_path, monkeypatch
):
    results = tmp_path / "results.json"
    previous = {"auto_class": "WRONG", "checks": {"x_beyond_180": True}}
    results.write_text(json.dumps({"records": {"historical": previous}}))
    monkeypatch.setattr(corpus, "RESULTS", results)
    record = correct_record()
    saved = copy.deepcopy(record)
    corpus.store_record("new", record)
    document = json.loads(results.read_text())
    assert document["records"]["historical"] == previous
    assert document["records"]["new"]["evaluation_policy"] == "source-values-v2"
    assert record == saved


def test_classification_guidance_separates_longitude_from_correctness():
    classes = json.loads((REPO / "scripts/corpus/classes.json").read_text())
    assert classes["_rules"]["evaluation_policy"] == "source-values-v2"
    assert "diagnostic" in classes["_rules"]["longitude"]
    for dataset in ("oisst-psl-daily", "era5-fiji-antimeridian-nc"):
        # The corrected minimum route keeps native values and requires no resampling.
        fix = classes[dataset]["fix"]
        assert "--crs EPSG:4326" in fix
        assert "warp" not in fix and "assign_coords" not in fix


def test_source_archive_import_needs_no_git(tmp_path, monkeypatch):
    source = tmp_path / "archive" / "scripts" / "corpus" / "run_corpus.py"
    source.parent.mkdir(parents=True)
    source.write_bytes((REPO / "scripts/corpus/run_corpus.py").read_bytes())
    monkeypatch.setattr(
        subprocess, "run", lambda *_args, **_kwargs: pytest.fail("an archive must not invoke Git")
    )
    spec = importlib.util.spec_from_file_location("archive_corpus_runner", source)
    assert spec is not None and spec.loader is not None
    archived = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(archived)
    assert archived.WORKTREE_SHA == "source-archive"


@pytest.mark.parametrize(
    "error", [FileNotFoundError("git"), subprocess.CalledProcessError(128, "git")]
)
def test_unavailable_git_does_not_prevent_importing_the_evaluator(tmp_path, monkeypatch, error):
    (tmp_path / ".git").mkdir()

    def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(subprocess, "run", fail)
    assert corpus.worktree_revision(tmp_path) == "unknown-revision"
