"""Unit tests for the terminal palette applier (Story 2.6, artifact switch in gt-2-3).

All TTY-adjacent tests inject the constructor's ``tty_path`` seam — a tmp
file as the apply target — so the real ``/dev/tty`` is never opened. The
single sanctioned ``builtins.open`` patch lives in the write-failure test:
the adapter has no subprocess seam like its siblings, so the open call IS
the I/O boundary.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable
from unittest.mock import patch

import pytest

from runtime.adapters.terminal_color_applier import TerminalColorApplier
from runtime.ports.desktop_reloader import IDesktopReloader

_COLORS = [f"#{i:02x}{i:02x}{i:02x}" for i in range(16)]


def _canonical() -> str:
    """A colors.yaml byte-exact with the pinned ``colors.yaml.j2`` schema."""
    lines = [
        'background: "#1a1b26"',
        'foreground: "#c0caf5"',
        'cursor: "#c0caf5"',
        "colors:",
        *(f'  - "{color}"' for color in _COLORS),
        'source_image: "/img/wall.png"',
        'backend: "fast"',
        'generated_at: "2026-09-02T00:00:00Z"',
    ]
    return "\n".join(lines) + "\n"


def _expected_payload() -> bytes:
    """The OLD re-derived payload (pre-gt-2-3): OSC sequences WITHOUT the
    per-line trailing LF — kept ONLY to pin Decision C's parity relation."""
    parts = [f"\x1b]4;{index};{color}\x1b\\" for index, color in enumerate(_COLORS)]
    parts.append("\x1b]10;#c0caf5\x1b\\")
    parts.append("\x1b]11;#1a1b26\x1b\\")
    parts.append("\x1b]12;#c0caf5\x1b\\")
    return "".join(parts).encode("ascii")


def _canonical_sequences() -> bytes:
    """The full 19-sequence ``colors.sequences`` artifact bytes — the exact
    pinned ``colors.sequences.j2`` shape (each OSC + ST + trailing LF, file
    ends with LF), from the same palette constants as ``_canonical()``."""
    parts = [f"\x1b]4;{index};{color}\x1b\\\n" for index, color in enumerate(_COLORS)]
    parts.append("\x1b]10;#c0caf5\x1b\\\n")
    parts.append("\x1b]11;#1a1b26\x1b\\\n")
    parts.append("\x1b]12;#c0caf5\x1b\\\n")
    return "".join(parts).encode("ascii")


def _make_sequences(tmp_path: Path, data: bytes | None = None) -> Path:
    """Seed ``current/colors.sequences`` as a real symlink to a real file."""
    state_root = tmp_path / "state"
    current = state_root / "current"
    current.mkdir(parents=True)
    target = state_root / "palette" / "colors.sequences"
    target.parent.mkdir(parents=True)
    target.write_bytes(data if data is not None else _canonical_sequences())
    (current / "colors.sequences").symlink_to(target)
    return state_root


def _make_dangling(tmp_path: Path) -> Path:
    state_root = tmp_path / "state"
    current = state_root / "current"
    current.mkdir(parents=True)
    (current / "colors.sequences").symlink_to(state_root / "missing" / "colors.sequences")
    return state_root


def _target_is_dir(tmp_path: Path) -> Path:
    target = tmp_path / "is-a-dir"
    target.mkdir()
    return target


def _target_missing_dir(tmp_path: Path) -> Path:
    return tmp_path / "missing-dir" / "tty"


def _target_no_permission(tmp_path: Path) -> Path:
    target = tmp_path / "no-perm"
    target.write_bytes(b"")
    target.chmod(0)
    return target


class TestTerminalColorApplierReadsArtifact:
    def test_writes_artifact_bytes_verbatim(self, tmp_path: Path) -> None:
        state_root = _make_sequences(tmp_path)
        sink = tmp_path / "tty"
        applier = TerminalColorApplier(state_root=state_root, tty_path=sink)
        assert applier.reload() is True
        # LF-inclusive: the artifact bytes are written UNMODIFIED (no decode,
        # no re-derivation, per-line trailing LFs included).
        assert sink.read_bytes() == _canonical_sequences()

    def test_hex_values_pass_through_verbatim(self, tmp_path: Path) -> None:
        colors = [f"#{i:02X}{i:02X}{i:02X}" for i in range(16)]
        artifact = b"".join(
            f"\x1b]4;{index};{color}\x1b\\\n".encode("ascii") for index, color in enumerate(colors)
        )
        artifact += b"\x1b]10;#C0CAF5\x1b\\\n\x1b]11;#1A1B26\x1b\\\n\x1b]12;#c0cAf5\x1b\\\n"
        state_root = _make_sequences(tmp_path, artifact)
        sink = tmp_path / "tty"
        applier = TerminalColorApplier(state_root=state_root, tty_path=sink)
        assert applier.reload() is True
        assert sink.read_bytes() == artifact

    def test_parity_lf_augmented_identity_with_old_payload(self, tmp_path: Path) -> None:
        """Decision C: artifact bytes are LF-augmented-identical to the old
        re-derived payload.

        Deliberately NOT asserting raw byte equality with the old payload:
        the artifact carries the pinned template's 19 per-line trailing LFs
        (the old ``_build_payload`` omitted them). The pinned relation is
        ``artifact.replace(ST+LF, ST) == old_payload`` — safe because the
        OSC payloads (slot indices + ``#rrggbb`` hex) contain no ESC or
        backslash bytes, so every ST+LF occurrence is a sequence boundary.
        """
        state_root = _make_sequences(tmp_path)
        sink = tmp_path / "tty"
        applier = TerminalColorApplier(state_root=state_root, tty_path=sink)
        assert applier.reload() is True
        artifact = _canonical_sequences()
        assert sink.read_bytes() == artifact
        assert artifact.replace(b"\x1b\\\n", b"\x1b\\") == _expected_payload()
        # both derivations carry the SAME palette constants pinned in the
        # canonical colors.yaml text fixture (the old parser's happy-path source)
        assert re.findall(r'^  - "([^"]+)"', _canonical(), re.M) == _COLORS


class TestTerminalColorApplierVacuous:
    def test_missing_colors_sequences_returns_true(self, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        state_root.mkdir(parents=True)
        sink = tmp_path / "sink"
        applier = TerminalColorApplier(state_root=state_root, tty_path=sink)
        assert applier.reload() is True
        assert not sink.exists()

    def test_current_dir_exists_no_colors_sequences_returns_true(self, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        (state_root / "current").mkdir(parents=True)
        sink = tmp_path / "sink"
        applier = TerminalColorApplier(state_root=state_root, tty_path=sink)
        assert applier.reload() is True
        assert not sink.exists()

    def test_vacuous_true_wins_without_tty(self, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        (state_root / "current").mkdir(parents=True)
        unreachable = tmp_path / "is-a-dir"
        unreachable.mkdir()
        applier = TerminalColorApplier(state_root=state_root, tty_path=unreachable)
        assert applier.reload() is True
        assert not any(unreachable.iterdir()), "vacuous path must not touch the TTY"

    def test_missing_consumer_no_write_attempted(self, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        (state_root / "current").mkdir(parents=True)
        sink = tmp_path / "sink"
        applier = TerminalColorApplier(state_root=state_root, tty_path=sink)
        assert applier.reload() is True
        assert not sink.exists(), "no open/write may be attempted on the vacuous path"


class TestTerminalColorApplierFailure:
    def test_dangling_symlink_is_failure(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        state_root = _make_dangling(tmp_path)
        sink = tmp_path / "sink"
        applier = TerminalColorApplier(state_root=state_root, tty_path=sink)
        with caplog.at_level(logging.WARNING):
            assert applier.reload() is False
        assert not sink.exists()
        assert "dangling symlink" in caplog.text

    @pytest.mark.parametrize(
        "corrupt",
        [
            b"",
            b"junk\n",
            b"\xff\xfe\x00garbage",
        ],
    )
    def test_corrupt_sequences_bytes_are_failure(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, corrupt: bytes
    ) -> None:
        """R5: a corrupt cache artifact is surfaced, never written to the TTY."""
        state_root = _make_sequences(tmp_path, corrupt)
        sink = tmp_path / "sink"
        applier = TerminalColorApplier(state_root=state_root, tty_path=sink)
        with caplog.at_level(logging.WARNING):
            assert applier.reload() is False
        assert not sink.exists()
        assert "corrupt (not OSC sequences)" in caplog.text

    def test_regular_file_entry_is_traversed(self, tmp_path: Path) -> None:
        """An existing-but-not-symlink entry is not dangling — it is read and applied."""
        state_root = tmp_path / "state"
        current = state_root / "current"
        current.mkdir(parents=True)
        (current / "colors.sequences").write_bytes(_canonical_sequences())
        sink = tmp_path / "tty"
        applier = TerminalColorApplier(state_root=state_root, tty_path=sink)
        assert applier.reload() is True
        assert sink.read_bytes() == _canonical_sequences()

    def test_unreadable_sequences_file_is_failure(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        state_root = _make_sequences(tmp_path)
        (state_root / "palette" / "colors.sequences").chmod(0)
        sink = tmp_path / "sink"
        applier = TerminalColorApplier(state_root=state_root, tty_path=sink)
        with caplog.at_level(logging.WARNING):
            assert applier.reload() is False
        assert not sink.exists()
        assert "cannot read" in caplog.text

    @pytest.mark.parametrize(
        "make_target", [_target_is_dir, _target_missing_dir, _target_no_permission]
    )
    def test_tty_open_failure_returns_false(
        self, tmp_path: Path, make_target: Callable[[Path], Path]
    ) -> None:
        state_root = _make_sequences(tmp_path)
        applier = TerminalColorApplier(state_root=state_root, tty_path=make_target(tmp_path))
        assert applier.reload() is False

    def test_tty_write_failure_returns_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The ONE sanctioned patch in this file: the adapter has no subprocess
        # seam like its siblings, so the open() call IS the I/O boundary.
        state_root = _make_sequences(tmp_path)
        applier = TerminalColorApplier(state_root=state_root, tty_path=tmp_path / "tty")

        def _fail_open(*_args: object, **_kwargs: object) -> object:
            raise OSError("simulated palette write failure")

        monkeypatch.setattr("builtins.open", _fail_open)
        assert applier.reload() is False


class TestTerminalColorApplierInterface:
    def test_port_conformance(self, tmp_path: Path) -> None:
        applier = TerminalColorApplier(state_root=tmp_path / "state", tty_path=tmp_path / "tty")
        assert isinstance(applier, IDesktopReloader)


class TestTerminalColorApplierStateRoot:
    def test_env_default_state_root_resolution(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
        applier = TerminalColorApplier(state_root=None)
        assert applier._state_root == (tmp_path / "xdg-state" / "dotfiles").resolve()

        with patch.dict(os.environ, {"XDG_STATE_HOME": ""}):
            fallback = TerminalColorApplier(state_root=None)
        assert fallback._state_root == (Path.home() / ".local" / "state" / "dotfiles").resolve()


class _FakeMutex:
    def hold(self, blocking: bool = False) -> object:  # type: ignore[no-untyped-def]
        class _Hold:
            def __enter__(self_inner) -> None:
                return None

            def __exit__(self_inner, *exc: object) -> None:
                pass

        return _Hold()


class _FakeCsg:
    def generate(self, wallpaper_path: Path, output_dir: Path) -> object:
        from runtime.adapters.hashing import hash_file
        from runtime.domain.models import PaletteArtifacts, PaletteEntry

        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "colors.yaml").write_text(_canonical())
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
    def generate(self, wallpaper_path: Path, output_dir: Path) -> object:
        from runtime.adapters.hashing import hash_file
        from runtime.domain.models import EffectsArtifacts, EffectsEntry

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
    def render(
        self, palette_hash: str, templates_dir: Path, mappings_path: Path, output_dir: Path
    ) -> object:
        from runtime.adapters.hashing import hash_file
        from runtime.domain.models import IconsArtifacts, IconsEntry

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


def _now_z() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _setup_spine(install_spine: Path) -> None:
    csg_templates = install_spine / "config" / "color-scheme-generator" / "templates"
    csg_templates.mkdir(parents=True)
    (csg_templates / "default.yaml").write_text("window: {}\n")
    weg_cfg = install_spine / "config" / "weg"
    weg_cfg.mkdir(parents=True)
    (weg_cfg / "effects.yaml").write_text("effects: []\n")
    itr_templates = install_spine / "icon-templates"
    itr_templates.mkdir(parents=True)
    (itr_templates / "terminal.svg").write_text("<svg/>")
    (install_spine / "icon-mappings").mkdir(parents=True, exist_ok=True)
    (install_spine / "icon-mappings" / "icons.yaml").write_text("icons: {}\n")


def _make_applied(tmp_path: Path) -> tuple[object, Path, Path]:
    from runtime.adapters.json_state_repository import JsonStateRepository
    from runtime.adapters.seeder import CacheSeeder
    from runtime.application.apply_wallpaper import ApplyWallpaperUseCase

    install_spine = tmp_path / "install"
    _setup_spine(install_spine)
    state_root = tmp_path / "state"
    repo = JsonStateRepository(state_root=state_root)
    img = tmp_path / "wall.png"
    img.write_bytes(b"terminal applier unit wallpaper")
    ApplyWallpaperUseCase(
        state_repo=repo,
        csg=_FakeCsg(),  # type: ignore[arg-type]
        weg=_FakeWeg(),  # type: ignore[arg-type]
        itr=_FakeItr(),  # type: ignore[arg-type]
        install_spine=install_spine,
        state_root=state_root,
        seeder=CacheSeeder(state_root),
        mutex=_FakeMutex(),  # type: ignore[arg-type]
    ).run(img)
    return repo, state_root, install_spine


def _make_reconcile_with_reloaders(
    repo: object,
    state_root: Path,
    install_spine: Path,
    reloaders: list[IDesktopReloader],
) -> object:
    from runtime.adapters.seeder import CacheSeeder
    from runtime.application.reconcile import ReconcileDesktopStateUseCase

    return ReconcileDesktopStateUseCase(
        state_repo=repo,  # type: ignore[arg-type]
        csg=_FakeCsg(),  # type: ignore[arg-type]
        weg=_FakeWeg(),  # type: ignore[arg-type]
        itr=_FakeItr(),  # type: ignore[arg-type]
        install_spine=install_spine,
        state_root=state_root,
        seeder=CacheSeeder(state_root),
        mutex=_FakeMutex(),  # type: ignore[arg-type]
        reloaders=reloaders,
    )


class _CountingReloader(IDesktopReloader):
    def __init__(self) -> None:
        self.calls = 0

    def reload(self) -> bool:
        self.calls += 1
        return True


class TestTerminalColorApplierReconcile:
    def test_terminal_failure_populates_reload_failures(self, tmp_path: Path) -> None:
        repo, state_root, install_spine = _make_applied(tmp_path)
        unwritable = tmp_path / "unwritable-sink"
        unwritable.mkdir()
        terminal = TerminalColorApplier(state_root=state_root, tty_path=unwritable)
        passing = _CountingReloader()
        use_case = _make_reconcile_with_reloaders(
            repo, state_root, install_spine, [passing, terminal]
        )
        result = use_case.run()  # type: ignore[attr-defined]
        assert "TerminalColorApplier" in result.reload_failures
        assert "_CountingReloader" not in result.reload_failures
        assert not any(unwritable.iterdir()), "no palette bytes may be written on failure"

    def test_all_reloaders_invoked_once(self, tmp_path: Path) -> None:
        repo, state_root, install_spine = _make_applied(tmp_path)
        hyprland = _CountingReloader()
        ags = _CountingReloader()
        hyprpaper = _CountingReloader()
        terminal = _CountingReloader()
        use_case = _make_reconcile_with_reloaders(
            repo, state_root, install_spine, [hyprland, ags, hyprpaper, terminal]
        )
        use_case.run()  # type: ignore[attr-defined]
        for reloader in (hyprland, ags, hyprpaper, terminal):
            assert reloader.calls == 1
