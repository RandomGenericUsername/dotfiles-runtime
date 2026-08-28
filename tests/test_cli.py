"""Stub CLI test exercising the version command via Typer CliRunner."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from runtime.cli.main import app

runner = CliRunner()


def test_version_command_returns_output() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.output.strip()


def test_version_command_with_json_format() -> None:
    result = runner.invoke(app, ["version", "--format", "json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert "version" in data
