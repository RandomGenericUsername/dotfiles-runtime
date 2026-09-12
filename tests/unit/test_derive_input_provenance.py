"""R-3 / AD-43: derivation inputs resolve from the install spine only.

Spine-first; repo only via the explicit DOTFILES_DEV_INPUTS_ROOT override (and
logged); a missing input is a loud typed error, and provenance is reportable
(used by doctor).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from runtime.application.derive import (
    find_templates_dir,
    input_provenance,
    require_input,
    resolve_input,
)
from runtime.domain.models import MissingDerivationInputError

_KEYS = ("csg_templates", "weg_effects", "icon_templates", "icon_mappings")


def _populate_spine(spine: Path) -> None:
    (spine / "icon-templates").mkdir(parents=True)
    (spine / "icon-templates" / "x.svg").write_text("<svg/>", encoding="utf-8")
    (spine / "icon-mappings").mkdir(parents=True)
    (spine / "icon-mappings" / "icons.yaml").write_text("icons: {}\n", encoding="utf-8")
    templates = spine / "config" / "color-scheme-generator" / "templates"
    templates.mkdir(parents=True)
    (templates / "t.j2").write_text("x", encoding="utf-8")
    weg = spine / "config" / "weg"
    weg.mkdir(parents=True)
    (weg / "effects.yaml").write_text("effects: {}\n", encoding="utf-8")


def _populate_repo(repo: Path) -> None:
    dirs = [
        "src/cli-tools/color-scheme-generator/src/color_scheme_generator/defaults/templates",
        "dotfiles/assets/icon-templates",
    ]
    for rel in dirs:
        d = repo / rel
        d.mkdir(parents=True, exist_ok=True)
        (d / "f").write_text("x", encoding="utf-8")
    files = [
        "src/cli-tools/wallpaper-effects-generator/src/wallpaper_effects_generator/defaults/effects.yaml",
        "dotfiles/config/icon-template-color-scheme-mappings/icons.yaml",
    ]
    for rel in files:
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x", encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_dev_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate from a dev shell that exports the override."""
    monkeypatch.delenv("DOTFILES_DEV_INPUTS_ROOT", raising=False)


def test_spine_resolution_reports_spine_source(tmp_path: Path) -> None:
    spine = tmp_path / "install"
    _populate_spine(spine)
    for key in _KEYS:
        path, source = resolve_input(key, spine)
        assert source == "spine"
        assert path is not None


def test_missing_input_is_missing_without_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DOTFILES_DEV_INPUTS_ROOT", raising=False)
    spine = tmp_path / "install"  # empty
    for key in _KEYS:
        path, source = resolve_input(key, spine)
        assert (path, source) == (None, "missing")
    assert find_templates_dir(spine) is None


def test_require_input_raises_typed_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOTFILES_DEV_INPUTS_ROOT", raising=False)
    with pytest.raises(MissingDerivationInputError, match="install spine"):
        require_input("csg_templates", tmp_path / "install")


def test_dev_override_restores_repo_and_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    repo = tmp_path / "checkout"
    _populate_repo(repo)
    monkeypatch.setenv("DOTFILES_DEV_INPUTS_ROOT", str(repo))
    import runtime.application.derive as derive_module

    derive_module._WARNED.clear()  # warnings are deduped per process
    spine = tmp_path / "install"  # empty
    with caplog.at_level(logging.WARNING):
        for key in _KEYS:
            path, source = resolve_input(key, spine)
            assert source == "repo"
            assert path is not None
    assert any("dev checkout" in r.message for r in caplog.records)


def test_dev_override_does_not_shadow_a_present_spine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spine = tmp_path / "install"
    _populate_spine(spine)
    repo = tmp_path / "checkout"
    _populate_repo(repo)
    monkeypatch.setenv("DOTFILES_DEV_INPUTS_ROOT", str(repo))
    # spine still wins when present
    assert resolve_input("csg_templates", spine)[1] == "spine"


def test_input_provenance_covers_all_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DOTFILES_DEV_INPUTS_ROOT", raising=False)
    provenance = input_provenance(tmp_path / "install")
    assert set(provenance) == set(_KEYS)
    assert all(source == "missing" for _, source in provenance.values())


@pytest.mark.parametrize("key", _KEYS)
def test_require_input_all_keys_raise_when_missing(key: str, tmp_path: Path) -> None:
    with pytest.raises(MissingDerivationInputError, match="install spine"):
        require_input(key, tmp_path / "install")


def test_repo_shaped_ancestor_ignored_without_override(tmp_path: Path) -> None:
    # The removed implicit walk searched install_spine.parents for src/cli-tools.
    # With the override unset, a repo-shaped ancestor must NOT be read.
    repo = tmp_path / "checkout"
    _populate_repo(repo)
    spine = repo / "nested" / "install"  # `repo` is an ancestor of the spine
    spine.mkdir(parents=True)
    for key in _KEYS:
        assert resolve_input(key, spine) == (None, "missing")
