"""``build_env`` allowlist: THEME__ keys pass, secrets still lose."""

from __future__ import annotations

import pytest

from runtime.adapters.env import build_env

_SECRET_KEYS = (
    "GITHUB_TOKEN",
    "AWS_SECRET_ACCESS_KEY",
    "THEME__OUTPUT__PASSWORD",
    "COLORSCHEME__API_SECRET",
)


def test_theme_prefix_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("THEME__OUTPUT__DIRECTORY", "/tmp/out")
    monkeypatch.setenv("THEME__GENERATION__ENGINE", "native")
    monkeypatch.setenv("THEME__RUNTIME__MODE", "local")

    env = build_env({})

    assert env["THEME__OUTPUT__DIRECTORY"] == "/tmp/out"
    assert env["THEME__GENERATION__ENGINE"] == "native"
    assert env["THEME__RUNTIME__MODE"] == "local"


def test_theme_overrides_are_injected(monkeypatch: pytest.MonkeyPatch) -> None:
    env = build_env(
        {
            "THEME__OUTPUT__DIRECTORY": "/tmp/palette",
            "THEME__OUTPUT__OVERWRITE": "true",
        }
    )
    assert env["THEME__OUTPUT__DIRECTORY"] == "/tmp/palette"
    assert env["THEME__OUTPUT__OVERWRITE"] == "true"


def test_secrets_are_blocked_even_with_allowed_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _SECRET_KEYS:
        monkeypatch.setenv(key, "super-secret")

    env = build_env({})

    for key in _SECRET_KEYS:
        assert key not in env


def test_unrelated_host_keys_stay_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOME_RANDOM_HOST_VAR", "leak")
    assert "SOME_RANDOM_HOST_VAR" not in build_env({})
