"""Unit tests for WegAdapter env override (Story 1.8, AD-7).

Covers ACs 1-5: env override literal keys, no settings.toml edit,
container passthrough, timeout, error preservation, entry-hash validation,
artifact hash hex, is_available.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from runtime.adapters.hashing import (
    HASH_ALGORITHM,
    effects_entry_hash,
    hash_file,
)
from runtime.adapters.weg_adapter import WegAdapter


def _make_catalog_file(tmp_path: Path) -> Path:
    """Create a minimal effects catalog file for deterministic hash."""
    cpath = tmp_path / "effects.yaml"
    cpath.write_text("version: '1.0'\neffects:\n- name: blur\n  command: magick\n")
    return cpath


def _copy_wallpaper_fixture(tmp_path: Path) -> Path:
    """Copy deterministic wallpaper.png fixture (619cd350...) into tmp_path."""
    candidates = [
        Path("tests/fixtures/wallpaper.png"),
        Path("src/runtime/tests/fixtures/wallpaper.png"),
        Path(__file__).parent.parent / "fixtures" / "wallpaper.png",
    ]
    fixture = next((p for p in candidates if p.exists()), None)
    assert fixture is not None, f"wallpaper.png fixture not found, tried {candidates}"
    dest = tmp_path / "wallpaper.png"
    dest.write_bytes(fixture.read_bytes())
    return dest


def _fake_success_factory() -> MagicMock:
    """Return a fake subprocess.run that writes PNG artifacts into env out dir."""

    def fake_run(
        args: list[str],
        capture_output: bool = False,  # noqa: ARG001
        text: bool = False,  # noqa: ARG001
        env: dict[str, str] | None = None,
        timeout: int | None = None,  # noqa: ARG001
    ) -> subprocess.CompletedProcess[str]:
        assert env is not None
        out = Path(env["WALLPAPER__OUTPUT__DIRECTORY"])
        out.mkdir(parents=True, exist_ok=True)
        # Simulate weg's nested output but also flat to test rglob collection:
        # Use flat PNGs for simplicity — adapter uses rglob so both work.
        # Write 3 PNGs with minimal PNG header bytes (not valid image but hashable)
        png_header = b"\x89PNG\r\n\x1a\n" + b"\x00" * 10
        (out / "blur.png").write_bytes(png_header + b"1")
        (out / "negate.png").write_bytes(png_header + b"2")
        (out / "sepia.png").write_bytes(png_header + b"3")
        # Also write nested to prove rglob captures both levels
        nested = out / "nested"
        nested.mkdir(exist_ok=True)
        (nested / "extra.png").write_bytes(png_header + b"4")
        return subprocess.CompletedProcess(args, 0, "", "")

    return MagicMock(side_effect=fake_run)


# ---------------------------------------------------------------------------
# 1. Env override writes to output_dir, returns EffectsEntry
# ---------------------------------------------------------------------------


def test_weg_generate_env_override_writes_to_output_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wallpaper = _copy_wallpaper_fixture(tmp_path)
    catalog = _make_catalog_file(tmp_path)
    catalog_hash = hash_file(catalog)
    wallpaper_hash = hash_file(wallpaper)
    eh = effects_entry_hash(wallpaper_hash, catalog_hash)
    output_dir = tmp_path / "state" / "cache" / "effects" / eh

    fake = _fake_success_factory()
    monkeypatch.setattr("runtime.adapters.weg_adapter.subprocess.run", fake)

    adapter = WegAdapter(catalog_path=catalog)
    entry = adapter.generate(wallpaper, output_dir)

    assert entry.hash_algorithm == HASH_ALGORITHM == "sha256"
    assert entry.kind == "effects"
    assert entry.entry_hash == eh
    assert entry.source_wallpaper_hash == wallpaper_hash
    assert entry.input_catalog_hash == catalog_hash
    # artifact_hashes should contain our 4 PNGs (keys are basenames when unique)
    assert "blur.png" in entry.artifact_hashes
    assert "negate.png" in entry.artifact_hashes
    assert "sepia.png" in entry.artifact_hashes
    assert entry.artifact_hashes["blur.png"] == hash_file(output_dir / "blur.png")
    assert entry.artifact_hashes["negate.png"] == hash_file(output_dir / "negate.png")
    # Files exist
    assert (output_dir / "blur.png").is_file()
    # Env keys literal, no -o flag
    assert fake.call_count == 1
    _, kwargs = fake.call_args
    env_passed = kwargs.get("env") or fake.call_args.kwargs.get("env")
    args_passed = kwargs.get("args") or (fake.call_args.args[0] if fake.call_args.args else None)
    if args_passed is None:
        args_passed = fake.call_args[0][0]
    assert env_passed is not None
    assert env_passed["WALLPAPER__OUTPUT__DIRECTORY"] == str(output_dir)
    assert "-o" not in args_passed
    assert "--output" not in args_passed
    assert "batch" in args_passed
    assert "all" in args_passed
    assert str(wallpaper) in args_passed
    assert list((tmp_path / "state" / "cache").glob(".staging-*")) == []


# ---------------------------------------------------------------------------
# 2. Never touches settings.toml
# ---------------------------------------------------------------------------


def test_weg_adapter_never_touches_settings_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wallpaper = _copy_wallpaper_fixture(tmp_path)
    catalog = _make_catalog_file(tmp_path)
    eh = effects_entry_hash(hash_file(wallpaper), hash_file(catalog))
    output_dir = tmp_path / "state" / "cache" / "effects" / eh

    candidates = [
        Path(__file__).resolve().parents[4]
        / "src"
        / "cli-tools"
        / "wallpaper-effects-generator"
        / "src"
        / "wallpaper_effects_generator"
        / "defaults"
        / "settings.toml",
        Path(__file__).resolve().parents[3]
        / "src"
        / "cli-tools"
        / "wallpaper-effects-generator"
        / "defaults"
        / "settings.toml",
    ]
    real_settings = next((p for p in candidates if p.is_file()), None)
    if real_settings is not None:
        mtime_before = real_settings.stat().st_mtime_ns
        content_before = real_settings.read_bytes()
    else:
        real_settings = tmp_path / "settings.toml"
        real_settings.write_text('[wallpaper]\nbackend = "magick"\n')
        mtime_before = real_settings.stat().st_mtime_ns
        content_before = real_settings.read_bytes()

    fake = _fake_success_factory()
    monkeypatch.setattr("runtime.adapters.weg_adapter.subprocess.run", fake)

    adapter = WegAdapter(catalog_path=catalog)
    adapter.generate(wallpaper, output_dir)

    assert real_settings.stat().st_mtime_ns == mtime_before
    assert real_settings.read_bytes() == content_before


# ---------------------------------------------------------------------------
# 3. Container env passthrough (host-only env would fail)
# ---------------------------------------------------------------------------


def test_weg_adapter_container_env_passthrough(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    wallpaper = _copy_wallpaper_fixture(tmp_path)
    catalog = _make_catalog_file(tmp_path)
    eh = effects_entry_hash(hash_file(wallpaper), hash_file(catalog))
    output_dir = tmp_path / "staging" / eh

    monkeypatch.delenv("WALLPAPER__OUTPUT__DIRECTORY", raising=False)
    assert "WALLPAPER__OUTPUT__DIRECTORY" not in os.environ

    captured: dict[str, str] = {}

    def fake_run(
        args: list[str],
        capture_output: bool = False,
        text: bool = False,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess[str]:  # noqa: ARG001
        assert env is not None
        captured.update(env)
        out = Path(env["WALLPAPER__OUTPUT__DIRECTORY"])
        out.mkdir(parents=True, exist_ok=True)
        (out / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"a")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("runtime.adapters.weg_adapter.subprocess.run", fake_run)

    adapter = WegAdapter(catalog_path=catalog)
    adapter.generate(wallpaper, output_dir)

    assert captured["WALLPAPER__OUTPUT__DIRECTORY"] == str(output_dir)
    assert "WALLPAPER__OUTPUT__DIRECTORY" not in os.environ


# ---------------------------------------------------------------------------
# 4. Raises on non-zero exit, preserves stderr
# ---------------------------------------------------------------------------


def test_weg_adapter_raises_on_non_zero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wallpaper = _copy_wallpaper_fixture(tmp_path)
    catalog = _make_catalog_file(tmp_path)
    eh = effects_entry_hash(hash_file(wallpaper), hash_file(catalog))
    output_dir = tmp_path / "state" / "cache" / "effects" / eh

    def fake_fail(
        args: list[str],
        capture_output: bool = False,
        text: bool = False,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess[str]:  # noqa: ARG001
        return subprocess.CompletedProcess(args, 1, "", "backend not available")

    monkeypatch.setattr("runtime.adapters.weg_adapter.subprocess.run", fake_fail)

    adapter = WegAdapter(catalog_path=catalog)
    with pytest.raises(RuntimeError, match="backend not available"):
        adapter.generate(wallpaper, output_dir)


# ---------------------------------------------------------------------------
# 5. Timeout raises
# ---------------------------------------------------------------------------


def test_weg_adapter_timeout_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wallpaper = _copy_wallpaper_fixture(tmp_path)
    catalog = _make_catalog_file(tmp_path)
    eh = effects_entry_hash(hash_file(wallpaper), hash_file(catalog))
    output_dir = tmp_path / "state" / "cache" / "effects" / eh

    def fake_timeout(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:  # noqa: ARG001
        raise subprocess.TimeoutExpired(cmd="weg", timeout=60, output="", stderr="partial")

    monkeypatch.setattr("runtime.adapters.weg_adapter.subprocess.run", fake_timeout)

    adapter = WegAdapter(timeout=60, catalog_path=catalog)
    with pytest.raises((TimeoutError, RuntimeError), match="timed out"):
        adapter.generate(wallpaper, output_dir)


# ---------------------------------------------------------------------------
# 6. Raises on missing wallpaper before subprocess
# ---------------------------------------------------------------------------


def test_weg_adapter_raises_on_missing_wallpaper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = _make_catalog_file(tmp_path)
    fake_wallpaper = tmp_path / "nope.png"
    fixture = _copy_wallpaper_fixture(tmp_path)
    eh = effects_entry_hash(hash_file(fixture), hash_file(catalog))
    output_dir = tmp_path / "state" / "cache" / "effects" / eh

    mock_run = MagicMock()
    monkeypatch.setattr("runtime.adapters.weg_adapter.subprocess.run", mock_run)

    adapter = WegAdapter(catalog_path=catalog)
    with pytest.raises(FileNotFoundError):
        adapter.generate(fake_wallpaper, output_dir)
    mock_run.assert_not_called()

    dir_path = tmp_path / "adir"
    dir_path.mkdir()
    with pytest.raises(IsADirectoryError):
        adapter.generate(dir_path, output_dir)
    mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# 7. Entry hash mismatch
# ---------------------------------------------------------------------------


def test_weg_adapter_entry_hash_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wallpaper = _copy_wallpaper_fixture(tmp_path)
    catalog = _make_catalog_file(tmp_path)
    wrong = "0" * 64
    output_dir = tmp_path / "state" / "cache" / "effects" / wrong

    fake = _fake_success_factory()
    monkeypatch.setattr("runtime.adapters.weg_adapter.subprocess.run", fake)

    adapter = WegAdapter(catalog_path=catalog)
    with pytest.raises(ValueError, match="hash mismatch"):
        adapter.generate(wallpaper, output_dir)
    fake.assert_not_called()


# ---------------------------------------------------------------------------
# 8. Artifact hashes are hex64, hash_algorithm literal
# ---------------------------------------------------------------------------


def test_weg_adapter_artifact_hashes_are_hex64(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wallpaper = _copy_wallpaper_fixture(tmp_path)
    catalog = _make_catalog_file(tmp_path)
    eh = effects_entry_hash(hash_file(wallpaper), hash_file(catalog))
    output_dir = tmp_path / "state" / "cache" / "effects" / eh

    fake = _fake_success_factory()
    monkeypatch.setattr("runtime.adapters.weg_adapter.subprocess.run", fake)

    adapter = WegAdapter(catalog_path=catalog)
    entry = adapter.generate(wallpaper, output_dir)

    assert entry.hash_algorithm == "sha256"
    assert entry.hash_algorithm == HASH_ALGORITHM
    assert entry.kind == "effects"
    for h in entry.artifact_hashes.values():
        assert len(h) == 64
        assert h == h.lower()
        assert all(c in "0123456789abcdef" for c in h)
    assert list((tmp_path / "state" / "cache").glob(".staging-*")) == []


# ---------------------------------------------------------------------------
# 9. is_available via shutil.which
# ---------------------------------------------------------------------------


def test_weg_adapter_is_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("runtime.adapters.weg_adapter.shutil.which", lambda x: "/usr/bin/weg")
    monkeypatch.setattr("runtime.adapters.weg_adapter.os.access", lambda p, mode: True)
    assert WegAdapter().is_available() is True
    monkeypatch.setattr("runtime.adapters.weg_adapter.shutil.which", lambda x: None)
    assert WegAdapter().is_available() is False
    monkeypatch.setattr("runtime.adapters.weg_adapter.shutil.which", lambda x: None)
    assert WegAdapter(weg_bin="/nonexistent/weg").is_available() is False


# ---------------------------------------------------------------------------
# 10. No settings.toml rewrite even on container (duplicate guard)
# ---------------------------------------------------------------------------


def test_weg_adapter_no_settings_toml_rewrite_even_on_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wallpaper = _copy_wallpaper_fixture(tmp_path)
    catalog = _make_catalog_file(tmp_path)
    eh = effects_entry_hash(hash_file(wallpaper), hash_file(catalog))
    output_dir = tmp_path / "state" / "cache" / "effects" / eh

    candidates = [
        Path(__file__).resolve().parents[4]
        / "src"
        / "cli-tools"
        / "wallpaper-effects-generator"
        / "defaults"
        / "settings.toml",
        Path(__file__).resolve().parents[3]
        / "src"
        / "cli-tools"
        / "wallpaper-effects-generator"
        / "defaults"
        / "settings.toml",
    ]
    real_settings = next((p for p in candidates if p.is_file()), None)
    if real_settings is not None:
        mtime = real_settings.stat().st_mtime_ns
        content = real_settings.read_bytes()
    else:
        settings = tmp_path / "settings.toml"
        settings.write_text("overwrite = false\n")
        real_settings = settings
        mtime = settings.stat().st_mtime_ns
        content = settings.read_bytes()

    captured_env: dict[str, str] = {}
    captured_args: list[str] = []

    def fake_container_run(
        args: list[str],
        capture_output: bool = False,
        text: bool = False,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess[str]:  # noqa: ARG001
        assert env is not None
        captured_env.update(env)
        captured_args.extend(args)
        out = Path(env["WALLPAPER__OUTPUT__DIRECTORY"])
        out.mkdir(parents=True, exist_ok=True)
        (out / "x.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("runtime.adapters.weg_adapter.subprocess.run", fake_container_run)

    adapter = WegAdapter(catalog_path=catalog)
    adapter.generate(wallpaper, output_dir)

    assert real_settings.stat().st_mtime_ns == mtime
    assert real_settings.read_bytes() == content
    assert captured_env["WALLPAPER__OUTPUT__DIRECTORY"] == str(output_dir)
    assert "-o" not in captured_args
    assert "--output" not in captured_args
