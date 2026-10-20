"""Runtime adapter invocation conformance, driven by the contracts record.

The expected argv/env come from ``cli_invocation_contract`` (pinned at
``contracts v0.1.0``), never from literals repeated here. The three tests replace
the per-adapter literal assertions named in S1 §6.1 with one record-driven check
per adapter.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from cli_invocation_contract import get as contract_record
from cli_invocation_contract.testkit import assert_consumer_invocation

from runtime.adapters.csg_adapter import CsgAdapter
from runtime.adapters.hashing import (
    canonical_hash_dir,
    effects_entry_hash,
    hash_file,
    icons_entry_hash,
    palette_entry_hash,
)
from runtime.adapters.itr_adapter import ItrAdapter
from runtime.adapters.weg_adapter import WegAdapter


def _copy_wallpaper_fixture(tmp_path: Path) -> Path:
    fixture = Path(__file__).parent.parent / "fixtures" / "wallpaper.png"
    assert fixture.is_file(), f"wallpaper.png fixture not found: {fixture}"
    dest = tmp_path / "wallpaper.png"
    dest.write_bytes(fixture.read_bytes())
    return dest


def _make_csg_templates_dir(tmp_path: Path) -> Path:
    tdir = tmp_path / "templates"
    tdir.mkdir(parents=True)
    (tdir / "colors.yaml.j2").write_text("background: {{ bg }}\n")
    return tdir


def _make_itr_templates_dir(tmp_path: Path) -> Path:
    tdir = tmp_path / "templates"
    tdir.mkdir(parents=True)
    (tdir / "icon.svg.j2").write_text('<svg fill="{{background}}"></svg>\n')
    return tdir


def _make_mappings_file(tmp_path: Path) -> Path:
    path = tmp_path / "mappings.yaml"
    path.write_text("battery:\n  variants: []\n")
    return path


def _make_catalog_file(tmp_path: Path) -> Path:
    path = tmp_path / "effects.yaml"
    path.write_text("version: '1.0'\neffects: []\n")
    return path


def _make_palette_entry(tmp_path: Path, palette_hash: str) -> Path:
    entry = tmp_path / "state" / "cache" / "palettes" / palette_hash
    entry.mkdir(parents=True, exist_ok=True)
    (entry / "colors.yaml").write_text('special:\n  background: "#000000"\ncolors: []\n')
    return entry


def _write_contract_artifacts(record: dict, output_dir: Path) -> None:
    """Write the artifacts a record requires so the adapter accepts the run."""
    artifacts = record["artifacts"]
    output_dir.mkdir(parents=True, exist_ok=True)
    if artifacts["match"] == "exact_filename":
        for filename in artifacts["filenames"]:
            (output_dir / filename).write_text("contract artifact\n")
        return
    (output_dir / f"artifact{artifacts['suffixes'][0]}").write_text("contract artifact\n")


def _recording_runner(record: dict) -> MagicMock:
    """Return a subprocess.run stand-in that records argv/env and writes artifacts."""

    def fake_run(
        args: list[str],
        capture_output: bool = False,  # noqa: ARG001
        text: bool = False,  # noqa: ARG001
        env: dict[str, str] | None = None,
        timeout: int | None = None,  # noqa: ARG001
        errors: str | None = None,  # noqa: ARG001
        **_kw: object,
    ) -> subprocess.CompletedProcess[str]:
        assert env is not None
        output_dir = Path(env[record["env"]["required"][0]])
        _write_contract_artifacts(record, output_dir)
        return subprocess.CompletedProcess(args, 0, "", "")

    return MagicMock(side_effect=fake_run)


def _assert_invocation(record: dict, cli: str, fake: MagicMock) -> None:
    argv = fake.call_args.args[0] if fake.call_args.args else fake.call_args.kwargs["args"]
    assert_consumer_invocation(record, cli, argv, fake.call_args.kwargs["env"])


def test_csg_invocation_matches_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    record = contract_record("csg")
    wallpaper = _copy_wallpaper_fixture(tmp_path)
    templates_dir = _make_csg_templates_dir(tmp_path)
    entry_hash = palette_entry_hash(hash_file(wallpaper), canonical_hash_dir(templates_dir))
    output_dir = tmp_path / "state" / "cache" / "palettes" / entry_hash

    fake = _recording_runner(record)
    monkeypatch.setattr("runtime.adapters.csg_adapter.subprocess.run", fake)

    CsgAdapter(templates_dir=templates_dir).generate(wallpaper, output_dir)

    _assert_invocation(record, "csg", fake)


def test_weg_invocation_matches_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    record = contract_record("weg")
    wallpaper = _copy_wallpaper_fixture(tmp_path)
    catalog = _make_catalog_file(tmp_path)
    entry_hash = effects_entry_hash(hash_file(wallpaper), hash_file(catalog))
    output_dir = tmp_path / "state" / "cache" / "effects" / entry_hash

    fake = _recording_runner(record)
    monkeypatch.setattr("runtime.adapters.weg_adapter.subprocess.run", fake)

    WegAdapter(catalog_path=catalog).generate(wallpaper, output_dir)

    _assert_invocation(record, "weg", fake)


def test_itr_invocation_matches_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    record = contract_record("itr")
    palette_hash = "a" * 64
    templates_dir = _make_itr_templates_dir(tmp_path)
    mappings_file = _make_mappings_file(tmp_path)
    palette_entry = _make_palette_entry(tmp_path, palette_hash)
    entry_hash = icons_entry_hash(
        palette_hash, canonical_hash_dir(templates_dir), hash_file(mappings_file)
    )
    output_dir = tmp_path / "state" / "cache" / "icons" / entry_hash

    fake = _recording_runner(record)
    monkeypatch.setattr("runtime.adapters.itr_adapter.subprocess.run", fake)

    ItrAdapter(
        templates_dir=templates_dir,
        mappings_path=mappings_file,
        palette_cache_dir=palette_entry,
    ).render(palette_hash, templates_dir, mappings_file, output_dir)

    _assert_invocation(record, "itr", fake)
