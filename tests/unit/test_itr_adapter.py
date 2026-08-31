"""Unit tests for ItrAdapter env override (Story 1.9, AD-7).

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
    canonical_hash_dir,
    hash_file,
    icons_entry_hash,
)
from runtime.adapters.itr_adapter import ItrAdapter


def _make_templates_dir(tmp_path: Path) -> Path:
    tdir = tmp_path / "templates"
    tdir.mkdir(parents=True)
    (tdir / "icon.svg.j2").write_text('<svg><path fill="{{background}}"/></svg>\n')
    (tdir / "subdir").mkdir()
    (tdir / "subdir" / "second.svg.j2").write_text("<svg></svg>\n")
    return tdir


def _make_mappings_dir(tmp_path: Path) -> Path:
    mdir = tmp_path / "mappings"
    mdir.mkdir(parents=True)
    (mdir / "icons.yaml").write_text(
        "battery:\n  template_dir: battery/\n  output_dir: out/\n  variants: []\n"
    )
    return mdir


def _make_mappings_file(tmp_path: Path) -> Path:
    f = tmp_path / "mappings.yaml"
    f.write_text("battery:\n  variants: []\n")
    return f


def _create_palette_entry(tmp_path: Path, palette_hash: str) -> Path:
    entry = tmp_path / "state" / "cache" / "palettes" / palette_hash
    entry.mkdir(parents=True, exist_ok=True)
    (entry / "colors.yaml").write_text('special:\n  background: "#000000"\ncolors: []\n')
    return entry


def _fake_success_factory() -> MagicMock:
    def fake_run(
        args: list[str],
        capture_output: bool = False,  # noqa: ARG001
        text: bool = False,  # noqa: ARG001
        env: dict[str, str] | None = None,
        timeout: int | None = None,  # noqa: ARG001
        errors: str | None = None,  # noqa: ARG001
    ) -> subprocess.CompletedProcess[str]:
        assert env is not None
        out = Path(env["ICON_RENDERER__OUTPUT__OUTPUT_DIR"])
        colors = Path(env["ICON_RENDERER__COLOR_SCHEME__PATH"])
        assert colors.is_file(), f"colors.yaml should exist: {colors}"
        out.mkdir(parents=True, exist_ok=True)
        (out / "battery-0.svg").write_text('<svg fill="#000"></svg>\n')
        (out / "wifi.svg").write_text('<svg fill="#111"></svg>\n')
        # nested svg to test relative posix handling
        nested = out / "subdir"
        nested.mkdir(parents=True, exist_ok=True)
        (nested / "nested.svg").write_text("<svg></svg>\n")
        return subprocess.CompletedProcess(args, 0, "", "")

    return MagicMock(side_effect=fake_run)


# ---------------------------------------------------------------------------
# 1. Env override writes to output_dir, returns IconsEntry
# ---------------------------------------------------------------------------


def test_itr_render_env_override_writes_to_output_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    palette_hash = "a" * 64
    templates_dir = _make_templates_dir(tmp_path)
    mappings_path = _make_mappings_dir(tmp_path)
    palette_entry = _create_palette_entry(tmp_path, palette_hash)
    templates_hash = canonical_hash_dir(templates_dir)
    mappings_hash = canonical_hash_dir(mappings_path)
    ih = icons_entry_hash(palette_hash, templates_hash, mappings_hash)
    output_dir = tmp_path / "state" / "cache" / "icons" / ih

    fake = _fake_success_factory()
    monkeypatch.setattr("runtime.adapters.itr_adapter.subprocess.run", fake)

    adapter = ItrAdapter(
        templates_dir=templates_dir,
        mappings_path=mappings_path,
        palette_cache_dir=palette_entry,
    )
    entry = adapter.render(palette_hash, templates_dir, mappings_path, output_dir)

    assert entry.hash_algorithm == HASH_ALGORITHM == "sha256"
    assert entry.kind == "icons"
    assert entry.entry_hash == ih
    assert entry.source_palette_hash == palette_hash
    assert entry.input_templates_hash == templates_hash
    assert entry.input_mappings_hash == mappings_hash
    assert entry.artifact_hashes["battery-0.svg"] == hash_file(output_dir / "battery-0.svg")
    assert entry.artifact_hashes["wifi.svg"] == hash_file(output_dir / "wifi.svg")
    assert (output_dir / "battery-0.svg").is_file()
    assert (output_dir / "wifi.svg").is_file()
    assert (output_dir / "subdir" / "nested.svg").is_file()
    assert fake.call_count == 1
    _, kwargs = fake.call_args
    env_passed = kwargs.get("env")
    args_passed = kwargs.get("args") or (fake.call_args.args[0] if fake.call_args.args else None)
    if args_passed is None:
        args_passed = fake.call_args[0][0]
    assert env_passed is not None
    assert env_passed["ICON_RENDERER__OUTPUT__OUTPUT_DIR"] == str(output_dir)
    assert env_passed["ICON_RENDERER__COLOR_SCHEME__PATH"] == str(palette_entry / "colors.yaml")
    assert "-o" not in args_passed
    assert "--output" not in args_passed
    assert "--output-dir" not in args_passed
    assert "--color-scheme" not in args_passed
    assert "render" in args_passed
    cache_root = tmp_path / "state" / "cache"
    assert list(cache_root.glob(".staging-*")) == []


def test_itr_render_with_mappings_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    palette_hash = "b" * 64
    templates_dir = _make_templates_dir(tmp_path)
    mappings_file = _make_mappings_file(tmp_path)
    palette_entry = _create_palette_entry(tmp_path, palette_hash)
    templates_hash = canonical_hash_dir(templates_dir)
    mappings_hash = hash_file(mappings_file)
    ih = icons_entry_hash(palette_hash, templates_hash, mappings_hash)
    output_dir = tmp_path / "state" / "cache" / "icons" / ih

    fake = _fake_success_factory()
    monkeypatch.setattr("runtime.adapters.itr_adapter.subprocess.run", fake)

    adapter = ItrAdapter(
        templates_dir=templates_dir,
        mappings_path=mappings_file,
        palette_cache_dir=palette_entry,
    )
    entry = adapter.render(palette_hash, templates_dir, mappings_file, output_dir)
    assert entry.input_mappings_hash == mappings_hash
    assert entry.entry_hash == ih


# ---------------------------------------------------------------------------
# 2. Never touches settings.toml
# ---------------------------------------------------------------------------


def test_itr_adapter_never_touches_settings_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    palette_hash = "c" * 64
    templates_dir = _make_templates_dir(tmp_path)
    mappings_path = _make_mappings_dir(tmp_path)
    palette_entry = _create_palette_entry(tmp_path, palette_hash)
    ih = icons_entry_hash(
        palette_hash, canonical_hash_dir(templates_dir), canonical_hash_dir(mappings_path)
    )
    output_dir = tmp_path / "state" / "cache" / "icons" / ih

    candidates = [
        Path(__file__).resolve().parents[4]
        / "src"
        / "cli-tools"
        / "icon-templates-renderer"
        / "src"
        / "icon_templates_renderer"
        / "defaults"
        / "settings.toml",
        Path(__file__).resolve().parents[3]
        / "src"
        / "cli-tools"
        / "icon-templates-renderer"
        / "defaults"
        / "settings.toml",
    ]
    real_settings = next((p for p in candidates if p.is_file()), None)
    if real_settings is not None:
        mtime_before = real_settings.stat().st_mtime_ns
        content_before = real_settings.read_bytes()
    else:
        real_settings = tmp_path / "settings.toml"
        real_settings.write_text('[output]\noutput_dir = "/tmp/itr"\n')
        mtime_before = real_settings.stat().st_mtime_ns
        content_before = real_settings.read_bytes()

    fake = _fake_success_factory()
    monkeypatch.setattr("runtime.adapters.itr_adapter.subprocess.run", fake)

    adapter = ItrAdapter(
        templates_dir=templates_dir,
        mappings_path=mappings_path,
        palette_cache_dir=palette_entry,
    )
    adapter.render(palette_hash, templates_dir, mappings_path, output_dir)

    assert real_settings.stat().st_mtime_ns == mtime_before
    assert real_settings.read_bytes() == content_before


# ---------------------------------------------------------------------------
# 3. Container env passthrough
# ---------------------------------------------------------------------------


def test_itr_adapter_container_env_passthrough(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    palette_hash = "d" * 64
    templates_dir = _make_templates_dir(tmp_path)
    mappings_path = _make_mappings_dir(tmp_path)
    palette_entry = _create_palette_entry(tmp_path, palette_hash)
    ih = icons_entry_hash(
        palette_hash, canonical_hash_dir(templates_dir), canonical_hash_dir(mappings_path)
    )
    output_dir = tmp_path / "staging" / ih

    monkeypatch.delenv("ICON_RENDERER__OUTPUT__OUTPUT_DIR", raising=False)
    monkeypatch.delenv("ICON_RENDERER__COLOR_SCHEME__PATH", raising=False)
    assert "ICON_RENDERER__OUTPUT__OUTPUT_DIR" not in os.environ

    captured: dict[str, str] = {}

    def fake_run(
        args: list[str],
        capture_output: bool = False,
        text: bool = False,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
        errors: str | None = None,
    ) -> subprocess.CompletedProcess[str]:  # noqa: ARG001
        assert env is not None
        captured.update(env)
        out = Path(env["ICON_RENDERER__OUTPUT__OUTPUT_DIR"])
        out.mkdir(parents=True, exist_ok=True)
        (out / "a.svg").write_text("<svg></svg>")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("runtime.adapters.itr_adapter.subprocess.run", fake_run)

    adapter = ItrAdapter(
        templates_dir=templates_dir,
        mappings_path=mappings_path,
        palette_cache_dir=palette_entry,
    )
    adapter.render(palette_hash, templates_dir, mappings_path, output_dir)

    assert captured["ICON_RENDERER__OUTPUT__OUTPUT_DIR"] == str(output_dir)
    assert captured["ICON_RENDERER__COLOR_SCHEME__PATH"] == str(palette_entry / "colors.yaml")
    assert "ICON_RENDERER__OUTPUT__OUTPUT_DIR" not in os.environ


# ---------------------------------------------------------------------------
# 4. Raises on non-zero exit, preserves stderr
# ---------------------------------------------------------------------------


def test_itr_adapter_raises_on_non_zero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    palette_hash = "e" * 64
    templates_dir = _make_templates_dir(tmp_path)
    mappings_path = _make_mappings_dir(tmp_path)
    palette_entry = _create_palette_entry(tmp_path, palette_hash)
    ih = icons_entry_hash(
        palette_hash, canonical_hash_dir(templates_dir), canonical_hash_dir(mappings_path)
    )
    output_dir = tmp_path / "state" / "cache" / "icons" / ih

    def fake_fail(
        args: list[str],
        capture_output: bool = False,
        text: bool = False,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
        errors: str | None = None,
    ) -> subprocess.CompletedProcess[str]:  # noqa: ARG001
        return subprocess.CompletedProcess(args, 1, "", "template not found")

    monkeypatch.setattr("runtime.adapters.itr_adapter.subprocess.run", fake_fail)

    adapter = ItrAdapter(
        templates_dir=templates_dir,
        mappings_path=mappings_path,
        palette_cache_dir=palette_entry,
    )
    with pytest.raises(RuntimeError, match="template not found"):
        adapter.render(palette_hash, templates_dir, mappings_path, output_dir)


# ---------------------------------------------------------------------------
# 5. Timeout raises
# ---------------------------------------------------------------------------


def test_itr_adapter_timeout_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    palette_hash = "f" * 64
    templates_dir = _make_templates_dir(tmp_path)
    mappings_path = _make_mappings_dir(tmp_path)
    palette_entry = _create_palette_entry(tmp_path, palette_hash)
    ih = icons_entry_hash(
        palette_hash, canonical_hash_dir(templates_dir), canonical_hash_dir(mappings_path)
    )
    output_dir = tmp_path / "state" / "cache" / "icons" / ih

    def fake_timeout(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:  # noqa: ARG001
        raise subprocess.TimeoutExpired(cmd="itr", timeout=60, output="", stderr="partial")

    monkeypatch.setattr("runtime.adapters.itr_adapter.subprocess.run", fake_timeout)

    adapter = ItrAdapter(
        timeout=60,
        templates_dir=templates_dir,
        mappings_path=mappings_path,
        palette_cache_dir=palette_entry,
    )
    with pytest.raises((TimeoutError, RuntimeError), match="timed out"):
        adapter.render(palette_hash, templates_dir, mappings_path, output_dir)


# ---------------------------------------------------------------------------
# 6. Raises on missing templates before subprocess
# ---------------------------------------------------------------------------


def test_itr_adapter_raises_on_missing_templates_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    palette_hash = "a1" * 32
    mappings_path = _make_mappings_dir(tmp_path)
    palette_entry = _create_palette_entry(tmp_path, palette_hash)
    # Hash for ih still needs templates_hash, but we will test missing dir
    templates_dir = tmp_path / "nope_templates"
    # compute ih with a real templates dir to get output_dir name, but call with missing
    real_templates = _make_templates_dir(tmp_path)
    ih = icons_entry_hash(
        palette_hash, canonical_hash_dir(real_templates), canonical_hash_dir(mappings_path)
    )
    output_dir = tmp_path / "state" / "cache" / "icons" / ih

    mock_run = MagicMock()
    monkeypatch.setattr("runtime.adapters.itr_adapter.subprocess.run", mock_run)

    adapter = ItrAdapter(mappings_path=mappings_path, palette_cache_dir=palette_entry)
    # Pass missing templates_dir via render arg (overrides constructor None)
    with pytest.raises(FileNotFoundError):
        adapter.render(palette_hash, templates_dir, mappings_path, output_dir)
    mock_run.assert_not_called()


def test_itr_adapter_raises_on_missing_mappings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    palette_hash = "b2" * 32
    templates_dir = _make_templates_dir(tmp_path)
    palette_entry = _create_palette_entry(tmp_path, palette_hash)
    real_mappings = _make_mappings_dir(tmp_path)
    ih = icons_entry_hash(
        palette_hash, canonical_hash_dir(templates_dir), canonical_hash_dir(real_mappings)
    )
    output_dir = tmp_path / "state" / "cache" / "icons" / ih
    missing = tmp_path / "nope_mappings"

    mock_run = MagicMock()
    monkeypatch.setattr("runtime.adapters.itr_adapter.subprocess.run", mock_run)

    adapter = ItrAdapter(templates_dir=templates_dir, palette_cache_dir=palette_entry)
    with pytest.raises(FileNotFoundError):
        adapter.render(palette_hash, templates_dir, missing, output_dir)
    mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# 7. Entry hash mismatch
# ---------------------------------------------------------------------------


def test_itr_adapter_entry_hash_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    palette_hash = "c3" * 32
    templates_dir = _make_templates_dir(tmp_path)
    mappings_path = _make_mappings_dir(tmp_path)
    palette_entry = _create_palette_entry(tmp_path, palette_hash)
    wrong = "0" * 64
    output_dir = tmp_path / "state" / "cache" / "icons" / wrong

    fake = _fake_success_factory()
    monkeypatch.setattr("runtime.adapters.itr_adapter.subprocess.run", fake)

    adapter = ItrAdapter(
        templates_dir=templates_dir,
        mappings_path=mappings_path,
        palette_cache_dir=palette_entry,
    )
    with pytest.raises(ValueError, match="hash mismatch"):
        adapter.render(palette_hash, templates_dir, mappings_path, output_dir)
    fake.assert_not_called()


# ---------------------------------------------------------------------------
# 8. Artifact hashes are hex64, hash_algorithm literal
# ---------------------------------------------------------------------------


def test_itr_adapter_artifact_hashes_are_hex64(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    palette_hash = "d4" * 32
    templates_dir = _make_templates_dir(tmp_path)
    mappings_path = _make_mappings_dir(tmp_path)
    palette_entry = _create_palette_entry(tmp_path, palette_hash)
    ih = icons_entry_hash(
        palette_hash, canonical_hash_dir(templates_dir), canonical_hash_dir(mappings_path)
    )
    output_dir = tmp_path / "state" / "cache" / "icons" / ih

    fake = _fake_success_factory()
    monkeypatch.setattr("runtime.adapters.itr_adapter.subprocess.run", fake)

    adapter = ItrAdapter(
        templates_dir=templates_dir,
        mappings_path=mappings_path,
        palette_cache_dir=palette_entry,
    )
    entry = adapter.render(palette_hash, templates_dir, mappings_path, output_dir)

    assert entry.hash_algorithm == "sha256"
    assert entry.hash_algorithm == HASH_ALGORITHM
    assert entry.kind == "icons"
    for h in entry.artifact_hashes.values():
        assert len(h) == 64
        assert h == h.lower()
        assert all(c in "0123456789abcdef" for c in h)
    assert list((tmp_path / "state" / "cache").glob(".staging-*")) == []


# ---------------------------------------------------------------------------
# 9. is_available via shutil.which
# ---------------------------------------------------------------------------


def test_itr_adapter_is_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("runtime.adapters.itr_adapter.shutil.which", lambda x: "/usr/bin/itr")
    monkeypatch.setattr("runtime.adapters.itr_adapter.os.access", lambda p, mode: True)
    assert ItrAdapter().is_available() is True
    monkeypatch.setattr("runtime.adapters.itr_adapter.shutil.which", lambda x: None)
    assert ItrAdapter().is_available() is False
    assert ItrAdapter(itr_bin="/nonexistent/itr").is_available() is False


# ---------------------------------------------------------------------------
# 10. No settings.toml rewrite even on container
# ---------------------------------------------------------------------------


def test_itr_adapter_no_settings_toml_rewrite_even_on_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    palette_hash = "e5" * 32
    templates_dir = _make_templates_dir(tmp_path)
    mappings_path = _make_mappings_dir(tmp_path)
    palette_entry = _create_palette_entry(tmp_path, palette_hash)
    ih = icons_entry_hash(
        palette_hash, canonical_hash_dir(templates_dir), canonical_hash_dir(mappings_path)
    )
    output_dir = tmp_path / "state" / "cache" / "icons" / ih

    candidates = [
        Path(__file__).resolve().parents[4]
        / "src"
        / "cli-tools"
        / "icon-templates-renderer"
        / "src"
        / "icon_templates_renderer"
        / "defaults"
        / "settings.toml",
        Path(__file__).resolve().parents[3]
        / "src"
        / "cli-tools"
        / "icon-templates-renderer"
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
        errors: str | None = None,
    ) -> subprocess.CompletedProcess[str]:  # noqa: ARG001
        assert env is not None
        captured_env.update(env)
        captured_args.extend(args)
        out = Path(env["ICON_RENDERER__OUTPUT__OUTPUT_DIR"])
        out.mkdir(parents=True, exist_ok=True)
        (out / "x.svg").write_text("<svg></svg>")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("runtime.adapters.itr_adapter.subprocess.run", fake_container_run)

    adapter = ItrAdapter(
        templates_dir=templates_dir,
        mappings_path=mappings_path,
        palette_cache_dir=palette_entry,
    )
    adapter.render(palette_hash, templates_dir, mappings_path, output_dir)

    assert real_settings.stat().st_mtime_ns == mtime
    assert real_settings.read_bytes() == content
    assert captured_env["ICON_RENDERER__OUTPUT__OUTPUT_DIR"] == str(output_dir)
    assert captured_env["ICON_RENDERER__COLOR_SCHEME__PATH"] == str(palette_entry / "colors.yaml")
    assert "-o" not in captured_args
    assert "--output" not in captured_args
