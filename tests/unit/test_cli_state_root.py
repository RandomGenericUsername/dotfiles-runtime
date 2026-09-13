"""Multi-session ``state_root`` scoping (P5 follow-up).

Two concurrent graphical sessions of one user must not share one
``state_root``; with no session identifier the historical single-session path
must be returned byte-identically. Resolution is exercised through
``_resolve_state_root`` and its env helpers (no live session needed).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import runtime.cli.main as cli_main

_SESSION_ENV = (
    "DOTFILES_SESSION_ID",
    "DOTFILES_SESSION_SCOPE",
    "XDG_SESSION_ID",
    "XDG_RUNTIME_DIR",
)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    for key in _SESSION_ENV:
        monkeypatch.delenv(key, raising=False)


def _base(tmp_path: Path) -> Path:
    return (tmp_path / "state-home" / "dotfiles").resolve()


class TestDefaultResolution:
    def test_default_is_byte_identical_single_session_path(self, tmp_path: Path) -> None:
        assert cli_main._resolve_state_root() == _base(tmp_path)

    def test_no_session_env_resolves_same_as_before(self, tmp_path: Path) -> None:
        """The default must stay exactly ``$XDG_STATE_HOME/dotfiles``."""
        assert cli_main._resolve_state_root("") == _base(tmp_path)
        assert cli_main._resolve_state_root(None) == _base(tmp_path)


class TestExplicitSession:
    def test_explicit_env_scopes_under_sessions(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("DOTFILES_SESSION_ID", "seat-a")
        assert cli_main._resolve_state_root() == _base(tmp_path) / "sessions" / "seat-a"

    def test_explicit_parameter_scopes(self, tmp_path: Path) -> None:
        assert cli_main._resolve_state_root("seat-b") == _base(tmp_path) / "sessions" / "seat-b"

    def test_allowed_characters_preserved(self, tmp_path: Path) -> None:
        assert cli_main._resolve_state_root("Seat_1.2-3") == (
            _base(tmp_path) / "sessions" / "Seat_1.2-3"
        )

    @pytest.mark.parametrize("hostile", ["../evil", "a/b", "..", ".", "with space", "x\x00y"])
    def test_unsafe_identifier_falls_back_to_unscoped(
        self, tmp_path: Path, hostile: str
    ) -> None:
        """A hostile/invalid id can never escape the sessions directory."""
        assert cli_main._resolve_state_root(hostile) == _base(tmp_path)

    def test_unsafe_env_falls_back_to_unscoped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("DOTFILES_SESSION_ID", "../escape")
        assert cli_main._resolve_state_root() == _base(tmp_path)


class TestAutoScopeOptIn:
    def test_scope_opt_in_uses_xdg_session_id(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("DOTFILES_SESSION_SCOPE", "1")
        monkeypatch.setenv("XDG_SESSION_ID", "7")
        assert cli_main._resolve_state_root() == _base(tmp_path) / "sessions" / "7"

    def test_scope_opt_in_falls_back_to_runtime_dir_basename(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("DOTFILES_SESSION_SCOPE", "true")
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
        assert cli_main._resolve_state_root() == _base(tmp_path) / "sessions" / "1000"

    def test_scope_disabled_ignores_xdg_identifiers(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("XDG_SESSION_ID", "7")
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
        assert cli_main._resolve_state_root() == _base(tmp_path)

    def test_explicit_id_wins_over_scope(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("DOTFILES_SESSION_ID", "explicit")
        monkeypatch.setenv("DOTFILES_SESSION_SCOPE", "1")
        monkeypatch.setenv("XDG_SESSION_ID", "7")
        assert cli_main._resolve_state_root() == _base(tmp_path) / "sessions" / "explicit"


class TestIndependentSessions:
    def test_two_sessions_do_not_collide(self, tmp_path: Path) -> None:
        a = cli_main._resolve_state_root("session-a")
        b = cli_main._resolve_state_root("session-b")
        assert a != b
        assert a.parent == b.parent == _base(tmp_path) / "sessions"
