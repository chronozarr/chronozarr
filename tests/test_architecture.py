"""Architecture checks must reject regressions, including deferred imports/re-exports."""

from pathlib import Path

import pytest
from scripts.check_architecture import check

pytestmark = pytest.mark.unit


def fixture_root(tmp_path: Path, files: dict[str, str], rules: str) -> Path:
    for name, content in {".sentrux/rules.toml": rules, **files}.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return tmp_path


def test_deferred_python_cycle_and_boundary(tmp_path):
    root = fixture_root(
        tmp_path,
        {
            "src/chronozarr/schema.py": (
                "def validate():\n    from chronozarr.decode import read\n"
            ),
            "src/chronozarr/decode.py": "from chronozarr import schema\n",
        },
        """[constraints]
max_cycles = 0
[[boundaries]]
from = "src/chronozarr/schema.py"
to = "src/chronozarr/decode.py"
reason = "neutral transport"
""",
    )
    problems = check(root)
    assert any("neutral transport" in problem for problem in problems)
    assert any("Dependency cycle" in problem for problem in problems)


def test_javascript_reexports_and_side_effect_imports(tmp_path):
    root = fixture_root(
        tmp_path,
        {
            "js/maplibre/layer.js": "export { value } from '../demo/value.js';\n",
            "js/demo/value.js": "import '../maplibre/layer.js';\nexport const value = 1;\n",
        },
        """[constraints]
max_cycles = 0
[[boundaries]]
from = "js/maplibre/*"
to = "js/demo/*"
reason = "demo consumer"
""",
    )
    problems = check(root)
    assert any("demo consumer" in problem for problem in problems)
    assert any("Dependency cycle" in problem for problem in problems)


def test_relative_python_import_and_layers(tmp_path):
    root = fixture_root(
        tmp_path,
        {
            "src/chronozarr/store.py": "from .decode import read\n",
            "src/chronozarr/decode.py": "def read(): pass\n",
        },
        """[[layers]]
name = "transport"
paths = ["src/chronozarr/store.py"]
order = 0
[[layers]]
name = "reader"
paths = ["src/chronozarr/decode.py"]
order = 1
""",
    )
    assert any("upward layer dependency" in problem for problem in check(root))


@pytest.mark.parametrize("rules", ["[constraints]\nmax_coupling = 'B'", "[typo]\nx = 1", ""])
def test_unsupported_or_empty_rules_fail(tmp_path, rules):
    root = fixture_root(tmp_path, {"src/chronozarr/store.py": ""}, rules)
    with pytest.raises(ValueError):
        check(root)


def test_commented_javascript_imports_do_not_create_dependencies(tmp_path):
    root = fixture_root(
        tmp_path,
        {
            "js/maplibre/layer.js": "/*\nimport '../demo/value.js';\n*/\n",
            "js/demo/value.js": "export const value = 'https://example.invalid';\n",
        },
        """[[boundaries]]
from = "js/maplibre/*"
to = "js/demo/*"
reason = "demo consumer"
""",
    )
    assert check(root) == []
