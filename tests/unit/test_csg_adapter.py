"""Unit tests for CsgAdapter env override (Story 1.7, AD-7).

Covers ACs 1-5: env override literal keys, no settings.toml edit,
container passthrough, timeout, error preservation, entry-hash validation,
artifact hash hex, is_available.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from runtime.adapters.csg_adapter import CsgAdapter
from runtime.adapters.hashing import (
    HASH_ALGORITHM,
    canonical_hash_dir,
    hash_file,
    palette_entry_hash,
)


def _make_templates_dir(tmp_path: Path) -> Path:
    """Create a minimal templates dir with one file for deterministic hash."""
    tdir = tmp_path / "templates"
    tdir.mkdir(parents=True)
    (tdir / "colors.yaml.j2").write_text("background: {{ bg }}\n")
    return tdir


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
    """Return a fake subprocess.run that writes six artifacts into env out dir."""

    def fake_run(
        args: list[str],
        capture_output: bool = False,  # noqa: ARG001
        text: bool = False,  # noqa: ARG001
        env: dict[str, str] | None = None,
        timeout: int | None = None,  # noqa: ARG001
    ) -> subprocess.CompletedProcess[str]:
        assert env is not None
        out = Path(env["COLORSCHEME__OUTPUT__DIRECTORY"])
        out.mkdir(parents=True, exist_ok=True)
        (out / "colors.yaml").write_text('{"background":"#000"}\n')
        (out / "colors.conf").write_text("$color0 = #000\n")
        (out / "colors.gtk.css").write_text("@define-color bg #000;\n")
        (out / "colors.adw.css").write_text("@define-color window_bg_bg #000;\n")
        (out / "colors.sequences").write_bytes(b"\x1b]4;0;#000\x1b\\")
        (out / "colors.rasi").write_text("* { background: #000; }\n")
        return subprocess.CompletedProcess(args, 0, "", "")

    return MagicMock(side_effect=fake_run)


# ---------------------------------------------------------------------------
# 1. Env override writes to output_dir, returns PaletteEntry
# ---------------------------------------------------------------------------


def test_csg_generate_env_override_writes_to_output_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wallpaper = _copy_wallpaper_fixture(tmp_path)
    templates_dir = _make_templates_dir(tmp_path)
    template_hash = canonical_hash_dir(templates_dir)
    wallpaper_hash = hash_file(wallpaper)
    ph = palette_entry_hash(wallpaper_hash, template_hash)
    output_dir = tmp_path / "state" / "cache" / "palettes" / ph

    fake = _fake_success_factory()
    monkeypatch.setattr("runtime.adapters.csg_adapter.subprocess.run", fake)

    adapter = CsgAdapter(templates_dir=templates_dir)
    entry = adapter.generate(wallpaper, output_dir)

    # PaletteEntry fields
    assert entry.hash_algorithm == HASH_ALGORITHM == "sha256"
    assert entry.kind == "palette"
    assert entry.entry_hash == ph
    assert entry.source_wallpaper_hash == wallpaper_hash
    assert entry.input_template_hash == template_hash
    # artifact_hashes via underscored TypedDict keys
    assert entry.artifact_hashes["colors_yaml"] == hash_file(output_dir / "colors.yaml")
    assert entry.artifact_hashes["colors_conf"] == hash_file(output_dir / "colors.conf")
    assert entry.artifact_hashes["colors_gtk_css"] == hash_file(output_dir / "colors.gtk.css")
    assert entry.artifact_hashes["colors_adw_css"] == hash_file(output_dir / "colors.adw.css")
    assert entry.artifact_hashes["colors_sequences"] == hash_file(output_dir / "colors.sequences")
    assert entry.artifact_hashes["colors_rasi"] == hash_file(output_dir / "colors.rasi")
    # Files exist
    assert (output_dir / "colors.yaml").is_file()
    assert (output_dir / "colors.conf").is_file()
    assert (output_dir / "colors.gtk.css").is_file()
    assert (output_dir / "colors.adw.css").is_file()
    assert (output_dir / "colors.sequences").is_file()
    assert (output_dir / "colors.rasi").is_file()
    # Env keys literal, no -o flag
    assert fake.call_count == 1
    # Extract args and env from mock call
    _, kwargs = fake.call_args
    env_passed = kwargs.get("env") or fake.call_args.kwargs.get("env")
    args_passed = kwargs.get("args") or (fake.call_args.args[0] if fake.call_args.args else None)
    # Fallback: inspect via call_args
    if args_passed is None:
        args_passed = fake.call_args[0][0]
    assert env_passed is not None
    assert env_passed["COLORSCHEME__OUTPUT__DIRECTORY"] == str(output_dir)
    assert env_passed["COLORSCHEME__OUTPUT__OVERWRITE"] == "true"
    # Verify no -o / --output flag in args
    assert "-o" not in args_passed
    assert "--output" not in args_passed
    assert "--format" in args_passed
    # Ensure all six formats present
    assert args_passed.count("--format") == 6
    assert args_passed[args_passed.index("--format") + 1] == "yaml"
    assert args_passed[args_passed.index("--format") + 3] == "conf"
    assert args_passed[args_passed.index("--format") + 5] == "gtk.css"
    assert args_passed[args_passed.index("--format") + 7] == "adw.css"
    assert args_passed[args_passed.index("--format") + 9] == "sequences"
    assert args_passed[args_passed.index("--format") + 11] == "rasi"
    # Verify no staging leak
    cache_root = tmp_path / "state" / "cache"
    assert list(cache_root.glob(".staging-*")) == []


# ---------------------------------------------------------------------------
# 2. Never touches settings.toml
# ---------------------------------------------------------------------------


def test_csg_adapter_never_touches_settings_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wallpaper = _copy_wallpaper_fixture(tmp_path)
    templates_dir = _make_templates_dir(tmp_path)
    ph = palette_entry_hash(hash_file(wallpaper), canonical_hash_dir(templates_dir))
    output_dir = tmp_path / "state" / "cache" / "palettes" / ph

    # Check real provisioning settings.toml if it exists (AD-7: never edits)
    # Fall back to tmp sentinel if real file not found — but now test proves
    # adapter never touches the *actual* settings path, not just a tmp file.
    candidates = [
        Path(__file__).resolve().parents[4]
        / "src"
        / "cli-tools"
        / "color-scheme-generator"
        / "src"
        / "color_scheme_generator"
        / "defaults"
        / "settings.toml",
        Path(__file__).resolve().parents[3]
        / "src"
        / "cli-tools"
        / "color-scheme-generator"
        / "defaults"
        / "settings.toml",
    ]
    real_settings = next((p for p in candidates if p.is_file()), None)
    if real_settings is not None:
        mtime_before = real_settings.stat().st_mtime_ns
        content_before = real_settings.read_bytes()
    else:
        # No real settings — use tmp sentinel as fallback proof of no global write
        real_settings = tmp_path / "settings.toml"
        real_settings.write_text('[color_scheme]\nbackend = "custom"\n')
        mtime_before = real_settings.stat().st_mtime_ns
        content_before = real_settings.read_bytes()

    fake = _fake_success_factory()
    monkeypatch.setattr("runtime.adapters.csg_adapter.subprocess.run", fake)

    adapter = CsgAdapter(templates_dir=templates_dir)
    adapter.generate(wallpaper, output_dir)

    assert real_settings.stat().st_mtime_ns == mtime_before
    assert real_settings.read_bytes() == content_before


# ---------------------------------------------------------------------------
# 3. Container env passthrough (host-only env would fail)
# ---------------------------------------------------------------------------


def test_csg_adapter_container_env_passthrough(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    wallpaper = _copy_wallpaper_fixture(tmp_path)
    templates_dir = _make_templates_dir(tmp_path)
    ph = palette_entry_hash(hash_file(wallpaper), canonical_hash_dir(templates_dir))
    output_dir = tmp_path / "staging" / ph

    # Ensure host env does NOT contain the key before call
    monkeypatch.delenv("COLORSCHEME__OUTPUT__DIRECTORY", raising=False)
    assert "COLORSCHEME__OUTPUT__DIRECTORY" not in os.environ

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
        out = Path(env["COLORSCHEME__OUTPUT__DIRECTORY"])
        out.mkdir(parents=True, exist_ok=True)
        (out / "colors.yaml").write_text("yaml")
        (out / "colors.conf").write_text("conf")
        (out / "colors.gtk.css").write_text("css")
        (out / "colors.adw.css").write_text("adw")
        (out / "colors.sequences").write_text("seq")
        (out / "colors.rasi").write_text("rasi")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("runtime.adapters.csg_adapter.subprocess.run", fake_run)

    adapter = CsgAdapter(templates_dir=templates_dir)
    adapter.generate(wallpaper, output_dir)

    assert captured["COLORSCHEME__OUTPUT__DIRECTORY"] == str(output_dir)
    # Verify host env still clean after call (no global mutation)
    assert "COLORSCHEME__OUTPUT__DIRECTORY" not in os.environ


# ---------------------------------------------------------------------------
# 4. Raises on non-zero exit, preserves stderr
# ---------------------------------------------------------------------------


def test_csg_adapter_raises_on_non_zero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wallpaper = _copy_wallpaper_fixture(tmp_path)
    templates_dir = _make_templates_dir(tmp_path)
    ph = palette_entry_hash(hash_file(wallpaper), canonical_hash_dir(templates_dir))
    output_dir = tmp_path / "state" / "cache" / "palettes" / ph

    def fake_fail(
        args: list[str],
        capture_output: bool = False,
        text: bool = False,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess[str]:  # noqa: ARG001
        return subprocess.CompletedProcess(args, 1, "", "backend not available")

    monkeypatch.setattr("runtime.adapters.csg_adapter.subprocess.run", fake_fail)

    adapter = CsgAdapter(templates_dir=templates_dir)
    with pytest.raises(RuntimeError, match="backend not available"):
        adapter.generate(wallpaper, output_dir)


# ---------------------------------------------------------------------------
# 5. Timeout raises
# ---------------------------------------------------------------------------


def test_csg_adapter_timeout_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wallpaper = _copy_wallpaper_fixture(tmp_path)
    templates_dir = _make_templates_dir(tmp_path)
    ph = palette_entry_hash(hash_file(wallpaper), canonical_hash_dir(templates_dir))
    output_dir = tmp_path / "state" / "cache" / "palettes" / ph

    def fake_timeout(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:  # noqa: ARG001
        raise subprocess.TimeoutExpired(cmd="csg", timeout=60, output="", stderr="partial")

    monkeypatch.setattr("runtime.adapters.csg_adapter.subprocess.run", fake_timeout)

    adapter = CsgAdapter(timeout=60, templates_dir=templates_dir)
    with pytest.raises((TimeoutError, RuntimeError), match="timed out"):
        adapter.generate(wallpaper, output_dir)


# ---------------------------------------------------------------------------
# 6. Raises on missing wallpaper before subprocess
# ---------------------------------------------------------------------------


def test_csg_adapter_raises_on_missing_wallpaper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    templates_dir = _make_templates_dir(tmp_path)
    # Compute a deterministic ph for output_dir, but wallpaper missing
    # Use fixture hash to compute expected ph, but wallpaper path missing
    fake_wallpaper = tmp_path / "nope.png"
    # Need ph for output_dir — we fake one from known hash but use missing file's hash?
    # Instead compute ph from fixture hash and use it for output_dir, but call with missing path
    fixture = _copy_wallpaper_fixture(tmp_path)
    ph = palette_entry_hash(hash_file(fixture), canonical_hash_dir(templates_dir))
    output_dir = tmp_path / "state" / "cache" / "palettes" / ph

    mock_run = MagicMock()
    monkeypatch.setattr("runtime.adapters.csg_adapter.subprocess.run", mock_run)

    adapter = CsgAdapter(templates_dir=templates_dir)
    with pytest.raises(FileNotFoundError):
        adapter.generate(fake_wallpaper, output_dir)
    mock_run.assert_not_called()

    # Also test IsADirectoryError
    dir_path = tmp_path / "adir"
    dir_path.mkdir()
    with pytest.raises(IsADirectoryError):
        adapter.generate(dir_path, output_dir)
    mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# 7. Entry hash mismatch
# ---------------------------------------------------------------------------


def test_csg_adapter_entry_hash_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wallpaper = _copy_wallpaper_fixture(tmp_path)
    templates_dir = _make_templates_dir(tmp_path)
    # Create output_dir with wrong hash (all zeros)
    wrong = "0" * 64
    output_dir = tmp_path / "state" / "cache" / "palettes" / wrong

    fake = _fake_success_factory()
    monkeypatch.setattr("runtime.adapters.csg_adapter.subprocess.run", fake)

    adapter = CsgAdapter(templates_dir=templates_dir)
    with pytest.raises(ValueError, match="hash mismatch"):
        adapter.generate(wallpaper, output_dir)
    fake.assert_not_called()


# ---------------------------------------------------------------------------
# 8. Artifact hashes are hex64, hash_algorithm literal
# ---------------------------------------------------------------------------


def test_csg_adapter_artifact_hashes_are_hex64(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wallpaper = _copy_wallpaper_fixture(tmp_path)
    templates_dir = _make_templates_dir(tmp_path)
    ph = palette_entry_hash(hash_file(wallpaper), canonical_hash_dir(templates_dir))
    output_dir = tmp_path / "state" / "cache" / "palettes" / ph

    fake = _fake_success_factory()
    monkeypatch.setattr("runtime.adapters.csg_adapter.subprocess.run", fake)

    adapter = CsgAdapter(templates_dir=templates_dir)
    entry = adapter.generate(wallpaper, output_dir)

    assert entry.hash_algorithm == "sha256"
    assert entry.hash_algorithm == HASH_ALGORITHM
    assert entry.kind == "palette"
    for key in (
        "colors_yaml",
        "colors_conf",
        "colors_gtk_css",
        "colors_adw_css",
        "colors_sequences",
        "colors_rasi",
    ):
        h = entry.artifact_hashes[key]  # type: ignore[literal-required]
        assert len(h) == 64
        assert h == h.lower()
        assert all(c in "0123456789abcdef" for c in h)
    # No staging dir created directly by adapter
    assert list((tmp_path / "state" / "cache").glob(".staging-*")) == []


# ---------------------------------------------------------------------------
# 9. is_available via shutil.which
# ---------------------------------------------------------------------------


def test_csg_adapter_is_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("runtime.adapters.csg_adapter.shutil.which", lambda x: "/usr/bin/csg")
    monkeypatch.setattr("runtime.adapters.csg_adapter.os.access", lambda p, mode: True)
    assert CsgAdapter().is_available() is True
    monkeypatch.setattr("runtime.adapters.csg_adapter.shutil.which", lambda x: None)
    assert CsgAdapter().is_available() is False
    # With explicit bin path containing separator
    monkeypatch.setattr("runtime.adapters.csg_adapter.shutil.which", lambda x: None)
    # Path exists case — create temp file to simulate bin
    # Use current file as dummy
    assert CsgAdapter(csg_bin="/nonexistent/csg").is_available() is False


# ---------------------------------------------------------------------------
# 10. No settings.toml rewrite even on container (duplicate guard)
# ---------------------------------------------------------------------------


def test_csg_adapter_no_settings_toml_rewrite_even_on_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wallpaper = _copy_wallpaper_fixture(tmp_path)
    templates_dir = _make_templates_dir(tmp_path)
    ph = palette_entry_hash(hash_file(wallpaper), canonical_hash_dir(templates_dir))
    output_dir = tmp_path / "state" / "cache" / "palettes" / ph

    candidates = [
        Path(__file__).resolve().parents[4]
        / "src"
        / "cli-tools"
        / "color-scheme-generator"
        / "src"
        / "color_scheme_generator"
        / "defaults"
        / "settings.toml",
        Path(__file__).resolve().parents[3]
        / "src"
        / "cli-tools"
        / "color-scheme-generator"
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
        # Simulate container mode: still writes to host env path
        out = Path(env["COLORSCHEME__OUTPUT__DIRECTORY"])
        out.mkdir(parents=True, exist_ok=True)
        (out / "colors.yaml").write_text("yaml")
        (out / "colors.conf").write_text("conf")
        (out / "colors.gtk.css").write_text("gtk")
        (out / "colors.adw.css").write_text("adw")
        (out / "colors.sequences").write_text("seq")
        (out / "colors.rasi").write_text("rasi")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("runtime.adapters.csg_adapter.subprocess.run", fake_container_run)

    adapter = CsgAdapter(templates_dir=templates_dir)
    adapter.generate(wallpaper, output_dir)

    assert real_settings.stat().st_mtime_ns == mtime
    assert real_settings.read_bytes() == content
    assert captured_env["COLORSCHEME__OUTPUT__DIRECTORY"] == str(output_dir)
    assert captured_env["COLORSCHEME__OUTPUT__OVERWRITE"] == "true"
    # Verify no -o/--output flag in args (not in env dict)
    assert "-o" not in captured_args
    assert "--output" not in captured_args
