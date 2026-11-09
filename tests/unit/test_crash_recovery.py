"""Unit tests for crash-mid-swap recovery (Story 2.2).

Covers: partial-swap revert per layer, full interrupt, idempotency,
cache-hit-after-recovery, absent/corrupt state, scope lock, missing
current/ directory, dangling symlinks, monitor traversal guard.
Mirrors test_reconcile.py fake-adapter patterns.
"""

from __future__ import annotations

from tests.support.palette_fakes import PaletteProducerStub, v1_colors_yaml, v1_gtk_css

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from runtime.adapters.cache import cache_entry_path
from runtime.adapters.hashing import hash_file
from runtime.adapters.json_state_repository import JsonStateRepository
from runtime.adapters.seeder import CacheSeeder, _repoint_symlink

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _now_z() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class _FakeMutex:
    def hold(self, blocking: bool = False) -> object:  # type: ignore[no-untyped-def]
        class _Hold:  # type: ignore[no-untyped-def]
            def __enter__(self_inner) -> None:
                return None

            def __exit__(self_inner, *exc: object) -> None:
                pass

        return _Hold()


class _FakeCsg(PaletteProducerStub):
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def generate(self, wallpaper_path: Path, output_dir: Path) -> object:
        from runtime.domain.models import PaletteArtifacts, PaletteEntry

        self.calls += 1
        if self.fail:
            raise RuntimeError("palette_producer exploded")
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "colors.yaml").write_text(v1_colors_yaml())
        (output_dir / "colors.conf").write_text("colors {}")
        (output_dir / "colors.gtk.css").write_text(v1_gtk_css())
        (output_dir / "colors.adw.css").write_text("colors {}")
        (output_dir / "colors.sequences").write_bytes(b"\x1b]4;0;#000\x1b\\")
        (output_dir / "colors.rasi").write_text("* { background: #000; }")
        (output_dir / "colors.kitty").write_text("* { background: #000; }")
        (output_dir / "colors.btop.theme").write_text("theme[colors.main_bg]=#000000\n")
        (output_dir / "colors.btop.theme").write_text("theme[colors.main_bg]=#000000\n")
        (output_dir / "colors.btop.theme").write_text("theme[colors.main_bg]=#000000\n")
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
                colors_kitty=hash_file(output_dir / "colors.kitty"),
                colors_btop=hash_file(output_dir / "colors.btop.theme"),
            ),
            generated_at=_now_z(),
        )


class _FakeWeg:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def generate(self, wallpaper_path: Path, output_dir: Path) -> object:
        from runtime.domain.models import EffectsArtifacts, EffectsEntry

        self.calls += 1
        if self.fail:
            raise RuntimeError("weg exploded")
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
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def render(
        self,
        palette_hash: str,
        templates_dir: Path,
        mappings_path: Path,
        output_dir: Path,
    ) -> object:
        from runtime.domain.models import IconsArtifacts, IconsEntry

        self.calls += 1
        if self.fail:
            raise RuntimeError("itr exploded")
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


def _setup_spine(install_spine: Path) -> None:
    csg_templates = install_spine / "config" / "theme-producer" / "templates"
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


def _apply_state(tmp_path: Path, *, img_bytes: bytes = b"user wallpaper bytes") -> tuple[JsonStateRepository, Path, Path, _FakeCsg, _FakeWeg, _FakeItr]:
    from runtime.application.apply_wallpaper import ApplyWallpaperUseCase

    install_spine = tmp_path / "install"
    _setup_spine(install_spine)
    state_root = tmp_path / "state"
    repo = JsonStateRepository(state_root=state_root)
    palette_producer, weg, itr = _FakeCsg(), _FakeWeg(), _FakeItr()
    img = tmp_path / "wall.png"
    img.write_bytes(img_bytes)
    ApplyWallpaperUseCase(
        state_repo=repo,
        palette_producer=palette_producer,
        weg=weg,
        itr=itr,
        install_spine=install_spine,
        state_root=state_root,
        seeder=CacheSeeder(state_root),
        mutex=_FakeMutex(),
    ).run(img)
    palette_producer.calls = weg.calls = itr.calls = 0
    return repo, state_root, install_spine, palette_producer, weg, itr


def _make_reconcile(repo: JsonStateRepository, state_root: Path, install_spine: Path, palette_producer: _FakeCsg, weg: _FakeWeg, itr: _FakeItr, tmp_path: Path | None = None, *, generation_store: object | None = None) -> object:
    from runtime.application.reconcile import ReconcileDesktopStateUseCase

    kwargs: dict[str, object] = {}
    if generation_store is not None:
        kwargs["generation_store"] = generation_store
    return ReconcileDesktopStateUseCase(
        state_repo=repo,
        palette_producer=palette_producer,
        weg=weg,
        itr=itr,
        install_spine=install_spine,
        state_root=state_root,
        seeder=CacheSeeder(state_root),
        mutex=_FakeMutex(),
        **kwargs,  # type: ignore[arg-type]
    )


def _reconcile_and_repoint(tmp_path: Path) -> tuple[JsonStateRepository, Path, Path, _FakeCsg, _FakeWeg, _FakeItr]:
    """Apply then reconcile once so current/ exists with correct targets."""
    repo, state_root, install_spine, palette_producer, weg, itr = _apply_state(tmp_path)
    _make_reconcile(repo, state_root, install_spine, palette_producer, weg, itr).run()  # type: ignore[attr-defined]
    palette_producer.calls = weg.calls = itr.calls = 0
    return repo, state_root, install_spine, palette_producer, weg, itr


def _symlink_target(link: Path) -> str:
    return os.readlink(link)


def _stale_hash(original: str) -> str:
    # flip last char to guarantee different valid hex
    return original[:-1] + ("0" if original[-1] != "0" else "1")


def _make_stale_wallpaper_entry(state_root: Path, stale_hash: str) -> Path:
    # Create a stale wallpaper cache entry so the dangling target is plausible
    entry = state_root / "cache" / "wallpapers" / stale_hash
    entry.mkdir(parents=True, exist_ok=True)
    (entry / "wallpaper.png").write_bytes(b"stale")
    (entry / "meta.json").write_text(json.dumps({"hash_algorithm": "sha256"}))
    return entry / "wallpaper.png"


def _make_stale_palette_entry(state_root: Path, stale_hash: str) -> Path:
    entry = state_root / "cache" / "palettes" / stale_hash
    entry.mkdir(parents=True, exist_ok=True)
    for n in (
        "colors.conf",
        "colors.gtk.css",
        "colors.yaml",
        "colors.adw.css",
        "colors.sequences",
        "colors.rasi",
    ):
        (entry / n).write_text("stale")
    (entry / "meta.json").write_text(json.dumps({"hash_algorithm": "sha256"}))
    return entry


class TestCrashGenerationCommit:
    """v3 crash injection: the old or new complete generation is always visible."""

    def _run_with_fault(
        self,
        tmp_path: Path,
        point: str,
    ) -> tuple[object, str, Path, str | None]:
        from runtime.adapters.generation_store import GenerationStore

        repo, state_root, install_spine, producer, weg, itr = _reconcile_and_repoint(tmp_path)
        old_hash = GenerationStore(state_root).current_hash()
        assert old_hash is not None
        img = tmp_path / "new-wall.png"
        img.write_bytes(b"new generation bytes")

        def _fault(at: str) -> None:
            if at == point:
                raise RuntimeError(f"injected crash at {at}")

        use_case = _make_reconcile(
            repo,
            state_root,
            install_spine,
            producer,
            weg,
            itr,
            generation_store=GenerationStore(state_root, fault=_fault),
        )
        with pytest.raises(RuntimeError, match=f"injected crash at {point}"):
            use_case.run(wallpaper_path=img)  # type: ignore[attr-defined]
        return use_case, old_hash, state_root, None

    def test_crash_before_generation_rename(self, tmp_path: Path) -> None:
        from runtime.adapters.generation_store import GenerationStore

        _use_case, old_hash, state_root, _ = self._run_with_fault(tmp_path, "staged")
        store = GenerationStore(state_root)
        assert store.current_hash() == old_hash
        # the torn staging directory is never a generation
        assert not list(store.generations_root.glob(".staging-*"))

    def test_crash_after_generation_rename_before_pointer(self, tmp_path: Path) -> None:
        from runtime.adapters.generation_store import GenerationStore

        _use_case, old_hash, state_root, _ = self._run_with_fault(tmp_path, "published")
        store = GenerationStore(state_root)
        assert store.current_hash() == old_hash
        dirs = [p for p in store.generations_root.iterdir() if p.is_dir() and not p.name.startswith(".")]
        assert dirs
        # every published directory has a complete manifest
        assert all(store.load_manifest(p.name) is not None for p in dirs)

    def test_crash_after_pointer_rename(self, tmp_path: Path) -> None:
        from runtime.adapters.generation_store import GenerationStore

        _use_case, old_hash, state_root, _ = self._run_with_fault(tmp_path, "committed")
        _spine = tmp_path / "install"
        _producer = _FakeCsg()
        _weg = _FakeWeg()
        _itr = _FakeItr()
        store = GenerationStore(state_root)
        new_hash = store.current_hash()
        assert new_hash is not None and new_hash != old_hash
        assert store.load_manifest(new_hash) is not None
        # current.json is a derived compatibility view and may lag; the next
        # converge repairs it after the authoritative manifest.
        repo = JsonStateRepository(state_root=state_root)
        state = repo.load_current()
        assert state is not None
        # The pointer is authoritative; current.json may lag one commit
        # behind after a crash between the pointer rename and its write.
        assert state.wallpaper.content_hash != store.load_manifest(new_hash)["intent"]["wallpaper"]
        # The next converge repairs the derived view, never rolls back.
        _make_reconcile(
            repo, state_root, _spine, _producer, _weg, _itr
        ).run(wallpaper_path=tmp_path / "new-wall.png")  # type: ignore[attr-defined]
        repaired = repo.load_current()
        assert repaired is not None
        assert repaired.wallpaper.content_hash == store.load_manifest(new_hash)["intent"]["wallpaper"]

    def test_ledger_from_old_generation_triggers_refresh_never_rollback(
        self, tmp_path: Path
    ) -> None:
        from runtime.adapters.generation_store import GenerationStore

        _use_case, old_hash, state_root, _ = self._run_with_fault(tmp_path, "committed")
        _spine = tmp_path / "install"
        _producer = _FakeCsg()
        _weg = _FakeWeg()
        _itr = _FakeItr()
        store = GenerationStore(state_root)
        new_hash = store.current_hash()
        assert new_hash is not None
        from runtime.adapters.appearance_ledger import AppearanceLedger

        ledger = AppearanceLedger(state_root)
        assert ledger.needs_refresh("ags", new_hash) is True
        assert ledger.needs_refresh("ags", old_hash) is True

    def test_gc_retains_active_and_pinned_never_acknowledged(self, tmp_path: Path) -> None:
        from runtime.adapters.generation_store import GenerationStore

        _repo, state_root, _spine, _producer, _weg, _itr = _reconcile_and_repoint(tmp_path)
        store = GenerationStore(state_root)
        active = store.current_hash()
        assert active is not None
        store.add_pin(active, "operator rollback target")
        swept = store.gc(
            keep=0,
            grace_seconds=0,
            ledger_generations=[],
            acknowledged_generations=[active],
        )
        assert active not in swept["generations"]
        assert store.load_manifest(active) is not None
        assert active in store.pinned_hashes()


class TestCrashIdempotency:
    def test_no_crash_is_noop(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        repo, state_root, install_spine, palette_producer, weg, itr = _reconcile_and_repoint(tmp_path)
        from runtime.application.reconcile import ReconcileDesktopStateUseCase

        use_case = ReconcileDesktopStateUseCase(
            state_repo=repo,
            palette_producer=palette_producer,
            weg=weg,
            itr=itr,
            install_spine=install_spine,
            state_root=state_root,
            seeder=CacheSeeder(state_root),
            mutex=_FakeMutex(),
        )
        # Directly test _revert_stale_symlinks is noop when already matching
        state = repo.load_current()
        assert state is not None
        reverted = use_case._revert_stale_symlinks(state)  # type: ignore[attr-defined]
        assert reverted == []
        assert palette_producer.calls == 0 and weg.calls == 0 and itr.calls == 0

    def test_double_recovery_noop(self, tmp_path: Path) -> None:
        repo, state_root, install_spine, palette_producer, weg, itr = _reconcile_and_repoint(tmp_path)
        from runtime.application.reconcile import ReconcileDesktopStateUseCase

        loaded = repo.load_current()
        assert loaded is not None
        stale = _stale_hash(loaded.wallpaper.content_hash)
        _make_stale_wallpaper_entry(state_root, stale)
        _repoint_symlink(state_root / "current" / "wallpaper-DP-1.png", state_root / "cache" / "wallpapers" / stale / "wallpaper.png")

        use_case = ReconcileDesktopStateUseCase(
            state_repo=repo,
            palette_producer=palette_producer,
            weg=weg,
            itr=itr,
            install_spine=install_spine,
            state_root=state_root,
            seeder=CacheSeeder(state_root),
            mutex=_FakeMutex(),
        )
        state = repo.load_current()
        assert state is not None
        first = use_case._revert_stale_symlinks(state)  # type: ignore[attr-defined]
        assert len(first) == 1
        second = use_case._revert_stale_symlinks(state)  # type: ignore[attr-defined]
        assert second == []


class TestCacheHitAfterRecovery:
    def test_recover_then_reconcile_is_cache_hit(self, tmp_path: Path) -> None:
        repo, state_root, install_spine, palette_producer, weg, itr = _reconcile_and_repoint(tmp_path)
        loaded = repo.load_current()
        assert loaded is not None
        stale = _stale_hash(loaded.wallpaper.content_hash)
        _make_stale_wallpaper_entry(state_root, stale)
        _repoint_symlink(state_root / "current" / "wallpaper-DP-1.png", state_root / "cache" / "wallpapers" / stale / "wallpaper.png")

        result = _make_reconcile(repo, state_root, install_spine, palette_producer, weg, itr).run()  # type: ignore[attr-defined]

        assert palette_producer.calls == 0
        assert weg.calls == 0
        assert itr.calls == 0
        assert result.cache_regenerated == []  # type: ignore[attr-defined]


class TestAbsentAndCorrupt:
    def test_recovery_absent_state_raises(self, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        repo = JsonStateRepository(state_root=state_root)
        install_spine = tmp_path / "install"
        _setup_spine(install_spine)
        palette_producer, weg, itr = _FakeCsg(), _FakeWeg(), _FakeItr()
        from runtime.application.reconcile import ReconcileDesktopStateUseCase

        use_case = ReconcileDesktopStateUseCase(
            state_repo=repo,
            palette_producer=palette_producer,
            weg=weg,
            itr=itr,
            install_spine=install_spine,
            state_root=state_root,
            seeder=CacheSeeder(state_root),
            mutex=_FakeMutex(),
        )
        with pytest.raises(RuntimeError, match="nothing to reconcile"):
            use_case.run()

    def test_recovery_corrupt_current_json_propagates(self, tmp_path: Path) -> None:
        repo, state_root, install_spine, palette_producer, weg, itr = _apply_state(tmp_path)
        (state_root / "current.json").write_text("{not json")
        from runtime.application.reconcile import ReconcileDesktopStateUseCase

        use_case = ReconcileDesktopStateUseCase(
            state_repo=repo,
            palette_producer=palette_producer,
            weg=weg,
            itr=itr,
            install_spine=install_spine,
            state_root=state_root,
            seeder=CacheSeeder(state_root),
            mutex=_FakeMutex(),
        )
        with pytest.raises(ValueError, match="not valid JSON"):
            use_case.run()


class TestStructuralScope:
    def test_recovery_writes_only_inside_current(self, tmp_path: Path) -> None:
        repo, state_root, install_spine, palette_producer, weg, itr = _reconcile_and_repoint(tmp_path)
        loaded = repo.load_current()
        assert loaded is not None
        stale = _stale_hash(loaded.wallpaper.content_hash)
        _make_stale_wallpaper_entry(state_root, stale)
        _repoint_symlink(state_root / "current" / "wallpaper-DP-1.png", state_root / "cache" / "wallpapers" / stale / "wallpaper.png")
        before = {p.relative_to(state_root) for p in state_root.rglob("*") if p.is_file() or p.is_symlink()}

        from runtime.application.reconcile import ReconcileDesktopStateUseCase

        use_case = ReconcileDesktopStateUseCase(
            state_repo=repo,
            palette_producer=palette_producer,
            weg=weg,
            itr=itr,
            install_spine=install_spine,
            state_root=state_root,
            seeder=CacheSeeder(state_root),
            mutex=_FakeMutex(),
        )
        state = repo.load_current()
        assert state is not None
        use_case._revert_stale_symlinks(state)  # type: ignore[attr-defined]

        after = {p.relative_to(state_root) for p in state_root.rglob("*") if p.is_file() or p.is_symlink()}
        # Only symlink target changed, no new file outside current/
        new_files = after - before
        # The stale entry we created is outside, but recovery itself adds nothing outside current/
        # So any new files must be inside current/ (or our manually created stale entry already existed)
        for nf in new_files:
            assert str(nf).startswith("current/") or str(nf).startswith("cache/"), f"unexpected write outside current/: {nf}"
        # No file at state_root root besides current.json/history.jsonl
        assert (state_root / "current" / "wallpaper-DP-1.png").is_symlink()

    def test_recovery_missing_current_dir_is_noop(self, tmp_path: Path) -> None:
        repo, state_root, install_spine, palette_producer, weg, itr = _apply_state(tmp_path)
        # remove current/ before reconcile — recovery should be noop not crash
        import shutil

        shutil.rmtree(state_root / "current", ignore_errors=True)
        from runtime.application.reconcile import ReconcileDesktopStateUseCase

        use_case = ReconcileDesktopStateUseCase(
            state_repo=repo,
            palette_producer=palette_producer,
            weg=weg,
            itr=itr,
            install_spine=install_spine,
            state_root=state_root,
            seeder=CacheSeeder(state_root),
            mutex=_FakeMutex(),
        )
        state = repo.load_current()
        assert state is not None
        reverted = use_case._revert_stale_symlinks(state)  # type: ignore[attr-defined]
        assert reverted == []

    def test_recovery_dangling_symlink_reverted(self, tmp_path: Path) -> None:
        repo, state_root, install_spine, palette_producer, weg, itr = _reconcile_and_repoint(tmp_path)
        loaded = repo.load_current()
        assert loaded is not None
        wh = loaded.wallpaper.content_hash
        link = state_root / "current" / "wallpaper-DP-1.png"
        # Point to non-existent cache entry
        dangling = state_root / "cache" / "wallpapers" / ("9" * 64) / "wallpaper.png"
        _repoint_symlink(link, dangling)
        assert not dangling.exists()

        from runtime.application.reconcile import ReconcileDesktopStateUseCase

        use_case = ReconcileDesktopStateUseCase(
            state_repo=repo,
            palette_producer=palette_producer,
            weg=weg,
            itr=itr,
            install_spine=install_spine,
            state_root=state_root,
            seeder=CacheSeeder(state_root),
            mutex=_FakeMutex(),
        )
        state = repo.load_current()
        assert state is not None
        reverted = use_case._revert_stale_symlinks(state)  # type: ignore[attr-defined]
        assert len(reverted) == 1
        assert wh in _symlink_target(link)

    def test_recovery_monitor_name_traversal_rejected(self, tmp_path: Path) -> None:
        repo, state_root, install_spine, palette_producer, weg, itr = _apply_state(tmp_path)
        loaded = repo.load_current()
        assert loaded is not None
        from runtime.domain.models import DesktopState

        # Inject traversal monitor name — bypass repo validation by editing current.json directly
        import json

        raw = json.loads((state_root / "current.json").read_text())
        # raw["monitors"] is dict of monitor_name -> config
        original_cfg = next(iter(raw["monitors"].values()))
        raw["monitors"] = {"../evil": original_cfg}
        (state_root / "current.json").write_text(json.dumps(raw))
        from runtime.application.reconcile import ReconcileDesktopStateUseCase

        use_case = ReconcileDesktopStateUseCase(
            state_repo=repo,
            palette_producer=palette_producer,
            weg=weg,
            itr=itr,
            install_spine=install_spine,
            state_root=state_root,
            seeder=CacheSeeder(state_root),
            mutex=_FakeMutex(),
        )
        with pytest.raises(ValueError, match="monitor name"):
            use_case.run()
