"""Integration tests for the ``wallpaper set`` end-to-end capstone (Story 2.7).

Composes the use cases DIRECTLY (never through the CLI in integration):
ApplyWallpaperUseCase → ReconcileDesktopStateUseCase(trigger="set") on a
REAL filesystem (real ``JsonStateRepository``/``CacheSeeder``/spine, fake
contract-honest csg/weg/itr with ``calls`` counters, fake mutex).

Covers the capstone ACs:
- FR-1: full pipeline — swap, reload, ``current.json``, one
  ``history.jsonl`` line with trigger ``"set"``, every reloader invoked.
- FR-2: repeat set on the same image = zero csg/weg/itr tool invocations
  in BOTH passes (counting ONLY the fake ``.calls`` counters —
  reconcile's ``hash_file`` read is a file read, not a tool invocation),
  while swap/history/reload still run.
- R5 → 2.2: a stray ``current/`` symlink from a mid-swap crash is
  reverted by ``_revert_stale_symlinks`` on the next set (pinned via the
  transient ``recovery: reverted`` INFO log, not just final state).
- R5: a failing reloader is surfaced in ``reload_failures`` (non-fatal).
- AD-18: per-monitor configs preserved, only ``source_hash`` updated.
- AR-9: golden-file guard pinning the exact history line + current.json
  emitted by the combined flow (timestamps normalized — the schema is
  otherwise byte-exact).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from runtime.adapters.hashing import hash_file
from runtime.adapters.json_state_repository import JsonStateRepository
from runtime.adapters.seeder import CacheSeeder
from runtime.application.reconcile import ReconcileDesktopStateUseCase
from runtime.domain.models import (
    BackendType,
    DesktopState,
    FitMode,
    MonitorWallpaperConfig,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
WALLPAPER_PNG = FIXTURES / "wallpaper.png"

_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z")


def _now_z() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class _FakeMutex:
    def hold(self, blocking: bool = False) -> object:
        class _Hold:
            def __enter__(self_inner) -> None:
                return None

            def __exit__(self_inner, *exc: object) -> None:
                pass

        return _Hold()


class _FakeCsg:
    def __init__(self) -> None:
        self.calls = 0

    def generate(self, wallpaper_path: Path, output_dir: Path) -> object:
        from runtime.domain.models import PaletteArtifacts, PaletteEntry

        self.calls += 1
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "colors.yaml").write_text("colors: []")
        (output_dir / "colors.conf").write_text("colors {}")
        (output_dir / "colors.gtk.css").write_text("colors {}")
        (output_dir / "colors.adw.css").write_text("colors {}")
        (output_dir / "colors.sequences").write_bytes(b"\x1b]4;0;#000\x1b\\")
        (output_dir / "colors.rasi").write_text("* { background: #000; }")
        return PaletteEntry(
            hash_algorithm="sha256",
            kind="palette",
            entry_hash=output_dir.name,
            source_wallpaper_hash=hash_file(wallpaper_path),
            input_template_hash="c" * 64,
            artifact_hashes=PaletteArtifacts(
                colors_yaml=hash_file(output_dir / "colors.yaml"),
                colors_conf=hash_file(output_dir / "colors.conf"),
                colors_gtk_css=hash_file(output_dir / "colors.gtk.css"),
                colors_adw_css=hash_file(output_dir / "colors.adw.css"),
                colors_sequences=hash_file(output_dir / "colors.sequences"),
                colors_rasi=hash_file(output_dir / "colors.rasi"),
            ),
            generated_at=_now_z(),
        )


class _FakeWeg:
    def __init__(self) -> None:
        self.calls = 0

    def generate(self, wallpaper_path: Path, output_dir: Path) -> object:
        from runtime.domain.models import EffectsArtifacts, EffectsEntry

        self.calls += 1
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "effect.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        effect_hash = hash_file(output_dir / "effect.png")
        return EffectsEntry(
            hash_algorithm="sha256",
            kind="effects",
            entry_hash=output_dir.name,
            source_wallpaper_hash=hash_file(wallpaper_path),
            input_catalog_hash="2" * 64,
            artifact_hashes=EffectsArtifacts(**{"effect.png": effect_hash}),
            generated_at=_now_z(),
        )


class _FakeItr:
    def __init__(self) -> None:
        self.calls = 0

    def render(
        self, palette_hash: str, templates_dir: Path, mappings_path: Path, output_dir: Path
    ) -> object:
        from runtime.domain.models import IconsArtifacts, IconsEntry

        self.calls += 1
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "icon.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
        return IconsEntry(
            hash_algorithm="sha256",
            kind="icons",
            entry_hash=output_dir.name,
            source_palette_hash=palette_hash,
            input_templates_hash="5" * 64,
            input_mappings_hash="6" * 64,
            artifact_hashes=IconsArtifacts(**{"icon.svg": hash_file(output_dir / "icon.svg")}),
            generated_at=_now_z(),
        )


class _RecordingReloader:
    """Passing reloader that records its name into a caller-owned list."""

    def __init__(self, name: str, invoked: list[str]) -> None:
        self._name = name
        self._invoked = invoked

    def reload(self) -> bool:
        self._invoked.append(self._name)
        return True


class _FailingReloader:
    """Reloader that reports failure (surfaced, non-fatal — R5)."""

    def reload(self) -> bool:
        return False


class _RaisingReloader:
    """Reloader that RAISES from reload() — the exception→reload_failures
    mapping in reconcile's loop (distinct from the return-False branch)."""

    def reload(self) -> bool:
        raise OSError("reload exploded")


class _HistoryProbeReloader:
    """Reloader that records the history.jsonl line count it observed at
    reload() time — pins AC1's "history BEFORE reload is considered complete"
    ordering (AR-3/AD-4)."""

    def __init__(self, state_root: Path, observed: list[int]) -> None:
        self._state_root = state_root
        self._observed = observed

    def reload(self) -> bool:
        history = self._state_root / "history.jsonl"
        lines = history.read_text().splitlines() if history.is_file() else []
        self._observed.append(len(lines))
        return True


def _setup_spine(install_spine: Path) -> None:
    csg_templates = install_spine / "config" / "color-scheme-generator" / "templates"
    csg_templates.mkdir(parents=True)
    (csg_templates / "default.yaml").write_text("window: {}\n")
    weg_config = install_spine / "config" / "weg"
    weg_config.mkdir(parents=True)
    (weg_config / "effects.yaml").write_text("effects: []\n")
    itr_templates = install_spine / "icon-templates"
    itr_templates.mkdir(parents=True)
    (itr_templates / "terminal.svg").write_text("<svg/>")
    (install_spine / "icon-mappings").mkdir(parents=True, exist_ok=True)
    (install_spine / "icon-mappings" / "icons.yaml").write_text("icons: {}\n")


@dataclass
class _Capstone:
    """Post-setup environment for the combined apply→reconcile flow."""

    repo: JsonStateRepository
    state_root: Path
    install_spine: Path
    img: Path
    csg: _FakeCsg
    weg: _FakeWeg
    itr: _FakeItr


def _setup(tmp_path: Path, *, img_bytes: bytes | None = None, fixture_img: bool = False) -> _Capstone:
    """Create spine + state_root + repo + contract-honest fake tools."""
    install_spine = tmp_path / "install"
    _setup_spine(install_spine)
    state_root = tmp_path / "state"
    repo = JsonStateRepository(state_root=state_root)
    if fixture_img:
        img = WALLPAPER_PNG
    else:
        img = tmp_path / "wall.png"
        img.write_bytes(img_bytes if img_bytes is not None else b"user wallpaper bytes")
    return _Capstone(
        repo=repo,
        state_root=state_root,
        install_spine=install_spine,
        img=img,
        csg=_FakeCsg(),
        weg=_FakeWeg(),
        itr=_FakeItr(),
    )


def _full_set(applied: _Capstone, reloaders: list[Any] | None = None, img: Path | None = None) -> Any:
    """One full ``wallpaper set``: apply → reconcile(trigger="set")."""
    from runtime.application.apply_wallpaper import ApplyWallpaperUseCase

    image = img if img is not None else applied.img
    ApplyWallpaperUseCase(
        state_repo=applied.repo,
        csg=applied.csg,
        weg=applied.weg,
        itr=applied.itr,
        install_spine=applied.install_spine,
        state_root=applied.state_root,
        seeder=CacheSeeder(applied.state_root),
        mutex=_FakeMutex(),
    ).run(image)
    return ReconcileDesktopStateUseCase(
        state_repo=applied.repo,
        csg=applied.csg,
        weg=applied.weg,
        itr=applied.itr,
        install_spine=applied.install_spine,
        state_root=applied.state_root,
        seeder=CacheSeeder(applied.state_root),
        mutex=_FakeMutex(),
        reloaders=reloaders if reloaders is not None else [],
    ).run(trigger="set")


def _symlink_map(current_dir: Path) -> dict[str, str]:
    import os

    return {p.name: str(Path(os.readlink(p))) for p in sorted(current_dir.iterdir())}


class TestCapstoneE2E:
    """FR-1: the combined flow swaps, reloads, and persists in one pass."""

    def test_full_pipeline_swaps_reloads_and_persists(self, tmp_path: Path) -> None:
        applied = _setup(tmp_path)
        invoked: list[str] = []
        reloaders = [_RecordingReloader(f"Reloader{i}", invoked) for i in range(4)]

        result = _full_set(applied, reloaders=reloaders)

        wh = applied.repo.load_current().wallpaper.content_hash  # type: ignore[union-attr]
        links = _symlink_map(applied.state_root / "current")
        assert set(links) == {
            "wallpaper-DP-1.png",
            "wallpaper.png",
            "colors.conf",
            "colors.gtk.css",
            "colors.yaml",
            "colors.adw.css",
            "colors.sequences",
            "colors.rasi",
            "effects",
            "icons",
        }
        for target in links.values():
            assert target.startswith(str(applied.state_root / "cache"))
        assert links["wallpaper-DP-1.png"] == (
            str(applied.state_root / "cache" / "wallpapers" / wh / "wallpaper.png")
        )
        # current.json written (schema v2, refreshed applied_at)
        saved_raw = json.loads((applied.state_root / "current.json").read_text())
        assert saved_raw["schema_version"] == 2
        assert saved_raw["wallpaper"]["hash"] == wh
        # history.jsonl gained exactly ONE line with trigger "set"
        lines = (applied.state_root / "history.jsonl").read_text().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["trigger"] == "set"
        # every reloader invoked exactly once
        assert sorted(invoked) == [
            "Reloader0",
            "Reloader1",
            "Reloader2",
            "Reloader3",
        ]
        assert result.reload_failures == []


class TestCapstoneHistoryOrdering:
    """AC 1 / AD-6 step 4: the history line is appended BEFORE any desktop
    reload is considered complete — every reloader must observe the new line
    already on disk (pinned via a reloader-side probe, not just final state)."""

    def test_every_reloader_observes_appended_history_line(self, tmp_path: Path) -> None:
        applied = _setup(tmp_path)
        observed: list[int] = []
        reloaders = [_HistoryProbeReloader(applied.state_root, observed) for _ in range(4)]

        _full_set(applied, reloaders=reloaders)

        # At the moment each reloader ran, history.jsonl already contained the
        # single "set" line for this run — reload must never precede the append.
        assert observed == [1, 1, 1, 1]
        result_sanity = (applied.state_root / "history.jsonl").read_text().splitlines()
        assert len(result_sanity) == 1


class TestCapstoneCacheHit:
    """FR-2: repeat set = zero tool invocations in BOTH passes."""

    def test_repeat_set_is_zero_tool_invocations_swap_still_runs(
        self, tmp_path: Path
    ) -> None:
        applied = _setup(tmp_path)
        invoked: list[str] = []
        reloaders = [_RecordingReloader(f"Reloader{i}", invoked) for i in range(4)]

        _full_set(applied, reloaders=reloaders)
        links_after_first = _symlink_map(applied.state_root / "current")
        first_history_line = (applied.state_root / "history.jsonl").read_text().splitlines()[0]
        applied.csg.calls = applied.weg.calls = applied.itr.calls = 0

        result = _full_set(applied, reloaders=reloaders)

        # zero csg/weg/itr invocations across apply AND reconcile passes
        assert (applied.csg.calls, applied.weg.calls, applied.itr.calls) == (0, 0, 0)
        # swap, history, and reload still ran
        assert result.repointed
        assert _symlink_map(applied.state_root / "current") == links_after_first
        lines = (applied.state_root / "history.jsonl").read_text().splitlines()
        assert len(lines) == 2
        assert all(json.loads(line)["trigger"] == "set" for line in lines)
        # AR-3 immutable history: the first line is byte-for-byte untouched
        # (a rewrite/truncation keeping count==2 would fail here).
        assert lines[0] == first_history_line
        assert sorted(invoked) == [
            "Reloader0",
            "Reloader0",
            "Reloader1",
            "Reloader1",
            "Reloader2",
            "Reloader2",
            "Reloader3",
            "Reloader3",
        ]


class TestCapstoneCrashRepair:
    """FR-3 / R5 → 2.2: stray symlink from a mid-swap crash is reverted."""

    def test_stray_symlink_reverted_on_next_set(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        applied = _setup(tmp_path)
        _full_set(applied)

        # Manufacture a mid-swap crash: stray colors.gtk.css repointed at
        # a wrong (old) cache target.
        loaded = applied.repo.load_current()
        assert loaded is not None
        stale = loaded.wallpaper.content_hash[:-1] + (
            "0" if loaded.wallpaper.content_hash[-1] != "0" else "1"
        )
        stale_entry = applied.state_root / "cache" / "wallpapers" / stale
        stale_entry.mkdir(parents=True, exist_ok=True)
        (stale_entry / "wallpaper.png").write_bytes(b"stale")
        (stale_entry / "meta.json").write_text(json.dumps({"hash_algorithm": "sha256"}))
        import os

        stray = applied.state_root / "current" / "colors.gtk.css"
        os.symlink(str(stale_entry / "wallpaper.png"), str(stray) + ".swap")
        os.replace(str(stray) + ".swap", str(stray))
        assert Path(os.readlink(stray)) == stale_entry / "wallpaper.png"

        with caplog.at_level(logging.INFO, logger="runtime.application.reconcile"):
            result = _full_set(applied)

        # Pin the TRANSIENT revert, not just the final state: the
        # reconcile-start recovery log is the direct observable that
        # _revert_stale_symlinks ran (a final-state-only assertion would
        # pass even with the revert disabled — the idempotent same-image
        # set masks it).
        reverted_logs = [r for r in caplog.records if "recovery: reverted" in r.message]
        assert reverted_logs, "reconcile-start stray-symlink revert did not run"
        assert "colors.gtk.css" in reverted_logs[0].getMessage()

        # Final state converged to the new last-good target too
        links = _symlink_map(applied.state_root / "current")
        loaded_after = applied.repo.load_current()
        assert loaded_after is not None and loaded_after.palette is not None
        pal_dir = applied.state_root / "cache" / "palettes" / loaded_after.palette.entry_hash
        assert links["colors.gtk.css"] == str(pal_dir / "colors.gtk.css")
        assert result.reload_failures == []


class TestCapstoneReloadFailure:
    """R5: a failing reloader is surfaced in reload_failures (non-fatal)."""

    def test_failing_reloader_surfaced_others_still_run(self, tmp_path: Path) -> None:
        applied = _setup(tmp_path)
        failing = _FailingReloader()
        invoked: list[str] = []
        reloaders: list[Any] = [
            _RecordingReloader("Reloader0", invoked),
            failing,
            _RecordingReloader("Reloader2", invoked),
        ]

        result = _full_set(applied, reloaders=reloaders)

        assert result.reload_failures == [_FailingReloader.__name__]
        # non-fatal: swap + history still completed
        assert result.repointed
        lines = (applied.state_root / "history.jsonl").read_text().splitlines()
        assert len(lines) == 1
        assert sorted(invoked) == ["Reloader0", "Reloader2"]

    def test_raising_reloader_surfaced_others_still_run(self, tmp_path: Path) -> None:
        """An exception from reload() lands in reload_failures too (the
        distinct exception→collect branch in reconcile's loop), and the
        combined flow still completes non-fatally."""
        applied = _setup(tmp_path)
        raising = _RaisingReloader()
        invoked: list[str] = []
        reloaders: list[Any] = [
            _RecordingReloader("Reloader0", invoked),
            raising,
            _RecordingReloader("Reloader2", invoked),
        ]

        result = _full_set(applied, reloaders=reloaders)

        assert result.reload_failures == [_RaisingReloader.__name__]
        assert result.repointed
        lines = (applied.state_root / "history.jsonl").read_text().splitlines()
        assert len(lines) == 1
        assert sorted(invoked) == ["Reloader0", "Reloader2"]


class TestCapstoneMonitorsPreserved:
    """AD-18: per-monitor configs preserved; only source_hash updates."""

    def test_per_monitor_configs_preserved_only_source_hash_moves(
        self, tmp_path: Path
    ) -> None:
        applied = _setup(tmp_path)
        reloaders: list[Any] = []
        _full_set(applied, reloaders=reloaders)

        # Seed non-default per-monitor configs (AD-18 Given)
        loaded = applied.repo.load_current()
        assert loaded is not None
        wh = loaded.wallpaper.content_hash
        monitors = {
            "DP-1": MonitorWallpaperConfig(
                backend=BackendType.swww,
                source_hash=wh,
                fit_mode=FitMode.contain,
                mpv_options=None,
                ipc_socket=None,
            ),
            "HDMI-1": MonitorWallpaperConfig(
                backend=BackendType.mpvpaper,
                source_hash=wh,
                fit_mode=FitMode.fill,
                mpv_options="--loop=inf",
                ipc_socket="/tmp/mpv-sock",
            ),
        }
        applied.repo.save(
            type(loaded)(
                schema_version=2,
                wallpaper=loaded.wallpaper,
                monitors=monitors,
                palette=loaded.palette,
                effects=loaded.effects,
                icons=loaded.icons,
                applied_at=loaded.applied_at,
            )
        )

        # Run a SECOND full set on a DIFFERENT image so source_hash must
        # actually move — the "only source_hash updated" claim is
        # unobservable if both runs share one image (new_hash == wh would
        # pass even with the refresh broken).
        other_img = tmp_path / "other.png"
        other_img.write_bytes(b"different wallpaper bytes")

        result = _full_set(applied, reloaders=reloaders, img=other_img)

        new_hash = result.state.wallpaper.content_hash
        assert new_hash != wh
        saved = applied.repo.load_current()
        assert saved is not None
        assert set(saved.monitors) == {"DP-1", "HDMI-1"}
        for name, cfg in saved.monitors.items():
            original = monitors[name]
            assert cfg.backend == original.backend, f"{name}: backend reset"
            assert cfg.fit_mode == original.fit_mode, f"{name}: fit_mode reset"
            assert cfg.mpv_options == original.mpv_options, f"{name}: mpv_options reset"
            assert cfg.ipc_socket == original.ipc_socket, f"{name}: ipc_socket reset"
            assert cfg.source_hash == new_hash, f"{name}: source_hash not updated"
            assert cfg.source_hash != wh
        # monitor symlinks follow the preserved monitor set
        links = _symlink_map(applied.state_root / "current")
        assert "wallpaper-DP-1.png" in links
        assert "wallpaper-HDMI-1.png" in links


class TestCapstoneGoldenSchema:
    """AR-9: the combined flow's history line + current.json are written
    EXACTLY (timestamps normalized — everything else is byte-exact).
    Fixed input: the checked-in ``tests/fixtures/wallpaper.png`` +
    fixed fake-tool artifacts + fixed spine templates."""

    GOLDEN_HISTORY = (
        '{"ts": "<TS>", "trigger": "set", '
        '"wallpaper": "619cd350283e11c3cc9ee7b3d67dce93738e87e7fefa16144840ebd716534381", '
        '"palette": "0a1470089dcedb08b07af5f8cb88c7e81f025ad6a8763e8cf31101d18aa01e70", '
        '"effects": "10e42516e247eb44f8c535eaa7b68c912aca34d8bb31b3ee8080652b157f0f48", '
        '"icons": "1e60f93c2e0ab87daa0bd7192f19d1fbe8f52a5cbfd9f0b875dc8c637e6db0c5", '
        '"source_path": "'
        + str(FIXTURES / "wallpaper.png")
        + '"}'
    )
    GOLDEN_CURRENT = """{
  "applied_at": "<TS>",
  "effects": {
    "generated_at": "<TS>",
    "hash": "10e42516e247eb44f8c535eaa7b68c912aca34d8bb31b3ee8080652b157f0f48"
  },
  "icons": {
    "generated_at": "<TS>",
    "hash": "1e60f93c2e0ab87daa0bd7192f19d1fbe8f52a5cbfd9f0b875dc8c637e6db0c5"
  },
  "monitors": {
    "DP-1": {
      "backend": "hyprpaper",
      "fit_mode": "cover",
      "ipc_socket": null,
      "mpv_options": null,
      "source_hash": "619cd350283e11c3cc9ee7b3d67dce93738e87e7fefa16144840ebd716534381"
    }
  },
  "palette": {
    "generated_at": "<TS>",
    "hash": "0a1470089dcedb08b07af5f8cb88c7e81f025ad6a8763e8cf31101d18aa01e70"
  },
  "schema_version": 2,
  "wallpaper": {
    "applied_at": "<TS>",
    "hash": "619cd350283e11c3cc9ee7b3d67dce93738e87e7fefa16144840ebd716534381",
    "source_path": \"""" + str(FIXTURES / "wallpaper.png") + """\"
  }
}
"""

    def test_history_and_current_json_are_byte_exact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        applied = _setup(tmp_path, fixture_img=True)
        _full_set(applied)

        history_text = (applied.state_root / "history.jsonl").read_text()
        current_text = (applied.state_root / "current.json").read_text()
        norm_history = _TS_RE.sub("<TS>", history_text).strip()
        norm_current = _TS_RE.sub("<TS>", current_text)

        assert norm_history == self.GOLDEN_HISTORY
        assert norm_current == self.GOLDEN_CURRENT


class _PassingHostReloader:
    """Isolation stub for HyprlandReloader/AgsReloader (no constructor args)."""

    def __init__(self, **_kwargs: object) -> None:
        pass

    def reload(self) -> bool:
        return True


class _PassingStatefulReloader:
    """Isolation stub for HyprpaperReloader/TerminalColorApplier (state_root kwarg)."""

    def __init__(self, **_kwargs: object) -> None:
        pass

    def reload(self) -> bool:
        return True


class _FailingTerminalColorApplier:
    """Isolation stub that fails like a surfaced TTY write failure (R5)."""

    def __init__(self, **_kwargs: object) -> None:
        pass

    def reload(self) -> bool:
        return False


class TestCapstoneCliExitCode:
    """The single real-composition ``wallpaper set`` CLI test — exit-code
    (R5) evidence through the real composition root. All four reloader
    classes are monkeypatched at their adapter modules (the composition
    root does ``from runtime.adapters.<x> import <Reloader>`` at call
    time, so patching the module attribute is sufficient — mirrors the
    test_cli_crash_recovery.py isolation pattern), otherwise the command
    would fire the live host's ``hyprctl``, ``ags``, and ``/dev/tty``."""

    def test_wallpaper_set_cli_success_then_reload_failure_exit_codes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from typer.testing import CliRunner

        from runtime.adapters.flock_seed_mutex import FlockSeedMutex
        from runtime.cli.main import app
        from runtime.application.apply_wallpaper import ApplyWallpaperUseCase

        # Prepare state with fake adapters so the real-adapter CLI pass is
        # a full cache hit (real csg/weg/itr never invoked).
        state_root = tmp_path / "dotfiles"  # XDG_STATE_HOME=str(tmp_path)
        install_spine = tmp_path / "install"
        _setup_spine(install_spine)
        repo = JsonStateRepository(state_root=state_root)
        img = tmp_path / "wall.png"
        img.write_bytes(b"cli capstone exit-code evidence")
        ApplyWallpaperUseCase(
            state_repo=repo,
            csg=_FakeCsg(),
            weg=_FakeWeg(),
            itr=_FakeItr(),
            install_spine=install_spine,
            state_root=state_root,
            seeder=CacheSeeder(state_root),
            mutex=FlockSeedMutex(state_root / ".seed.lock"),
        ).run(img)

        monkeypatch.setenv("DOTFILES_INSTALL_SPINE", str(install_spine))
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        monkeypatch.setattr(
            "runtime.adapters.hyprland_reloader.HyprlandReloader", _PassingHostReloader
        )
        monkeypatch.setattr("runtime.adapters.ags_reloader.AgsReloader", _PassingHostReloader)
        monkeypatch.setattr(
            "runtime.adapters.hyprpaper_reloader.HyprpaperReloader", _PassingStatefulReloader
        )
        monkeypatch.setattr(
            "runtime.adapters.terminal_color_applier.TerminalColorApplier",
            _PassingStatefulReloader,
        )

        # Happy path through the REAL composition root: all four reloaders
        # pass → exit 0 with the combined success render.
        ok = CliRunner().invoke(app, ["wallpaper", "set", str(img)])
        assert ok.exit_code == 0, ok.output
        assert "wallpaper applied:" in ok.output
        assert "symlink(s) repointed" in ok.output
        assert "ReloadError" not in ok.output

        # Now force a reload failure on the terminal consumer → R5 exit 1.
        monkeypatch.setattr(
            "runtime.adapters.terminal_color_applier.TerminalColorApplier",
            _FailingTerminalColorApplier,
        )

        failed = CliRunner().invoke(app, ["wallpaper", "set", str(img)])

        assert failed.exit_code == 1
        assert "ReloadError" in failed.output
        assert "reload failed for: _FailingTerminalColorApplier" in failed.output

