"""REQ-006 — ``inspect palette`` CLI rendering (vocabulary conformance)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from runtime.application.inspect import PaletteInspection
from runtime.application.palette_conformance import check_palette_entry
from runtime.cli.main import app

runner = CliRunner()

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "palettes"


def _inspection(fixture: str) -> PaletteInspection:
    return PaletteInspection(
        palette="a" * 64,
        entry_present=True,
        conformance=check_palette_entry(FIXTURES / fixture),
    )


def _patch(monkeypatch: pytest.MonkeyPatch, fixture: str) -> None:
    monkeypatch.setattr(
        "runtime.cli.main._run_inspect_palette",
        lambda: _inspection(fixture),
    )


def test_conforming_palette_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, "v1-valid")
    result = runner.invoke(app, ["inspect", "palette"])
    assert result.exit_code == 0
    assert "palette vocabulary v1: conforming" in result.output
    assert "extra roles:" in result.output


def test_nonconforming_palette_exits_one_naming_on_backdrop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch(monkeypatch, "v0.4.0")
    result = runner.invoke(app, ["inspect", "palette"])
    assert result.exit_code == 1
    assert "NONCONFORMING" in result.output
    assert "on_backdrop" in result.output


def test_extra_roles_are_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, "v1-extra-roles")
    result = runner.invoke(app, ["inspect", "palette"])
    assert result.exit_code == 0
    assert "experimental_surface" in result.output


def test_json_format_carries_vocabulary_object(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, "v0.4.0")
    result = runner.invoke(app, ["inspect", "palette", "--format", "json"])
    assert result.exit_code == 1
    assert '"vocabulary_version"' in result.output
    assert '"missing_roles"' in result.output
    assert '"on_backdrop"' in result.output


def test_absent_palette_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "runtime.cli.main._run_inspect_palette",
        lambda: PaletteInspection(palette=None, entry_present=False, conformance=None),
    )
    result = runner.invoke(app, ["inspect", "palette"])
    assert result.exit_code == 0
    assert "no palette recorded" in result.output


def test_legacy_csg_palette_is_reported_na(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    entry = tmp_path / "csg"
    entry.mkdir()
    (entry / "colors.yaml").write_text('background: "#000000"\n', encoding="utf-8")
    (entry / "colors.gtk.css").write_text(
        "@define-color color_background #000000;\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        "runtime.cli.main._run_inspect_palette",
        lambda: PaletteInspection(
            palette="b" * 64,
            entry_present=True,
            conformance=check_palette_entry(entry),
        ),
    )
    result = runner.invoke(app, ["inspect", "palette"])
    assert result.exit_code == 0
    assert "n/a (legacy csg palette" in result.output
