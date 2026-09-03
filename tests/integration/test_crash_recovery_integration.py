"""Integration tests for crash-mid-swap recovery — real filesystem + real repo + fakes."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from runtime.adapters.cache import cache_entry_path
from runtime.adapters.flock_seed_mutex import FlockSeedMutex
from runtime.adapters.hashing import hash_file
from runtime.adapters.json_state_repository import JsonStateRepository
from runtime.adapters.seeder import CacheSeeder, _repoint_symlink

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
WALLPAPER_PNG = FIXTURES / "wallpaper.png"


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
            ),
            generated_at="2026-01-01T00:00:00Z",
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
            generated_at="2026-01-01T00:00:00Z",
        )


class _FakeItr:
    def __init__(self) -> None:
        self.calls = 0

    def render(self, palette_hash: str, templates_dir: Path, mappings_path: Path, output_dir: Path) -> object:
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
            generated_at="2026-01-01T00:00:00Z",
        )


class _FakeFactory:
    def create_static(self, backend_type: object) -> object:
        raise NotImplementedError

    def create_video(self, backend_type: object) -> object:
        raise NotImplementedError

    def auto_detect(self, source_path: str) -> None:
        return None


def _setup(tmp_path: Path) -> tuple[JsonStateRepository, Path, Path, _FakeCsg, _FakeWeg, _FakeItr]:
    install_spine = tmp_path / "install"
    generated = install_spine / "generated"
    generated.mkdir(parents=True)
    (generated / "default.png").write_bytes(WALLPAPER_PNG.read_bytes())
    csg_templates = install_spine / "config" / "color-scheme-generator" / "templates"
    csg_templates.mkdir(parents=True)
    (csg_templates / "default.yaml").write_text("window: {}\n")
    weg_config = install_spine / "config" / "weg"
    weg_config.mkdir(parents=True)
    (weg_config / "effects.yaml").write_text("effects: []\n")
    itr_templates = install_spine / "config" / "icon-templates-renderer" / "templates"
    itr_templates.mkdir(parents=True)
    (itr_templates / "terminal.svg").write_text("<svg/>")
    (install_spine / "config" / "icon-templates-renderer" / "icons.yaml").write_text("icons: {}\n")
    state_root = tmp_path / "state"
    repo = JsonStateRepository(state_root=state_root)
    csg, weg, itr = _FakeCsg(), _FakeWeg(), _FakeItr()
    from runtime.application.apply_wallpaper import ApplyWallpaperUseCase
    from runtime.application.seed_cache import SeedCacheUseCase

    seeder = CacheSeeder(state_root)
    SeedCacheUseCase(
        state_repo=repo,
        csg=csg,
        weg=weg,
        itr=itr,
        factory=_FakeFactory(),
        install_spine=install_spine,
        state_root=state_root,
        seeder=seeder,
        mutex=FlockSeedMutex(state_root / ".seed.lock"),
    ).run()
    img = tmp_path / "wall.png"
    img.write_bytes(b"user wallpaper bytes")
    ApplyWallpaperUseCase(
        state_repo=repo,
        csg=csg,
        weg=weg,
        itr=itr,
        install_spine=install_spine,
        state_root=state_root,
        seeder=seeder,
        mutex=FlockSeedMutex(state_root / ".seed.lock"),
    ).run(img)
    csg.calls = weg.calls = itr.calls = 0
    return repo, state_root, install_spine, csg, weg, itr


class TestCrashRecoveryIntegration:
    def test_e2e_recovery_converges_to_last_good(self, tmp_path: Path) -> None:
        repo, state_root, install_spine, csg, weg, itr = _setup(tmp_path)
        # Normal reconcile to establish current/ correct
        from runtime.application.reconcile import ReconcileDesktopStateUseCase

        ReconcileDesktopStateUseCase(
            state_repo=repo,
            csg=csg,
            weg=weg,
            itr=itr,
            install_spine=install_spine,
            state_root=state_root,
            seeder=CacheSeeder(state_root),
            mutex=FlockSeedMutex(state_root / ".seed.lock"),
        ).run()
        csg.calls = weg.calls = itr.calls = 0
        loaded = repo.load_current()
        assert loaded is not None
        wh = loaded.wallpaper.content_hash
        peh = loaded.palette.entry_hash if loaded.palette else None
        # Simulate crash: repoint 2 symlinks to stale hashes
        stale_wh = wh[:-1] + ("0" if wh[-1] != "0" else "1")
        stale_entry = state_root / "cache" / "wallpapers" / stale_wh
        stale_entry.mkdir(parents=True, exist_ok=True)
        (stale_entry / "wallpaper.png").write_bytes(b"stale")
        (stale_entry / "meta.json").write_text(json.dumps({"hash_algorithm": "sha256"}))
        _repoint_symlink(state_root / "current" / "wallpaper-DP-1.png", stale_entry / "wallpaper.png")
        if peh:
            stale_peh = peh[:-1] + ("0" if peh[-1] != "0" else "1")
            stale_pal = state_root / "cache" / "palettes" / stale_peh
            stale_pal.mkdir(parents=True, exist_ok=True)
            for n in ("colors.conf", "colors.gtk.css", "colors.yaml"):
                (stale_pal / n).write_text("stale")
            (stale_pal / "meta.json").write_text(json.dumps({"hash_algorithm": "sha256"}))
            _repoint_symlink(state_root / "current" / "colors.conf", stale_pal / "colors.conf")

        result = ReconcileDesktopStateUseCase(
            state_repo=repo,
            csg=csg,
            weg=weg,
            itr=itr,
            install_spine=install_spine,
            state_root=state_root,
            seeder=CacheSeeder(state_root),
            mutex=FlockSeedMutex(state_root / ".seed.lock"),
        ).run()

        # Symlinks match current.json
        assert wh in os.readlink(state_root / "current" / "wallpaper-DP-1.png")
        if peh:
            assert peh in os.readlink(state_root / "current" / "colors.conf")
        # Cache-hit verification
        assert csg.calls == 0 and weg.calls == 0 and itr.calls == 0
        assert result.cache_regenerated == []

    def test_recovery_plus_history_one_line(self, tmp_path: Path) -> None:
        repo, state_root, install_spine, csg, weg, itr = _setup(tmp_path)
        from runtime.application.reconcile import ReconcileDesktopStateUseCase

        # ensure current/
        ReconcileDesktopStateUseCase(
            state_repo=repo,
            csg=csg,
            weg=weg,
            itr=itr,
            install_spine=install_spine,
            state_root=state_root,
            seeder=CacheSeeder(state_root),
            mutex=FlockSeedMutex(state_root / ".seed.lock"),
        ).run()
        csg.calls = weg.calls = itr.calls = 0
        # corrupt one symlink to force recovery
        loaded = repo.load_current()
        assert loaded is not None
        stale = loaded.wallpaper.content_hash[:-1] + "0"
        stale_entry = state_root / "cache" / "wallpapers" / stale
        stale_entry.mkdir(parents=True, exist_ok=True)
        (stale_entry / "wallpaper.png").write_bytes(b"x")
        (stale_entry / "meta.json").write_text(json.dumps({"hash_algorithm": "sha256"}))
        _repoint_symlink(state_root / "current" / "wallpaper-DP-1.png", stale_entry / "wallpaper.png")
        history_before = (state_root / "history.jsonl").read_text().splitlines() if (state_root / "history.jsonl").exists() else []

        ReconcileDesktopStateUseCase(
            state_repo=repo,
            csg=csg,
            weg=weg,
            itr=itr,
            install_spine=install_spine,
            state_root=state_root,
            seeder=CacheSeeder(state_root),
            mutex=FlockSeedMutex(state_root / ".seed.lock"),
        ).run()

        history_after = (state_root / "history.jsonl").read_text().splitlines()
        # exactly one new line appended (trigger reconcile)
        assert len(history_after) == len(history_before) + 1
        assert json.loads(history_after[-1])["trigger"] == "reconcile"

    def test_restart_survival_fresh_instances_accumulate_byte_identical(self, tmp_path: Path) -> None:
        """AC 3 / FR-4: the store survives process restart — a FRESH
        CacheSeeder appended against a FRESH JsonStateRepository on the same
        state_root (simulated new process) accumulates history while prior
        lines stay byte-identical."""
        repo, state_root, install_spine, csg, weg, itr = _setup(tmp_path)
        from runtime.application.reconcile import ReconcileDesktopStateUseCase

        # First "process": seed + apply establish current.json + history.
        first_history = (state_root / "history.jsonl").read_text().splitlines()
        assert len(first_history) >= 1

        # Second "process": brand-new adapter pair on the SAME state_root.
        fresh_repo = JsonStateRepository(state_root=state_root)
        assert fresh_repo.load_current() is not None  # restart sees persisted state
        fresh_seeder = CacheSeeder(state_root)
        ReconcileDesktopStateUseCase(
            state_repo=fresh_repo,
            csg=csg,
            weg=weg,
            itr=itr,
            install_spine=install_spine,
            state_root=state_root,
            seeder=fresh_seeder,
            mutex=FlockSeedMutex(state_root / ".seed.lock"),
        ).run()

        after = (state_root / "history.jsonl").read_text().splitlines()
        assert len(after) == len(first_history) + 1  # accumulation across restart
        assert after[: len(first_history)] == first_history  # prior lines byte-identical
        assert json.loads(after[-1])["trigger"] == "reconcile"

    def test_crash_window_reconcile_save_append_lost_line_not_backfilled(self, tmp_path: Path) -> None:
        """AC 5 reconcile window (reconcile.py save→append): rt-2-2 divergence
        recovery CANNOT detect a crash here (symlinks and current.json MATCH
        post-save), so the transition's history line is lost forever. Reconcile
        self-heals lazily — the NEXT reconcile appends unconditionally — but the
        crashed line is NOT backfilled. Pin: deleting a prior line leaves exactly
        that line missing after the next reconcile (no resurrection)."""
        repo, state_root, install_spine, csg, weg, itr = _setup(tmp_path)
        from runtime.application.reconcile import ReconcileDesktopStateUseCase

        ReconcileDesktopStateUseCase(
            state_repo=repo,
            csg=csg,
            weg=weg,
            itr=itr,
            install_spine=install_spine,
            state_root=state_root,
            seeder=CacheSeeder(state_root),
            mutex=FlockSeedMutex(state_root / ".seed.lock"),
        ).run()
        csg.calls = weg.calls = itr.calls = 0
        before = (state_root / "history.jsonl").read_text().splitlines()
        assert before

        # Simulate a crash AFTER current.json matched symlinks but BEFORE the
        # line landed: drop the last line (the lost transition).
        simulate_lost_append = before[:-1]
        (state_root / "history.jsonl").write_text("\n".join(simulate_lost_append) + "\n")

        ReconcileDesktopStateUseCase(
            state_repo=repo,
            csg=csg,
            weg=weg,
            itr=itr,
            install_spine=install_spine,
            state_root=state_root,
            seeder=CacheSeeder(state_root),
            mutex=FlockSeedMutex(state_root / ".seed.lock"),
        ).run()

        after = (state_root / "history.jsonl").read_text().splitlines()
        # self-heal: exactly ONE new line appended for the new reconcile...
        assert len(after) == len(before)
        # ...and the previously-lost line is NOT resurrected (append-only;
        # current.json history can never be reconstructed).
        assert all(after[i] == before[i] for i in range(len(simulate_lost_append)))

    def test_crash_window_seed_save_append_seed_line_permanently_lost(self, tmp_path: Path) -> None:
        """AC 5 seed window (seed_cache.py save→append): permanent loss. After a
        crash between save and append, load_current() returns non-None → the
        next seed run is a NO-OP, so the ``trigger: "seed"`` line is never
        written (a later reconcile appends a "reconcile" line, or nothing)."""
        repo, state_root, install_spine, csg, weg, itr = _setup(tmp_path)
        from runtime.application.reconcile import ReconcileDesktopStateUseCase
        from runtime.application.seed_cache import SeedCacheUseCase

        # Crash AFTER seed's save + repoint, BEFORE its append: current.json
        # exists but the seed line never landed.
        seed_lines = (state_root / "history.jsonl").read_text().splitlines()
        assert any(json.loads(l)["trigger"] == "seed" for l in seed_lines)

        # Drop ALL seed lines (the crashed append never wrote them).
        (state_root / "history.jsonl").write_text("")

        # Re-run seed in a "fresh process": current.json present → no-op →
        # no seed line ever appears for the re-run.
        SeedCacheUseCase(
            state_repo=JsonStateRepository(state_root=state_root),
            csg=_FakeCsg(),
            weg=_FakeWeg(),
            itr=_FakeItr(),
            factory=_FakeFactory(),
            install_spine=install_spine,
            state_root=state_root,
            seeder=CacheSeeder(state_root),
            mutex=FlockSeedMutex(state_root / ".seed.lock"),
        ).run()

        after_seed = (state_root / "history.jsonl").read_text().splitlines()
        # Permanent loss: no trigger:"seed" line anywhere — the seed line can
        # never be written once the store observes current.json.
        assert not any(json.loads(l)["trigger"] == "seed" for l in after_seed)

        # A subsequent reconcile appends a "reconcile" line — never the lost seed line.
        ReconcileDesktopStateUseCase(
            state_repo=JsonStateRepository(state_root=state_root),
            csg=csg,
            weg=weg,
            itr=itr,
            install_spine=install_spine,
            state_root=state_root,
            seeder=CacheSeeder(state_root),
            mutex=FlockSeedMutex(state_root / ".seed.lock"),
        ).run()
        final = (state_root / "history.jsonl").read_text().splitlines()
        assert json.loads(final[-1])["trigger"] == "reconcile"

    def test_concurrent_recovery_safety(self, tmp_path: Path) -> None:
        repo, state_root, install_spine, csg, weg, itr = _setup(tmp_path)
        from runtime.application.reconcile import ReconcileDesktopStateUseCase

        ReconcileDesktopStateUseCase(
            state_repo=repo,
            csg=csg,
            weg=weg,
            itr=itr,
            install_spine=install_spine,
            state_root=state_root,
            seeder=CacheSeeder(state_root),
            mutex=FlockSeedMutex(state_root / ".seed.lock"),
        ).run()
        loaded = repo.load_current()
        assert loaded is not None
        stale = loaded.wallpaper.content_hash[:-1] + "0"
        stale_entry = state_root / "cache" / "wallpapers" / stale
        stale_entry.mkdir(parents=True, exist_ok=True)
        (stale_entry / "wallpaper.png").write_bytes(b"x")
        (stale_entry / "meta.json").write_text(json.dumps({"hash_algorithm": "sha256"}))
        _repoint_symlink(state_root / "current" / "wallpaper-DP-1.png", stale_entry / "wallpaper.png")

        errors: list[Exception] = []

        def _run() -> None:
            try:
                ReconcileDesktopStateUseCase(
                    state_repo=repo,
                    csg=_FakeCsg(),
                    weg=_FakeWeg(),
                    itr=_FakeItr(),
                    install_spine=install_spine,
                    state_root=state_root,
                    seeder=CacheSeeder(state_root),
                    mutex=FlockSeedMutex(state_root / ".seed.lock"),
                ).run()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        t1 = threading.Thread(target=_run)
        t2 = threading.Thread(target=_run)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        assert not errors
        # symlinks still correct
        assert loaded.wallpaper.content_hash in os.readlink(state_root / "current" / "wallpaper-DP-1.png")
