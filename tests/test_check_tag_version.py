"""Tests for ci/check_tag_version.py - the release workflow's tag gate."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "check_tag_version", Path(__file__).parents[1] / "ci" / "check_tag_version.py"
)
check_tag_version = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check_tag_version)


def _project(tmp_path: Path, version: str = "0.3.0", dunder: str | None = None) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "x"\nversion = "{version}"\n', encoding="utf-8"
    )
    package = tmp_path / "src" / "otelfeature_instrument"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        f'__version__ = "{dunder if dunder is not None else version}"\n', encoding="utf-8"
    )
    return tmp_path


@pytest.mark.parametrize(
    "tag, prerelease",
    [("v0.3.0", False), ("v0.3.0-rc.1", True), ("v0.3.0-rc.0", True), ("v0.3.0-rc.12", True)],
)
def test_release_and_rc_tags_of_the_version_pass(tmp_path, tag, prerelease):
    assert check_tag_version.check(tag, _project(tmp_path)) == (0, prerelease)


@pytest.mark.parametrize(
    "tag",
    [
        "v0.3.1",
        "v0x3x0",  # every '.' is a literal dot
        "v0.3.0-rc.1x",
        "v0.3.0-rc.",
        "v0.3.0-rc.01",
        "v0.3.0rc1",
        "v0.3.0-alpha.1",
        "v0.3",
        "v00.3.0",
        "v0.3.0.0",
        "0.3.0",
        "V0.3.0",
        "v0.3.0+local",
        "",
    ],
)
def test_anything_else_fails(tmp_path, tag):
    assert check_tag_version.check(tag, _project(tmp_path))[0] == 1


def test_a_stale_dunder_version_fails(tmp_path):
    assert check_tag_version.check("v0.3.0", _project(tmp_path, dunder="0.1.0"))[0] == 1


def test_a_missing_version_fails(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    assert check_tag_version.check("v", tmp_path)[0] == 1


def test_the_repository_itself_is_consistent():
    """pyproject.toml and __version__ agree, so the release tag can pass."""
    root = Path(__file__).parents[1]
    version = check_tag_version.tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    assert check_tag_version.check(f"v{version}", root) == (0, False)


def test_main_prints_the_prerelease_output(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(_project(tmp_path))
    assert check_tag_version.main(["x", "v0.3.0-rc.1"]) == 0
    assert capsys.readouterr().out == "prerelease=true\n"
