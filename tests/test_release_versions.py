"""Release checks refuse mixed package and lockfile versions before publication."""

from __future__ import annotations

import json

import pytest
from scripts.check_release import check_versions, npm_version

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("python_version", "javascript_version"),
    [
        ("0.4.1", "0.4.1"),
        ("0.5.0.dev0", "0.5.0-dev.0"),
        ("0.5.0a2", "0.5.0-alpha.2"),
        ("0.5.0b3", "0.5.0-beta.3"),
        ("0.5.0rc1", "0.5.0-rc.1"),
    ],
)
def test_python_and_npm_share_a_release_tag(tmp_path, python_version, javascript_version):
    (tmp_path / "pyproject.toml").write_text(f'[project]\nversion = "{python_version}"\n')
    (tmp_path / "uv.lock").write_text(
        f'[[package]]\nname = "chronozarr"\nversion = "{python_version}"\n'
    )
    (tmp_path / "js").mkdir()
    (tmp_path / "js/package.json").write_text(json.dumps({"version": javascript_version}))
    (tmp_path / "js/package-lock.json").write_text(
        json.dumps(
            {"version": javascript_version, "packages": {"": {"version": javascript_version}}}
        )
    )
    assert check_versions(tmp_path, f"v{javascript_version}") == (
        python_version,
        javascript_version,
    )
    with pytest.raises(ValueError, match="release tag"):
        check_versions(tmp_path, "v9.9.9")
    (tmp_path / "uv.lock").write_text('[[package]]\nname = "chronozarr"\nversion = "0.1.0"\n')
    with pytest.raises(ValueError, match=r"uv\.lock"):
        check_versions(tmp_path)
    (tmp_path / "uv.lock").write_text(
        f'[[package]]\nname = "chronozarr"\nversion = "{python_version}"\n'
    )
    for filename, key in (
        ("package.json", "version"),
        ("package-lock.json", "version"),
        ("package-lock.json", "root version"),
    ):
        path = tmp_path / "js" / filename
        original = path.read_text()
        broken = json.loads(original)
        if key == "root version":
            broken["packages"][""]["version"] = "0.1.0"
        else:
            broken[key] = "0.1.0"
        path.write_text(json.dumps(broken))
        with pytest.raises(ValueError, match="must be"):
            check_versions(tmp_path)
        path.write_text(original)


@pytest.mark.parametrize("version", ["v0.5.0", "0.5", "0.5.0+local", "0.5.0rc1.dev0"])
def test_non_release_versions_are_refused(version):
    with pytest.raises(ValueError, match="unsupported release version"):
        npm_version(version)
