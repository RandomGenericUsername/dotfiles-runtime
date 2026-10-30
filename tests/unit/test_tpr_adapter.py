"""TprAdapter: argv/env contract, artifact copy set, and cache-key fingerprints."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from runtime.adapters.csg_adapter import _CsgRunResult
from runtime.adapters.hashing import (
    canonical_hash_dir,
    hash_file,
    palette_entry_hash,
    palette_producer_seed,
)
from runtime.adapters.tpr_adapter import TPR_VERSION_FALLBACK, TprAdapter
from runtime.domain.models import PALETTE_ARTIFACT_NAMES

_EXPECTED_FORMAT_ARGS = [
    "--format",
    "yaml",
    "--format",
    "conf",
    "--format",
    "gtk.css",
    "--format",
    "adw.css",
    "--format",
    "sequences",
    "--format",
    "rasi",
    "--format",
    "kitty",
    "--format",
    "btop.theme",
]


def _make_templates_dir(tmp_path: Path) -> Path:
    tdir = tmp_path / "tpr-templates"
    tdir.mkdir(parents=True)
    (tdir / "colors.yaml.j2").write_text("background: {{ bg }}\n")
    return tdir


def _copy_wallpaper_fixture(tmp_path: Path) -> Path:
    fixture = Path(__file__).parent.parent / "fixtures" / "wallpaper.png"
    assert fixture.is_file(), f"wallpaper.png fixture not found: {fixture}"
    dest = tmp_path / "wallpaper.png"
    dest.write_bytes(fixture.read_bytes())
    return dest


def _runner(
    captured: dict[str, object],
    *,
    include_theme_json: bool = True,
    skip: str | None = None,
) -> MagicMock:
    def fake_run(args: list[str], env: dict[str, str], timeout: int) -> _CsgRunResult:  # noqa: ARG001
        captured["args"] = list(args)
        captured["env"] = dict(env)
        out = Path(env["THEME__OUTPUT__DIRECTORY"])
        out.mkdir(parents=True, exist_ok=True)
        for name in PALETTE_ARTIFACT_NAMES:
            if name == skip:
                continue
            (out / name).write_text(f"{name} from tpr\n")
        if include_theme_json:
            (out / "theme.json").write_text('{"schemaVersion": 1}\n')
        return _CsgRunResult(
            returncode=0, stdout="", stderr="", timed_out=False, elapsed_seconds=0.01
        )

    return MagicMock(side_effect=fake_run)


def _adapter(tmp_path: Path, **kwargs: object) -> TprAdapter:
    return TprAdapter(
        templates_dir=_make_templates_dir(tmp_path),
        engine="native",
        version="v0.1.0",
        **kwargs,  # type: ignore[arg-type]
    )


def _output_dir(tmp_path: Path, adapter: TprAdapter, wallpaper_hash: str) -> Path:
    entry_hash = adapter.expected_entry_hash(wallpaper_hash)
    return tmp_path / "state" / "cache" / "palettes" / entry_hash


def test_generate_invokes_contract_and_copies_only_eight_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wallpaper = _copy_wallpaper_fixture(tmp_path)
    adapter = _adapter(tmp_path)
    wallpaper_hash = hash_file(wallpaper)
    output_dir = _output_dir(tmp_path, adapter, wallpaper_hash)
    captured: dict[str, object] = {}
    monkeypatch.setattr("runtime.adapters.tpr_adapter._run_csg", _runner(captured))

    entry = adapter.generate(wallpaper, output_dir)

    args = captured["args"]
    env = captured["env"]
    assert isinstance(args, list) and isinstance(env, dict)
    assert args == ["tpr", "generate", str(wallpaper), *_EXPECTED_FORMAT_ARGS]
    assert "-o" not in args and "--output" not in args
    assert env["THEME__OUTPUT__OVERWRITE"] == "true"
    assert env["THEME__GENERATION__ENGINE"] == "native"
    # REQ-005: container is the production default, never a forced local mode.
    assert env["THEME__RUNTIME__MODE"] == "container"
    assert env["THEME__TEMPLATES__DIRECTORY"] == str(adapter.templates_dir)
    assert Path(env["THEME__OUTPUT__DIRECTORY"]) != output_dir

    # Exactly the 8 recorded artifacts land in the palette entry.
    assert sorted(p.name for p in output_dir.iterdir()) == sorted(PALETTE_ARTIFACT_NAMES)
    assert not (output_dir / "theme.json").exists()

    assert entry.entry_hash == adapter.expected_entry_hash(wallpaper_hash)
    assert entry.input_template_hash == canonical_hash_dir(adapter.templates_dir)  # type: ignore[arg-type]
    for name in PALETTE_ARTIFACT_NAMES:
        assert (output_dir / name).is_file()


def test_explicit_local_runtime_is_available_for_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wallpaper = _copy_wallpaper_fixture(tmp_path)
    adapter = _adapter(tmp_path, runtime="local")
    output_dir = _output_dir(tmp_path, adapter, hash_file(wallpaper))
    captured: dict[str, object] = {}
    monkeypatch.setattr("runtime.adapters.tpr_adapter._run_csg", _runner(captured))

    adapter.generate(wallpaper, output_dir)

    env = captured["env"]
    assert isinstance(env, dict)
    assert env["THEME__RUNTIME__MODE"] == "local"


def test_invalid_runtime_fails_loud(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="tpr runtime"):
        TprAdapter(templates_dir=_make_templates_dir(tmp_path), runtime="vm")


def test_provisioned_settings_are_pinned_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wallpaper = _copy_wallpaper_fixture(tmp_path)
    settings = tmp_path / "settings.toml"
    settings.write_text('[runtime]\nmode = "container"\n', encoding="utf-8")
    adapter = _adapter(tmp_path, settings_path=settings)
    output_dir = _output_dir(tmp_path, adapter, hash_file(wallpaper))
    captured: dict[str, object] = {}
    monkeypatch.setattr("runtime.adapters.tpr_adapter._run_csg", _runner(captured))

    adapter.generate(wallpaper, output_dir)

    env = captured["env"]
    assert isinstance(env, dict)
    assert env["THEME_CONFIG_FILE_PATH"] == str(settings)


def test_theme_json_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wallpaper = _copy_wallpaper_fixture(tmp_path)
    adapter = _adapter(tmp_path)
    output_dir = _output_dir(tmp_path, adapter, hash_file(wallpaper))
    monkeypatch.setattr("runtime.adapters.tpr_adapter._run_csg", _runner({}))

    entry = adapter.generate(wallpaper, output_dir)

    assert "theme.json" not in entry.artifact_hashes
    assert not (output_dir / "theme.json").exists()


def test_missing_artifact_fails_loudly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wallpaper = _copy_wallpaper_fixture(tmp_path)
    adapter = _adapter(tmp_path)
    output_dir = _output_dir(tmp_path, adapter, hash_file(wallpaper))
    monkeypatch.setattr(
        "runtime.adapters.tpr_adapter._run_csg", _runner({}, skip="colors.kitty")
    )

    from runtime.domain.models import PaletteUnavailableError

    with pytest.raises(PaletteUnavailableError, match="tpr did not write expected artifact") as excinfo:
        adapter.generate(wallpaper, output_dir)

    exc = excinfo.value
    assert exc.tool == "tpr"
    assert exc.returncode == 0
    assert exc.missing_artifacts == ("colors.kitty",)
    assert exc.timed_out is False
    assert exc.elapsed_seconds is not None


def test_missing_templates_dir_fails_clearly(tmp_path: Path) -> None:
    adapter = TprAdapter(templates_dir=tmp_path / "missing", version="v0.1.0")
    with pytest.raises(FileNotFoundError, match="TPR templates dir not found"):
        adapter.template_set_hash()


def test_empty_templates_dir_fails_clearly(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    adapter = TprAdapter(templates_dir=empty, version="v0.1.0")
    with pytest.raises(RuntimeError, match="templates dir is empty"):
        adapter.template_set_hash()


def test_version_probe_takes_last_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = TprAdapter(templates_dir=_make_templates_dir(tmp_path))
    fake = MagicMock(
        return_value=subprocess.CompletedProcess(["tpr", "--version"], 0, "tpr v9.9.9\n", "")
    )
    monkeypatch.setattr("runtime.adapters.tpr_adapter.subprocess.run", fake)

    assert adapter.version() == "v9.9.9"


def test_version_probe_falls_back_when_binary_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = TprAdapter(templates_dir=_make_templates_dir(tmp_path))

    def _missing(*_a: object, **_kw: object) -> object:
        raise FileNotFoundError("tpr")

    monkeypatch.setattr("runtime.adapters.tpr_adapter.subprocess.run", _missing)

    assert adapter.version() == TPR_VERSION_FALLBACK


def test_entry_hash_seed_includes_engine_version_and_templates(tmp_path: Path) -> None:
    wallpaper = _copy_wallpaper_fixture(tmp_path)
    wallpaper_hash = hash_file(wallpaper)
    adapter = _adapter(tmp_path)
    template_hash = adapter.template_set_hash()
    seed = palette_producer_seed(
        "tpr", template_hash, engine="native", version="v0.1.0"
    )
    assert seed is not None
    assert adapter.expected_entry_hash(wallpaper_hash) == palette_entry_hash(
        wallpaper_hash, template_hash, seed=seed
    )

    other_engine = TprAdapter(
        templates_dir=adapter.templates_dir, engine="matugen", version="v0.1.0"
    )
    other_version = TprAdapter(
        templates_dir=adapter.templates_dir, engine="native", version="v0.2.0"
    )
    assert other_engine.expected_entry_hash(wallpaper_hash) != adapter.expected_entry_hash(
        wallpaper_hash
    )
    assert other_version.expected_entry_hash(wallpaper_hash) != adapter.expected_entry_hash(
        wallpaper_hash
    )
