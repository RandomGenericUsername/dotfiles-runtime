"""Unit tests for the ``reconcile --check-inputs`` read-only report (Story 1.3).

Covers: CLI shape (exit codes, plain/json rendering, absent-state exit 1,
seed guard), CheckInputsUseCase with fakes, and a real end-to-end (tmp
state + spine, template edit -> exact stale set) with an FS-snapshot
zero-mutation pin. Mirrors test_cli_inspect_status.py patterns — monkeypatch
the composition helper for shape tests, never the whole app.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

import runtime.cli.main as cli_main
from runtime.application.check_inputs import CheckInputsResult, CheckInputsUseCase
from runtime.cli.main import app
from runtime.domain.invalidation import DerivationLayer
from runtime.domain.models import DesktopState

runner = CliRunner()

PH = "aa" * 32
EH = "bb" * 32
IH = "cc" * 32
WH = "dd" * 32


@pytest.fixture(autouse=True)
def _quiet_seed_hook(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point first-run seeding at an empty spine so it skips quietly."""
    monkeypatch.setenv("DOTFILES_INSTALL_SPINE", str(tmp_path / "install"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))


def _check_result(
    stale: frozenset[DerivationLayer] | set[DerivationLayer] = frozenset(),
    fresh: frozenset[DerivationLayer] | set[DerivationLayer] = frozenset(
        {"palettes", "effects", "icons"}
    ),
) -> CheckInputsResult:
    return CheckInputsResult(stale=frozenset(stale), fresh=frozenset(fresh))


def _fake_composition(monkeypatch: pytest.MonkeyPatch, result: CheckInputsResult) -> None:
    def _run() -> CheckInputsResult:
        return result

    monkeypatch.setattr("runtime.cli.main._run_check_inputs", _run)


class TestCheckInputsCliShape:
    def test_success_exits_zero_and_renders_summary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_composition(monkeypatch, _check_result())
        result = runner.invoke(app, ["reconcile", "--check-inputs"])
        assert result.exit_code == 0
        assert "all layers fresh" in result.output

    def test_stale_layers_named_in_summary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_composition(
            monkeypatch, _check_result(stale={"palettes", "icons"}, fresh={"effects"})
        )
        result = runner.invoke(app, ["reconcile", "--check-inputs"])
        assert result.exit_code == 0
        assert "palettes" in result.output
        assert "icons" in result.output

    def test_json_object_shape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_composition(
            monkeypatch, _check_result(stale={"palettes", "icons"}, fresh={"effects"})
        )
        result = runner.invoke(app, ["reconcile", "--check-inputs", "--format", "json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["stale"] == ["icons", "palettes"]
        assert payload["fresh"] == ["effects"]

    def test_absent_state_exits_nonzero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise() -> CheckInputsResult:
            raise ValueError("no runtime state recorded yet")

        monkeypatch.setattr("runtime.cli.main._run_check_inputs", _raise)
        result = runner.invoke(app, ["reconcile", "--check-inputs"])
        assert result.exit_code == 1

    def test_empty_fresh_renders_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_composition(
            monkeypatch,
            _check_result(stale={"palettes", "effects", "icons"}, fresh=set()),
        )
        result = runner.invoke(app, ["reconcile", "--check-inputs"])
        assert result.exit_code == 0
        assert "none" in result.output


class _FakeStateRepo:
    def __init__(self, state: object) -> None:
        self._state = state

    def load_current(self) -> object:
        return self._state

    def save(self, state: object) -> None:
        raise AssertionError("read-only use case must never save")


class _FakeInvalidation:
    """Port double: scripted recompute, recorded map, real domain close."""

    def __init__(
        self,
        recomputed: dict[DerivationLayer, str | None],
        recorded: dict[DerivationLayer, str | None],
    ) -> None:
        self._recomputed = recomputed
        self._recorded = recorded
        self.recorded_calls: list[tuple[DerivationLayer, str]] = []

    def recompute_input_hashes(self) -> dict[DerivationLayer, str | None]:
        return dict(self._recomputed)

    def recorded_inputs(self, layer: DerivationLayer, entry_hash: str) -> str | None:
        self.recorded_calls.append((layer, entry_hash))
        return self._recorded.get(layer)

    def compare_against_meta(
        self,
        recorded: dict[DerivationLayer, str | None],
        recomputed: dict[DerivationLayer, str | None],
    ) -> frozenset[DerivationLayer]:
        from runtime.domain.invalidation import close_stale_set, diff_input_hashes

        return close_stale_set(diff_input_hashes(recorded, recomputed))

    def stale_set_with_cascade(
        self, directly_stale: frozenset[DerivationLayer] | set[DerivationLayer]
    ) -> frozenset[DerivationLayer]:
        from runtime.domain.invalidation import close_stale_set

        return close_stale_set(directly_stale)


def _desktop_state(
    palette_hash: str | None = PH, effects_hash: str | None = EH, icons_hash: str | None = IH
) -> DesktopState:
    from runtime.domain.models import (
        EffectsEntry,
        IconsEntry,
        PaletteEntry,
        WallpaperEntry,
    )

    return DesktopState(
        schema_version=2,
        wallpaper=WallpaperEntry(
            hash_algorithm="sha256",
            kind="wallpaper",
            content_hash=WH,
            source_path="/img/wall.png",
            imported_at="2026-09-10T00:00:00Z",
        ),
        monitors={},
        palette=(
            PaletteEntry(
                hash_algorithm="sha256",
                kind="palette",
                entry_hash=palette_hash,
                source_wallpaper_hash=WH,
                input_template_hash="t" * 64,
                artifact_hashes={
                    "colors.yaml": "e" * 64,
                    "colors.conf": "e" * 64,
                    "colors.gtk.css": "e" * 64,
                    "colors.adw.css": "e" * 64,
                    "colors.sequences": "e" * 64,
                    "colors.rasi": "e" * 64,
                },
                generated_at="2026-09-10T00:00:00Z",
            )
            if palette_hash
            else None
        ),
        effects=(
            EffectsEntry(
                hash_algorithm="sha256",
                kind="effects",
                entry_hash=effects_hash,
                source_wallpaper_hash=WH,
                input_catalog_hash="c" * 64,
                artifact_hashes={},
                generated_at="2026-09-10T00:00:00Z",
            )
            if effects_hash
            else None
        ),
        icons=(
            IconsEntry(
                hash_algorithm="sha256",
                kind="icons",
                entry_hash=icons_hash,
                source_palette_hash=palette_hash or "",
                input_templates_hash="i" * 64,
                input_mappings_hash="m" * 64,
                artifact_hashes={},
                generated_at="2026-09-10T00:00:00Z",
            )
            if icons_hash
            else None
        ),
        applied_at="2026-09-10T00:00:00Z",
    )


class TestCheckInputsUseCase:
    def test_absent_state_raises(self) -> None:
        use_case = CheckInputsUseCase(
            state_repo=_FakeStateRepo(None),  # type: ignore[arg-type]
            invalidation=_FakeInvalidation({}, {}),  # type: ignore[arg-type]
            recorded_inputs=lambda layer, entry_hash: None,
        )
        with pytest.raises(ValueError, match="no runtime state recorded"):
            use_case.run()

    def test_stale_and_fresh_split(self) -> None:
        state = _desktop_state()
        fake = _FakeInvalidation(
            recomputed={"palettes": "xx" * 32, "effects": "cc" * 32, "icons": "yy" * 32},
            recorded={},
        )
        calls: list[tuple[DerivationLayer, str]] = []

        def _reader(layer: DerivationLayer, entry_hash: str) -> str | None:
            calls.append((layer, entry_hash))
            return {
                "palettes": "bb" * 32,
                "effects": "cc" * 32,
                "icons": "dd" * 32,
            }[layer]

        use_case = CheckInputsUseCase(
            state_repo=_FakeStateRepo(state),  # type: ignore[arg-type]
            invalidation=fake,  # type: ignore[arg-type]
            recorded_inputs=_reader,
        )
        result = use_case.run()
        assert result.stale == frozenset({"palettes", "icons"})
        assert result.fresh == frozenset({"effects"})
        assert {layer for layer, _ in calls} == {"palettes", "effects", "icons"}

    def test_missing_layer_omitted_from_recorded(self) -> None:
        state = _desktop_state(effects_hash=None)
        seen: dict[str, str | None] = {}

        def _reader(layer: DerivationLayer, entry_hash: str) -> str | None:
            seen[layer] = entry_hash
            return "zz" * 32

        use_case = CheckInputsUseCase(
            state_repo=_FakeStateRepo(state),  # type: ignore[arg-type]
            invalidation=_FakeInvalidation(  # type: ignore[arg-type]
                recomputed={"palettes": "bb" * 32, "effects": "cc" * 32, "icons": "dd" * 32},
                recorded={},
            ),
            recorded_inputs=_reader,
        )
        result = use_case.run()
        assert "effects" not in seen
        assert result.stale == frozenset({"palettes", "effects", "icons"})
        assert result.fresh == frozenset()

    def test_all_layers_missing_reports_all_stale(self) -> None:
        state = _desktop_state(palette_hash=None, effects_hash=None, icons_hash=None)
        calls: list[tuple[DerivationLayer, str]] = []

        def _reader(layer: DerivationLayer, entry_hash: str) -> str | None:
            calls.append((layer, entry_hash))
            raise AssertionError("reader must not run with no active entries")

        use_case = CheckInputsUseCase(
            state_repo=_FakeStateRepo(state),  # type: ignore[arg-type]
            invalidation=_FakeInvalidation(  # type: ignore[arg-type]
                recomputed={"palettes": "aa" * 32, "effects": "bb" * 32, "icons": "cc" * 32},
                recorded={},
            ),
            recorded_inputs=_reader,
        )
        result = use_case.run()
        assert result.stale == frozenset({"palettes", "effects", "icons"})
        assert result.fresh == frozenset()
        assert calls == []


def _snapshot(root: Path) -> dict[str, str]:
    """Map every path under root to a state tag (FS-mutation pin).

    Records symlinks (targets), dirs, file modes/mtimes, and content hashes —
    repoints, empty-dir creation, and permission/mtime-only changes are visible.
    """
    snapshot: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        try:
            st = path.stat(follow_symlinks=False)
        except OSError:
            snapshot[rel] = "vanished"
            continue
        tag = f"{st.st_mode:o}:{st.st_mtime_ns}"
        if path.is_symlink():
            snapshot[rel] = f"link->{os.readlink(path)}:{tag}"
        elif path.is_file():
            snapshot[rel] = f"file:{hashlib.sha256(path.read_bytes()).hexdigest()}:{tag}"
        elif path.is_dir():
            snapshot[rel + "/"] = f"dir:{tag}"
    return snapshot


def _build_spine(install: Path) -> dict[str, Path]:
    templates = install / "config" / "color-scheme-generator" / "templates"
    templates.mkdir(parents=True)
    (templates / "a.j2").write_text("template-a", encoding="utf-8")
    catalog = install / "config" / "weg" / "effects.yaml"
    catalog.parent.mkdir(parents=True)
    catalog.write_text("effects: []", encoding="utf-8")
    icon_templates = install / "icon-templates"
    icon_templates.mkdir(parents=True)
    (icon_templates / "icon.svg").write_text("<svg/>", encoding="utf-8")
    icon_mappings = install / "icon-mappings" / "icons.yaml"
    icon_mappings.parent.mkdir(parents=True)
    icon_mappings.write_text("mappings: {}", encoding="utf-8")
    return {"templates": templates}


class TestCheckInputsEndToEnd:
    """Real wiring: tmp state + spine, no monkeypatched composition."""

    def _seed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> tuple[Path, Path, dict[str, Path]]:
        from runtime.adapters.hashing import canonical_hash_dir, hash_file
        from runtime.adapters.json_state_repository import JsonStateRepository
        from runtime.domain.models import (
            DesktopState,
            EffectsEntry,
            IconsEntry,
            PaletteEntry,
            WallpaperEntry,
        )

        install = tmp_path / "install"
        spine = _build_spine(install)
        state_root = tmp_path / "state-home" / "dotfiles"
        monkeypatch.setenv("DOTFILES_INSTALL_SPINE", str(install))
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))

        template_hash = canonical_hash_dir(spine["templates"])
        catalog_hash = hash_file(install / "config" / "weg" / "effects.yaml")
        icon_templates_hash = canonical_hash_dir(install / "icon-templates")
        icon_mappings_hash = hash_file(install / "icon-mappings" / "icons.yaml")

        for layer, entry_hash, fields in [
            (
                "palettes",
                PH,
                {"entry_hash": PH, "input_template_hash": template_hash},
            ),
            (
                "effects",
                EH,
                {"entry_hash": EH, "input_catalog_hash": catalog_hash},
            ),
            (
                "icons",
                IH,
                {
                    "entry_hash": IH,
                    "input_templates_hash": icon_templates_hash,
                    "input_mappings_hash": icon_mappings_hash,
                },
            ),
        ]:
            entry_dir = state_root / "cache" / layer / entry_hash
            entry_dir.mkdir(parents=True)
            (entry_dir / "meta.json").write_text(
                json.dumps(
                    {"hash_algorithm": "sha256", "kind": layer.rstrip("s"), **fields},
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            (entry_dir / "artifact.bin").write_bytes(b"artifact")

        JsonStateRepository(state_root=state_root).save(
            DesktopState(
                schema_version=2,
                wallpaper=WallpaperEntry(
                    hash_algorithm="sha256",
                    kind="wallpaper",
                    content_hash=WH,
                    source_path="/img/wall.png",
                    imported_at="2026-09-10T00:00:00Z",
                ),
                monitors={},
                palette=PaletteEntry(
                    hash_algorithm="sha256",
                    kind="palette",
                    entry_hash=PH,
                    source_wallpaper_hash=WH,
                    input_template_hash=template_hash,
                    artifact_hashes={
                        "colors.yaml": "e" * 64,
                        "colors.conf": "e" * 64,
                        "colors.gtk.css": "e" * 64,
                        "colors.adw.css": "e" * 64,
                        "colors.sequences": "e" * 64,
                        "colors.rasi": "e" * 64,
                    },
                    generated_at="2026-09-10T00:00:00Z",
                ),
                effects=EffectsEntry(
                    hash_algorithm="sha256",
                    kind="effects",
                    entry_hash=EH,
                    source_wallpaper_hash=WH,
                    input_catalog_hash=catalog_hash,
                    artifact_hashes={},
                    generated_at="2026-09-10T00:00:00Z",
                ),
                icons=IconsEntry(
                    hash_algorithm="sha256",
                    kind="icons",
                    entry_hash=IH,
                    source_palette_hash=PH,
                    input_templates_hash=icon_templates_hash,
                    input_mappings_hash=icon_mappings_hash,
                    artifact_hashes={},
                    generated_at="2026-09-10T00:00:00Z",
                ),
                applied_at="2026-09-10T00:00:00Z",
            )
        )
        return state_root, install, spine

    def test_warm_reports_all_fresh(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        state_root, _, _ = self._seed(monkeypatch, tmp_path)
        before = _snapshot(state_root)
        result = runner.invoke(app, ["reconcile", "--check-inputs"])
        assert result.exit_code == 0
        assert "all layers fresh" in result.output
        assert _snapshot(state_root) == before

    def test_template_edit_reports_palette_plus_cascade(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        state_root, _, spine = self._seed(monkeypatch, tmp_path)
        (spine["templates"] / "b.j2").write_text("template-b", encoding="utf-8")
        before = _snapshot(state_root)
        result = runner.invoke(app, ["reconcile", "--check-inputs", "--format", "json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["stale"] == ["icons", "palettes"]
        assert payload["fresh"] == ["effects"]
        assert _snapshot(state_root) == before

    def test_absent_state_never_seeds(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        state_root = tmp_path / "state-home" / "dotfiles"
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
        result = runner.invoke(app, ["reconcile", "--check-inputs"])
        assert result.exit_code == 1
        assert "no runtime state recorded" in (result.output + getattr(result, "stderr", ""))
        assert not (state_root / "current.json").exists()
        assert list(state_root.rglob("cache")) == []
        assert not (state_root / ".seed.lock").exists()
        assert list(state_root.glob("current.json.tmp.*")) == []


def test_wiring_contains_no_derivation_adapters() -> None:
    """Structural AC2 pin: the check composition wires no generators/locks/seeders."""
    import inspect

    source = inspect.getsource(cli_main._run_check_inputs)
    for name in ("CsgAdapter", "WegAdapter", "ItrAdapter", "CacheSeeder", "FlockSeedMutex"):
        assert name not in source


def test_reconcile_without_flag_still_converges(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The --check-inputs flag defaults off; plain reconcile takes the swap path."""
    check_calls: list[None] = []

    def _swap() -> object:
        raise RuntimeError("swap path taken")

    def _nope() -> CheckInputsResult:
        check_calls.append(None)
        raise AssertionError("check path must not run without the flag")

    monkeypatch.setattr(cli_main, "_run_reconcile", _swap)
    monkeypatch.setattr(cli_main, "_run_check_inputs", _nope)
    result = runner.invoke(app, ["reconcile"])
    assert result.exit_code == 1
    assert check_calls == []
