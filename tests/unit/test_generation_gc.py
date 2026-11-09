"""Generation GC current-reference protection (BUG-014).

The live failure: converge committed generation ``43d62e17e2da`` and the
same converge's GC swept that generation's palette, wallpaper and composed
appearance cache layers. Manifest layer keys are singular while cache
directories are plural, so only effects/icons were protected. These tests
pin the post-commit protection set and the commit → protect → prune ordering.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path

import pytest

from runtime.adapters.converge_lock import ConvergeLock
from runtime.adapters.generation_store import (
    GenerationSpec,
    GenerationStore,
    generation_intent_hash,
)
from runtime.adapters.hashing import appearance_generation_hash
from runtime.adapters.json_state_repository import JsonStateRepository
from runtime.adapters.seeder import CacheSeeder
from runtime.application.doctor import DoctorUseCase
from runtime.application.reconcile import default_skin_entry
from runtime.domain.ags_apps import AppliedAppearance, AppRefreshOutcome
from runtime.domain.models import (
    BackendType,
    DesktopState,
    EffectsEntry,
    FitMode,
    IconsEntry,
    MonitorWallpaperConfig,
    PaletteArtifacts,
    PaletteEntry,
    WallpaperEntry,
)
from tests.support.palette_fakes import v1_colors_yaml, v1_gtk_css

# Live BUG-014 values (the committed generation on the host).
LIVE_WALLPAPER_HASH = "fb1c5be63e002f6902f66be8b97744bd29d4224815291a4ad8d0115a23cde48c"
LIVE_PALETTE_HASH = "0797efd74520459ec24c15a9f7abbbcb62f34a506f7fe371bda3bfb7cabc8363"
LIVE_EFFECTS_HASH = "1a3d8368a4a747e07bd3eb76a26ab891e8797abc91aa6e8f761ea7956a90f9f8"
LIVE_ICONS_HASH = "e53912ea1f20835c08911d6ca3ade9d950a944c75852166df5f848b9a2dbfe6a"
LIVE_MODE = "dark"
TS = "2026-11-07T23:03:04.578570Z"

PALETTE_FILES = (
    "colors.conf",
    "colors.gtk.css",
    "colors.yaml",
    "colors.adw.css",
    "colors.sequences",
    "colors.rasi",
    "colors.kitty",
    "colors.btop.theme",
)
_PALETTE_ARTIFACT_KEYS = {
    "colors.yaml": "colors_yaml",
    "colors.conf": "colors_conf",
    "colors.gtk.css": "colors_gtk_css",
    "colors.adw.css": "colors_adw_css",
    "colors.sequences": "colors_sequences",
    "colors.rasi": "colors_rasi",
    "colors.kitty": "colors_kitty",
    "colors.btop.theme": "colors_btop",
}


def _h(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_meta(entry_dir: Path, fields: dict[str, object]) -> None:
    entry_dir.mkdir(parents=True, exist_ok=True)
    payload = {"hash_algorithm": "sha256", **fields}
    (entry_dir / "meta.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )


def _write_wallpaper(state_root: Path, wallpaper_hash: str) -> WallpaperEntry:
    entry_dir = state_root / "cache" / "wallpapers" / wallpaper_hash
    entry_dir.mkdir(parents=True, exist_ok=True)
    data = b"live wallpaper bytes"
    (entry_dir / "wallpaper.png").write_bytes(data)
    _write_meta(
        entry_dir,
        {
            "kind": "wallpaper",
            "content_hash": wallpaper_hash,
            "source_path": "/img/live.png",
            "imported_at": TS,
        },
    )
    return WallpaperEntry(
        hash_algorithm="sha256",
        kind="wallpaper",
        content_hash=wallpaper_hash,
        source_path="/img/live.png",
        imported_at=TS,
    )


def _write_palette(
    state_root: Path, palette_hash: str, wallpaper_hash: str
) -> PaletteEntry:
    entry_dir = state_root / "cache" / "palettes" / palette_hash
    artifact_bytes = {name: name.encode() for name in PALETTE_FILES}
    artifact_bytes["colors.yaml"] = v1_colors_yaml().encode()
    artifact_bytes["colors.gtk.css"] = v1_gtk_css().encode()
    entry_dir.mkdir(parents=True, exist_ok=True)
    artifact_hashes: dict[str, str] = {}
    for name, data in artifact_bytes.items():
        (entry_dir / name).write_bytes(data)
        artifact_hashes[name] = _h(data)
    _write_meta(
        entry_dir,
        {
            "kind": "palette",
            "entry_hash": palette_hash,
            "source_wallpaper_hash": wallpaper_hash,
            "input_template_hash": "t" * 64,
            "artifact_hashes": artifact_hashes,
            "generated_at": TS,
        },
    )
    by_key = {
        _PALETTE_ARTIFACT_KEYS[name]: digest
        for name, digest in artifact_hashes.items()
    }
    return PaletteEntry(
        hash_algorithm="sha256",
        kind="palette",
        entry_hash=palette_hash,
        source_wallpaper_hash=wallpaper_hash,
        input_template_hash="t" * 64,
        artifact_hashes=PaletteArtifacts(**by_key),
        generated_at=TS,
    )


def _write_effects(state_root: Path, effects_hash: str, wallpaper_hash: str) -> EffectsEntry:
    entry_dir = state_root / "cache" / "effects" / effects_hash
    entry_dir.mkdir(parents=True, exist_ok=True)
    (entry_dir / "fx.png").write_bytes(b"fx")
    _write_meta(
        entry_dir,
        {
            "kind": "effects",
            "entry_hash": effects_hash,
            "source_wallpaper_hash": wallpaper_hash,
            "input_catalog_hash": "c" * 64,
            "artifact_hashes": {"fx.png": _h(b"fx")},
            "generated_at": TS,
        },
    )
    return EffectsEntry(
        hash_algorithm="sha256",
        kind="effects",
        entry_hash=effects_hash,
        source_wallpaper_hash=wallpaper_hash,
        input_catalog_hash="c" * 64,
        artifact_hashes={"fx.png": _h(b"fx")},
        generated_at=TS,
    )


def _write_icons(state_root: Path, icons_hash: str, palette_hash: str) -> IconsEntry:
    entry_dir = state_root / "cache" / "icons" / icons_hash
    entry_dir.mkdir(parents=True, exist_ok=True)
    (entry_dir / "icon.svg").write_bytes(b"<svg/>")
    _write_meta(
        entry_dir,
        {
            "kind": "icons",
            "entry_hash": icons_hash,
            "source_palette_hash": palette_hash,
            "input_templates_hash": "i" * 64,
            "input_mappings_hash": "m" * 64,
            "artifact_hashes": {"icon.svg": _h(b"<svg/>")},
            "generated_at": TS,
        },
    )
    return IconsEntry(
        hash_algorithm="sha256",
        kind="icons",
        entry_hash=icons_hash,
        source_palette_hash=palette_hash,
        input_templates_hash="i" * 64,
        input_mappings_hash="m" * 64,
        artifact_hashes={"icon.svg": _h(b"<svg/>")},
        generated_at=TS,
    )


def _commit_generation(
    state_root: Path,
    *,
    wallpaper_hash: str,
    palette_hash: str,
    effects_hash: str,
    icons_hash: str,
    mode: str = LIVE_MODE,
) -> tuple[str, str]:
    """Commit one generation from real cache entries; return (gen, appearance)."""
    from runtime.adapters.appearance import AppearanceGenerator

    wallpaper = _write_wallpaper(state_root, wallpaper_hash)
    palette = _write_palette(state_root, palette_hash, wallpaper_hash)
    effects = _write_effects(state_root, effects_hash, wallpaper_hash)
    icons = _write_icons(state_root, icons_hash, palette_hash)
    skin = default_skin_entry(state_root, CacheSeeder(state_root))
    state = DesktopState(
        schema_version=2,
        wallpaper=wallpaper,
        monitors={
            "DP-1": MonitorWallpaperConfig(
                backend=BackendType.hyprpaper,
                source_hash=wallpaper_hash,
                fit_mode=FitMode.cover,
                mpv_options=None,
                ipc_socket=None,
            )
        },
        palette=palette,
        effects=effects,
        icons=icons,
        applied_at=TS,
        skin=skin,
    )
    JsonStateRepository(state_root=state_root).save(state)
    appearance = AppearanceGenerator(state_root).ensure(
        palette_entry=palette,
        skin_entry=skin,
        install_spine=state_root.parent / "install",
    )[0]
    generation = GenerationStore(state_root).commit(
        GenerationSpec(
            state=state,
            appearance=appearance,
            mode=mode,
            skin_name=skin.name,
            skin_hash=skin.entry_hash,
            intent_hash=generation_intent_hash(
                wallpaper=wallpaper_hash,
                palette=palette_hash,
                skin=skin.name,
                mode=mode,
                effects=effects_hash,
                icons=icons_hash,
            ),
            monitor_names=("DP-1",),
            source_kind="cli",
        )
    )
    CacheSeeder(state_root).append_history(
        trigger="set",
        wallpaper_hash=wallpaper_hash,
        palette_hash=palette_hash,
        effects_hash=effects_hash,
        icons_hash=icons_hash,
        skin_hash=skin.entry_hash,
        source_path=wallpaper.source_path,
    )
    return generation.generation_hash, appearance.appearance_hash


def _seed_live_state(state_root: Path) -> tuple[str, str]:
    return _commit_generation(
        state_root,
        wallpaper_hash=LIVE_WALLPAPER_HASH,
        palette_hash=LIVE_PALETTE_HASH,
        effects_hash=LIVE_EFFECTS_HASH,
        icons_hash=LIVE_ICONS_HASH,
    )


def _old_entry(state_root: Path, layer: str, entry_hash: str) -> Path:
    entry = state_root / "cache" / layer / entry_hash
    entry.mkdir(parents=True)
    os.utime(entry, (1, 1))
    return entry


def _doctor(state_root: Path) -> DoctorUseCase:
    return DoctorUseCase(
        state_repo=JsonStateRepository(state_root=state_root),
        state_root=state_root,
    )


def test_gc_protects_every_current_generation_layer_live_values(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    generation_hash, appearance_hash = _seed_live_state(state_root)
    store = GenerationStore(state_root)
    manifest = store.verify(generation_hash)
    assert manifest["layers"]["palette"]["entryHash"] == LIVE_PALETTE_HASH
    assert manifest["layers"]["wallpaper"]["entryHash"] == LIVE_WALLPAPER_HASH
    assert (
        appearance_hash
        == appearance_generation_hash(LIVE_PALETTE_HASH, manifest["skin"]["entryHash"], LIVE_MODE)
    )

    old_entries = {
        layer: _old_entry(state_root, layer, "ab" * 32)
        for layer in ("wallpapers", "palettes", "effects", "icons", "appearance")
    }

    swept = store.gc(
        keep=0,
        grace_seconds=0,
        acknowledged_generations=[generation_hash],
        current_generation=generation_hash,
    )

    # Every cache layer the committed/current generation needs survives.
    assert (state_root / "cache" / "wallpapers" / LIVE_WALLPAPER_HASH).is_dir()
    assert (state_root / "cache" / "palettes" / LIVE_PALETTE_HASH).is_dir()
    assert (state_root / "cache" / "effects" / LIVE_EFFECTS_HASH).is_dir()
    assert (state_root / "cache" / "icons" / LIVE_ICONS_HASH).is_dir()
    assert (state_root / "cache" / "appearance" / appearance_hash).is_dir()
    for rel in (
        "palette/colors.yaml",
        "palette/colors.gtk.css",
        "wallpaper/wallpaper.png",
        "effects",
        "icons",
        "appearance/tokens.json",
        "appearance/ags.css",
    ):
        assert (state_root / "current" / rel).exists(), rel
    # The old unreferenced entries are still pruned.
    for layer, entry in old_entries.items():
        assert not entry.exists(), layer
    assert set(swept["cache"]) == {
        f"{layer}/{'ab' * 32}"
        for layer in ("wallpapers", "palettes", "effects", "icons", "appearance")
    }

    report = _doctor(state_root).check()
    assert report.clean, [item for item in report.items if item.status != "ok"]


def test_gc_protects_pinned_generation_layers(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    first_hash, first_appearance = _seed_live_state(state_root)
    store = GenerationStore(state_root)
    store.add_pin(first_hash, "operator rollback target")

    second_hash, _ = _commit_generation(
        state_root,
        wallpaper_hash=LIVE_WALLPAPER_HASH,
        palette_hash="ef" * 32,
        effects_hash=LIVE_EFFECTS_HASH,
        icons_hash=LIVE_ICONS_HASH,
    )
    assert second_hash != first_hash
    assert store.current_hash() == second_hash

    old = _old_entry(state_root, "palettes", "cd" * 32)
    swept = store.gc(
        keep=0,
        grace_seconds=0,
        acknowledged_generations=[second_hash],
        current_generation=second_hash,
    )

    assert first_hash not in swept["generations"]
    assert store.load_manifest(first_hash) is not None
    assert (state_root / "cache" / "palettes" / LIVE_PALETTE_HASH).is_dir()
    assert (state_root / "cache" / "appearance" / first_appearance).is_dir()
    assert not old.exists()


def test_gc_protects_ledger_referenced_appearance_layer(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    generation_hash, _ = _seed_live_state(state_root)
    store = GenerationStore(state_root)
    ledger_appearance = "ab" * 32
    ledger_dir = _old_entry(state_root, "appearance", ledger_appearance)
    (ledger_dir / "tokens.json").write_text("{}", encoding="utf-8")

    store.gc(
        keep=0,
        grace_seconds=0,
        ledger_layers={"appearance": [ledger_appearance]},
        acknowledged_generations=[generation_hash],
        current_generation=generation_hash,
    )

    assert ledger_dir.is_dir()
    assert not (state_root / "cache" / "appearance" / ("cd" * 32)).exists()


def test_gc_protects_ledger_referenced_generation_layers(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    ledger_generation, ledger_appearance = _seed_live_state(state_root)
    store = GenerationStore(state_root)
    current_generation, _ = _commit_generation(
        state_root,
        wallpaper_hash=LIVE_WALLPAPER_HASH,
        palette_hash="ef" * 32,
        effects_hash=LIVE_EFFECTS_HASH,
        icons_hash=LIVE_ICONS_HASH,
    )

    store.gc(
        keep=0,
        grace_seconds=0,
        ledger_generations=[ledger_generation],
        acknowledged_generations=[current_generation],
        current_generation=current_generation,
    )

    assert store.load_manifest(ledger_generation) is not None
    assert (state_root / "cache" / "palettes" / LIVE_PALETTE_HASH).is_dir()
    assert (state_root / "cache" / "appearance" / ledger_appearance).is_dir()


def test_gc_protects_newest_entry_per_layer(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    generation_hash, _ = _seed_live_state(state_root)
    store = GenerationStore(state_root)
    oldest = _old_entry(state_root, "effects", "ab" * 32)
    newest = state_root / "cache" / "effects" / ("cd" * 32)
    newest.mkdir()

    store.gc(
        keep=0,
        grace_seconds=0,
        acknowledged_generations=[generation_hash],
        current_generation=generation_hash,
    )

    assert not oldest.exists()
    assert newest.is_dir()
    assert (state_root / "cache" / "effects" / LIVE_EFFECTS_HASH).is_dir()


def test_gc_fails_closed_when_current_manifest_is_invalid(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    generation_hash, _ = _seed_live_state(state_root)
    store = GenerationStore(state_root)
    manifest_path = store.generation_path(generation_hash) / "manifest.json"
    manifest_path.write_text("{not json", encoding="utf-8")
    old_palette = _old_entry(state_root, "palettes", "ab" * 32)

    store.gc(
        keep=0,
        grace_seconds=0,
        acknowledged_generations=[generation_hash],
        current_generation=generation_hash,
    )

    assert old_palette.is_dir()
    assert (state_root / "cache" / "palettes" / LIVE_PALETTE_HASH).is_dir()


class _RecordingStore(GenerationStore):
    """Records commit/gc order and probes the convergence lock from GC."""

    def __init__(self, state_root: Path) -> None:
        super().__init__(state_root)
        self.events: list[tuple[str, str | None, str | None]] = []
        self.probe_acquired = threading.Event()

    def commit(self, spec: GenerationSpec) -> object:
        result = super().commit(spec)
        self.events.append(("commit", result.generation_hash, None))
        return result

    def gc(self, **kwargs: object) -> dict[str, list[str]]:
        current_generation = kwargs.get("current_generation")
        assert isinstance(current_generation, str) or current_generation is None
        self.events.append(("gc", current_generation, self.current_hash()))
        lock = ConvergeLock(self._state_root)

        def probe() -> None:
            with lock.hold():
                self.probe_acquired.set()

        thread = threading.Thread(target=probe, daemon=True)
        thread.start()
        # GC runs inside the held converge.lock, so a second converge blocks.
        assert not self.probe_acquired.wait(0.2)
        self._probe_thread = thread
        return super().gc(**kwargs)

    def finish(self) -> None:
        thread = getattr(self, "_probe_thread", None)
        if thread is not None:
            thread.join(timeout=5)


def test_gc_runs_after_commit_under_the_converge_lock(tmp_path: Path) -> None:
    from tests.unit.test_reconcile import _apply_state, _make_reconcile

    applied = _apply_state(tmp_path)
    store = _RecordingStore(applied.state_root)
    use_case = _make_reconcile(applied)
    use_case._generation_store = store  # type: ignore[attr-defined]

    result = use_case.run(trigger="reconcile")
    store.finish()

    assert [event[0] for event in store.events] == ["commit", "gc"]
    committed = store.events[0][1]
    assert result.generation_hash == committed
    assert store.events[1][1] == committed == store.current_hash()
    assert store.probe_acquired.is_set()
    for layer, entry_hash in (
        ("wallpapers", result.state.wallpaper.content_hash),
        ("palettes", result.state.palette.entry_hash),
        ("effects", result.state.effects.entry_hash),
        ("icons", result.state.icons.entry_hash),
    ):
        assert (applied.state_root / "cache" / layer / entry_hash).is_dir()


class _FakeLedger:
    def __init__(self, appearance_hash: str) -> None:
        self._appearance_hash = appearance_hash

    def load(self) -> AppliedAppearance:
        return AppliedAppearance(
            version=1,
            appearance_hash=self._appearance_hash,
            operation_id="op",
            completed_at=TS,
            apps={},
        )


class _FakeRefresher:
    def __init__(self, appearance_hash: str) -> None:
        self.ledger = _FakeLedger(appearance_hash)

    def refresh(
        self,
        state: DesktopState,
        *,
        generation_hash: str,
        trigger: str = "reconcile",
    ) -> AppRefreshOutcome:
        return AppRefreshOutcome(appearance_hash=generation_hash, run_id="run")


def test_reconcile_gc_protects_the_previous_ledger_appearance_layer(
    tmp_path: Path,
) -> None:
    from tests.unit.test_reconcile import _apply_state, _make_reconcile

    applied = _apply_state(tmp_path)
    ledger_appearance = "ab" * 32
    ledger_dir = _old_entry(applied.state_root, "appearance", ledger_appearance)
    (ledger_dir / "tokens.json").write_text("{}", encoding="utf-8")
    use_case = _make_reconcile(applied)
    use_case._app_refresher = _FakeRefresher(ledger_appearance)  # type: ignore[attr-defined]

    use_case.run(trigger="reconcile")

    assert ledger_dir.is_dir()
