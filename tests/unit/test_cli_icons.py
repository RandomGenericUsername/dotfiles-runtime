"""Unit tests for the ``icons regenerate`` / ``icons preference`` CLI (§2.3, §2.4).

Command-level rendering + exit codes (composition faked at the module
boundary), event emission around the real ``_run_icons_regenerate``
(use case faked), and the real ``_run_icons_preference`` accessor
(show/set/default-source, missing store OK, absent state loud).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from runtime.cli.main import app

runner = CliRunner()
WH = "f" * 64
PEH = "1a" * 32


def _now_z() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _regenerate_result(**overrides: Any) -> Any:
    from runtime.application.regenerate_icons import RegenerateIconsResult
    from runtime.domain.models import IconsArtifacts, IconsEntry

    icons = IconsEntry(
        hash_algorithm="sha256",
        kind="icons",
        entry_hash="e" * 64,
        source_palette_hash=PEH,
        input_templates_hash="c" * 64,
        input_mappings_hash="d" * 64,
        artifact_hashes=IconsArtifacts(**{"icon.svg": "b" * 64}),
        generated_at=_now_z(),
    )
    kwargs: dict[str, Any] = {
        "icons": icons,
        "cache_hit": False,
        "state": None,
        "repointed": [Path("/state/current/icons")],
        "consumer_symlinks": [],
        "reload_failures": [],
        "contrast_enabled": True,
        "contrast_source": "default",
    }
    kwargs.update(overrides)
    if kwargs["state"] is None:
        from runtime.domain.models import (
            BackendType,
            DesktopState,
            FitMode,
            MonitorWallpaperConfig,
            PaletteArtifacts,
            PaletteEntry,
            WallpaperEntry,
        )

        now = _now_z()
        kwargs["state"] = DesktopState(
            schema_version=2,
            wallpaper=WallpaperEntry(
                hash_algorithm="sha256",
                kind="wallpaper",
                content_hash=WH,
                source_path="/img/wall.png",
                imported_at=now,
            ),
            monitors={
                "DP-1": MonitorWallpaperConfig(
                    backend=BackendType.hyprpaper,
                    source_hash=WH,
                    fit_mode=FitMode.cover,
                    mpv_options=None,
                    ipc_socket=None,
                )
            },
            palette=PaletteEntry(
                hash_algorithm="sha256",
                kind="palette",
                entry_hash=PEH,
                source_wallpaper_hash=WH,
                input_template_hash="c" * 64,
                artifact_hashes=PaletteArtifacts(
                    colors_yaml="a" * 64,
                    colors_conf="b" * 64,
                    colors_gtk_css="d" * 64,
                    colors_adw_css="e" * 64,
                    colors_sequences="f" * 64,
                    colors_rasi="a" * 64,
                    colors_kitty="a" * 64,
                ),
                generated_at=now,
            ),
            effects=None,
            icons=icons,
            applied_at=now,
        )
    return RegenerateIconsResult(**kwargs)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point seeding + state resolution at tmp dirs (never the real home)."""
    monkeypatch.setenv("DOTFILES_INSTALL_SPINE", str(tmp_path / "install"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))


def _fake_regenerate(monkeypatch: pytest.MonkeyPatch, behavior: Any) -> None:
    def _run(*, contrast: str = "auto", client: Any = None) -> Any:
        return behavior(contrast=contrast, client=client)

    monkeypatch.setattr("runtime.cli.main._run_icons_regenerate", _run)


class TestIconsRegenerateCli:
    def test_success_renders_summary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_regenerate(monkeypatch, lambda contrast, client: _regenerate_result())
        result = runner.invoke(app, ["icons", "regenerate"])

        assert result.exit_code == 0
        assert "icons regenerated" in result.output
        assert "contrast guard ON" in result.output

    def test_contrast_off_rendered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_regenerate(
            monkeypatch,
            lambda contrast, client: _regenerate_result(
                contrast_enabled=False, contrast_source="flag"
            ),
        )
        result = runner.invoke(app, ["icons", "regenerate", "--contrast", "off"])

        assert result.exit_code == 0
        assert "contrast guard OFF" in result.output

    def test_json_format(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_regenerate(monkeypatch, lambda contrast, client: _regenerate_result())
        result = runner.invoke(app, ["icons", "regenerate", "--format", "json"])

        assert result.exit_code == 0
        obj = json.loads(result.output)
        assert obj["icons"] == "e" * 64
        assert obj["contrast_enabled"] is True
        assert obj["contrast_source"] == "default"
        assert obj["repointed"] == ["/state/current/icons"]
        assert obj["reload_failures"] == []

    def test_contrast_flag_threads_into_composition(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, Any] = {}

        def _run(*, contrast: str = "auto", client: Any = None) -> Any:
            seen["contrast"] = contrast
            return _regenerate_result()

        monkeypatch.setattr("runtime.cli.main._run_icons_regenerate", _run)
        result = runner.invoke(app, ["icons", "regenerate", "--contrast", "off"])

        assert result.exit_code == 0
        assert seen["contrast"] == "off"

    def test_invalid_flag_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = runner.invoke(app, ["icons", "regenerate", "--contrast", "x"])

        assert result.exit_code == 1
        assert "invalid contrast flag" in result.output

    def test_absent_state_maps_to_exit_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(*, contrast: str = "auto", client: Any = None) -> Any:
            raise RuntimeError("nothing to regenerate: no runtime state recorded yet")

        _fake_regenerate(monkeypatch, _boom)
        result = runner.invoke(app, ["icons", "regenerate"])

        assert result.exit_code == 1
        assert "nothing to regenerate" in result.output

    def test_reload_failure_maps_to_exit_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_regenerate(
            monkeypatch,
            lambda contrast, client: _regenerate_result(reload_failures=["AgsReloader"]),
        )
        result = runner.invoke(app, ["icons", "regenerate"])

        assert result.exit_code == 1
        assert "reload failed for: AgsReloader" in result.output


class TestIconsRegenerateEmission:
    """Events around the real composition root (use case faked)."""

    def _save_live_state(self, tmp_path: Path) -> None:
        from runtime.adapters.json_state_repository import JsonStateRepository

        state_root = tmp_path / "state-home" / "dotfiles"
        JsonStateRepository(state_root).save(_regenerate_result().state)

    def _fake_use_case(
        self, monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]
    ) -> None:
        from runtime.application.regenerate_icons import RegenerateIconsResult

        class _FakeUseCase:
            def __init__(self, **kwargs: Any) -> None:
                captured["kwargs"] = kwargs

            def run(self, *, contrast: str = "auto") -> RegenerateIconsResult:
                captured["contrast"] = contrast
                return _regenerate_result()

        monkeypatch.setattr(
            "runtime.application.regenerate_icons.RegenerateIconsUseCase", _FakeUseCase
        )

    def test_applying_then_done_never_visible(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import runtime.cli.main as cli_main

        self._save_live_state(tmp_path)
        captured: dict[str, Any] = {}
        self._fake_use_case(monkeypatch, captured)
        published: list[tuple[str, dict[str, object]]] = []

        class _Client:
            def publish(self, topic: str, payload: Any) -> None:
                published.append((topic, dict(payload)))

        result = cli_main._run_icons_regenerate(client=_Client())  # type: ignore[arg-type]

        assert published == [
            (
                "wallpaper.state",
                {"state": "applying", "wallpaper_hash": WH, "trigger": "regenerate"},
            ),
            (
                "wallpaper.state",
                {"state": "done", "wallpaper_hash": WH, "trigger": "regenerate"},
            ),
        ]
        assert captured["contrast"] == "auto"
        # AGS-only reload wiring: exactly one reloader, the AGS one.
        from runtime.adapters.ags_reloader import AgsReloader

        reloaders = captured["kwargs"]["reloaders"]
        assert [type(r) for r in reloaders] == [AgsReloader]
        assert result.contrast_enabled is True

    def test_absent_state_publishes_error_without_mutation(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import runtime.cli.main as cli_main

        captured: dict[str, Any] = {}
        self._fake_use_case(monkeypatch, captured)
        published: list[tuple[str, dict[str, object]]] = []

        class _Client:
            def publish(self, topic: str, payload: Any) -> None:
                published.append((topic, dict(payload)))

        with pytest.raises(RuntimeError, match="nothing to regenerate"):
            cli_main._run_icons_regenerate(client=_Client())  # type: ignore[arg-type]

        assert published == [
            (
                "wallpaper.state",
                {"state": "error", "wallpaper_hash": "", "trigger": "regenerate"},
            )
        ]
        assert "contrast" not in captured  # use case never ran

    def test_busy_second_command_fails_without_visible(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import runtime.cli.main as cli_main
        from runtime.adapters.flock_seed_mutex import FlockSeedMutex
        from runtime.domain.models import SeedLockedError

        self._save_live_state(tmp_path)
        captured: dict[str, Any] = {}
        self._fake_use_case(monkeypatch, captured)
        published: list[tuple[str, dict[str, object]]] = []

        class _Client:
            def publish(self, topic: str, payload: Any) -> None:
                published.append((topic, dict(payload)))

        state_root = tmp_path / "state-home" / "dotfiles"
        state_root.mkdir(parents=True, exist_ok=True)
        with FlockSeedMutex(state_root / ".seed.lock").hold(blocking=False):
            with pytest.raises(SeedLockedError, match="already in progress"):
                cli_main._run_icons_regenerate(client=_Client())  # type: ignore[arg-type]

        assert published == [
            (
                "wallpaper.state",
                {"state": "applying", "wallpaper_hash": WH, "trigger": "regenerate"},
            ),
            (
                "wallpaper.state",
                {"state": "error", "wallpaper_hash": "", "trigger": "regenerate"},
            ),
        ]


class TestIconsPreferenceAccessor:
    def test_show_default_on_without_store_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import runtime.cli.main as cli_main
        from runtime.adapters.hashing import hash_file

        img = tmp_path / "wall.png"
        img.write_bytes(b"live bytes")
        wallpaper_hash = hash_file(img)
        state_root = tmp_path / "state-home" / "dotfiles"
        from runtime.adapters.json_state_repository import JsonStateRepository

        saved = _regenerate_result().state
        import dataclasses

        saved = dataclasses.replace(
            saved,
            wallpaper=dataclasses.replace(
                saved.wallpaper, content_hash=wallpaper_hash, source_path=str(img)
            ),
        )
        JsonStateRepository(state_root).save(saved)

        resolved = cli_main._run_icons_preference(None, None)

        assert resolved == {"hash": wallpaper_hash, "enabled": True, "source": "default"}
        assert not (state_root / "icon-contrast.json").exists()

    def test_set_persists_then_reads_store(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import runtime.cli.main as cli_main

        first = cli_main._run_icons_preference(WH, "off")
        assert first == {"hash": WH, "enabled": False, "source": "store"}
        second = cli_main._run_icons_preference(WH, None)
        assert second == {"hash": WH, "enabled": False, "source": "store"}

    def test_explicit_hash_needs_no_state(self, tmp_path: Path) -> None:
        import runtime.cli.main as cli_main

        resolved = cli_main._run_icons_preference(WH, None)
        assert resolved == {"hash": WH, "enabled": True, "source": "default"}

    def test_invalid_hash_rejected(self, tmp_path: Path) -> None:
        import runtime.cli.main as cli_main

        with pytest.raises(ValueError, match="invalid wallpaper hash"):
            cli_main._run_icons_preference("nope", None)

    def test_invalid_set_rejected(self, tmp_path: Path) -> None:
        import runtime.cli.main as cli_main

        with pytest.raises(ValueError, match="invalid --set value"):
            cli_main._run_icons_preference(WH, "maybe")

    def test_default_hash_without_state_fails_loud(self, tmp_path: Path) -> None:
        import runtime.cli.main as cli_main

        with pytest.raises(RuntimeError, match="no runtime state"):
            cli_main._run_icons_preference(None, None)

    def test_cli_show_and_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = runner.invoke(app, ["icons", "preference", WH])
        assert result.exit_code == 0
        assert WH in result.output
        assert "on (source: default)" in result.output

        result = runner.invoke(app, ["icons", "preference", WH, "--set", "off"])
        assert result.exit_code == 0
        assert "off (source: store)" in result.output

        result = runner.invoke(app, ["icons", "preference", WH, "--format", "json"])
        assert result.exit_code == 0
        assert json.loads(result.output) == {"hash": WH, "enabled": False, "source": "store"}

    def test_cli_invalid_set_maps_to_exit_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = runner.invoke(app, ["icons", "preference", WH, "--set", "maybe"])
        assert result.exit_code == 1
        assert "invalid --set value" in result.output
