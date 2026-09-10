"""Unit tests for Story 3.1 cache verify (verify_entry + VerifyCacheUseCase).

Hand-built entry dirs for the adapter verdicts; real lister + injected
verify_entry for the use case; CliRunner + monkeypatched composition for the
CLI shape. Covers Story 3.1 ACs: default output unchanged, per-entry verdicts,
legacy lazy annotation, idempotency, taxonomy, single walk.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import runtime.cli.main as cli_main  # noqa: F401  (used by monkeypatch path strings)
from runtime.adapters.cache import resolve_entry_artifact, verify_entry
from runtime.application.inspect import InspectCacheResult, InspectCacheUseCase
from runtime.application.verify_cache import VerifyCacheUseCase
from runtime.cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _state_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))


def _h(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_entry(
    state_root: Path,
    layer: str,
    entry_hash: str,
    files: dict[str, bytes],
    *,
    artifact_hashes: dict[str, str] | None | str = "auto",
    extra_meta: dict[str, object] | None = None,
) -> Path:
    entry_dir = state_root / "cache" / layer / entry_hash
    entry_dir.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        target = entry_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    meta: dict[str, object] = {"hash_algorithm": "sha256", "kind": layer.rstrip("s")}
    if extra_meta:
        meta.update(extra_meta)
    if artifact_hashes == "auto":
        meta["artifact_hashes"] = {rel: _h(c) for rel, c in files.items()}
    elif artifact_hashes is not None:
        meta["artifact_hashes"] = artifact_hashes
    (entry_dir / "meta.json").write_text(
        json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8"
    )
    return entry_dir


class TestVerifyEntryVerdicts:
    def test_healthy_ok(self, tmp_path: Path) -> None:
        d = _write_entry(tmp_path, "effects", "a" * 64, {"effect.png": b"png"})
        h = verify_entry(d)
        assert h.status == "ok" and h.annotated is False

    def test_missing_meta(self, tmp_path: Path) -> None:
        d = tmp_path / "cache" / "icons" / ("b" * 64)
        d.mkdir(parents=True)
        assert verify_entry(d).status == "missing"

    def test_unparseable_meta_corrupt(self, tmp_path: Path) -> None:
        d = _write_entry(tmp_path, "effects", "a" * 64, {"effect.png": b"png"})
        (d / "meta.json").write_text("{bad", encoding="utf-8")
        assert verify_entry(d).status == "corrupt"

    def test_non_object_meta_corrupt(self, tmp_path: Path) -> None:
        d = _write_entry(tmp_path, "effects", "a" * 64, {"effect.png": b"png"})
        (d / "meta.json").write_text("[]", encoding="utf-8")
        assert verify_entry(d).status == "corrupt"

    def test_malformed_map_corrupt(self, tmp_path: Path) -> None:
        d = _write_entry(
            tmp_path,
            "effects",
            "a" * 64,
            {"effect.png": b"png"},
            artifact_hashes={"effect.png": 123},  # type: ignore[dict-item]
        )
        assert verify_entry(d).status == "corrupt"

    def test_unsafe_key_corrupt(self, tmp_path: Path) -> None:
        d = _write_entry(
            tmp_path,
            "effects",
            "a" * 64,
            {"effect.png": b"png"},
            artifact_hashes={"/etc/passwd": "ab" * 32},
        )
        assert verify_entry(d).status == "corrupt"

    def test_absent_recorded_artifact_missing(self, tmp_path: Path) -> None:
        d = _write_entry(
            tmp_path,
            "effects",
            "a" * 64,
            {"effect.png": b"png"},
            artifact_hashes={"ghost.png": _h(b"x")},
        )
        assert verify_entry(d).status == "missing"

    def test_digest_mismatch_corrupt(self, tmp_path: Path) -> None:
        d = _write_entry(
            tmp_path,
            "effects",
            "a" * 64,
            {"effect.png": b"png"},
            artifact_hashes={"effect.png": "ab" * 32},
        )
        assert verify_entry(d).status == "corrupt"

    def test_dotdot_key_corrupt(self, tmp_path: Path) -> None:
        d = _write_entry(
            tmp_path,
            "effects",
            "a" * 64,
            {"effect.png": b"png"},
            artifact_hashes={"../x": "ab" * 32},
        )
        assert verify_entry(d).status == "corrupt"

    def test_symlink_escape_corrupt(self, tmp_path: Path) -> None:
        outside = tmp_path / "secret.bin"
        outside.write_bytes(b"secret")
        d = tmp_path / "cache" / "effects" / ("a" * 64)
        d.mkdir(parents=True)
        (d / "link.bin").symlink_to(outside)
        (d / "meta.json").write_text(
            json.dumps(
                {
                    "hash_algorithm": "sha256",
                    "artifact_hashes": {"link.bin": _h(b"secret")},
                }
            ),
            encoding="utf-8",
        )
        assert verify_entry(d).status == "corrupt"

    def test_symlinked_meta_refused(self, tmp_path: Path) -> None:
        d = tmp_path / "cache" / "effects" / ("a" * 64)
        d.mkdir(parents=True)
        outside = tmp_path / "meta.json"
        outside.write_text(json.dumps({"artifact_hashes": {}}), encoding="utf-8")
        (d / "meta.json").symlink_to(outside)
        assert verify_entry(d).status == "corrupt"

    def test_unrecorded_file_corrupt(self, tmp_path: Path) -> None:
        d = _write_entry(tmp_path, "effects", "a" * 64, {"effect.png": b"png"})
        (d / "stray.bin").write_bytes(b"stray")
        assert verify_entry(d).status == "corrupt"

    def test_nested_filename_keys_ok(self, tmp_path: Path) -> None:
        """WEG-shaped entry: nested files, meta keys by filename → ok."""
        d = tmp_path / "cache" / "effects" / ("a" * 64)
        nested = d / "abstract" / "effect"
        nested.mkdir(parents=True)
        (nested / "blur.jpg").write_bytes(b"blur")
        (d / "meta.json").write_text(
            json.dumps({"hash_algorithm": "sha256", "artifact_hashes": {"blur.jpg": _h(b"blur")}}),
            encoding="utf-8",
        )
        assert verify_entry(d).status == "ok"

    def test_nested_filename_key_tampered_corrupt(self, tmp_path: Path) -> None:
        d = tmp_path / "cache" / "effects" / ("a" * 64)
        nested = d / "abstract" / "effect"
        nested.mkdir(parents=True)
        (nested / "blur.jpg").write_bytes(b"tampered")
        (d / "meta.json").write_text(
            json.dumps(
                {"hash_algorithm": "sha256", "artifact_hashes": {"blur.jpg": _h(b"original")}}
            ),
            encoding="utf-8",
        )
        assert verify_entry(d).status == "corrupt"

    def test_non_sha256_algorithm_corrupt(self, tmp_path: Path) -> None:
        d = _write_entry(tmp_path, "effects", "a" * 64, {"effect.png": b"png"})
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        meta["hash_algorithm"] = "md5"
        (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        assert verify_entry(d).status == "corrupt"


class TestLegacyAnnotation:
    def test_annotate_false_tolerates_without_write(self, tmp_path: Path) -> None:
        d = _write_entry(
            tmp_path,
            "effects",
            "a" * 64,
            {"effect.png": b"png"},
            artifact_hashes=None,
        )
        before = (d / "meta.json").read_bytes()
        health = verify_entry(d, annotate=False)
        assert health.status == "ok" and health.annotated is False
        assert (d / "meta.json").read_bytes() == before

    def test_annotate_true_records_and_preserves_keys(self, tmp_path: Path) -> None:
        d = _write_entry(
            tmp_path,
            "effects",
            "a" * 64,
            {"effect.png": b"png", "nested/fx.png": b"nested"},
            artifact_hashes=None,
            extra_meta={"source_wallpaper_hash": "w" * 64},
        )
        health = verify_entry(d, annotate=True)
        assert health.status == "ok" and health.annotated is True
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        assert meta["source_wallpaper_hash"] == "w" * 64  # other keys preserved
        # Contract keys are filenames (unique basename), matching the adapter.
        assert meta["artifact_hashes"] == {
            "effect.png": _h(b"png"),
            "fx.png": _h(b"nested"),
        }

    def test_empty_map_is_treated_as_legacy_and_annotated(self, tmp_path: Path) -> None:
        d = _write_entry(tmp_path, "effects", "a" * 64, {"effect.png": b"png"}, artifact_hashes={})
        health = verify_entry(d, annotate=True)
        assert health.status == "ok" and health.annotated is True
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        assert meta["artifact_hashes"] == {"effect.png": _h(b"png")}

    def test_annotation_write_failure_returns_corrupt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import runtime.adapters.cache as cache_mod

        d = _write_entry(
            tmp_path, "effects", "a" * 64, {"effect.png": b"png"}, artifact_hashes=None
        )

        def _boom(*args: object, **kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(cache_mod.os, "replace", _boom)
        assert verify_entry(d, annotate=True).status == "corrupt"

    def test_annotation_idempotent(self, tmp_path: Path) -> None:
        d = _write_entry(
            tmp_path,
            "effects",
            "a" * 64,
            {"effect.png": b"png"},
            artifact_hashes=None,
        )
        verify_entry(d, annotate=True)
        after_first = (d / "meta.json").read_bytes()
        second = verify_entry(d, annotate=True)
        assert second.status == "ok" and second.annotated is False
        assert (d / "meta.json").read_bytes() == after_first  # no rewrite


class TestUseCase:
    def test_single_walk_and_aggregate(self, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        _write_entry(state_root, "effects", "a" * 64, {"e.png": b"e"})
        _write_entry(
            state_root,
            "palettes",
            "b" * 64,
            {"colors.yaml": b"y"},
            artifact_hashes={"colors.yaml": "cd" * 32},  # corrupt
        )
        lister = InspectCacheUseCase(state_root=state_root)
        calls: list[int] = []
        real_run = lister.run

        def _spy() -> InspectCacheResult:
            calls.append(1)
            return real_run()

        lister.run = _spy  # type: ignore[assignment, method-assign]
        result = VerifyCacheUseCase(
            state_root=state_root,
            lister=lister,
            verify_entry=lambda d: verify_entry(d, annotate=True),
        ).run()
        assert calls == [1]  # single bounded walk
        assert result.total == 2 and result.unhealthy == 1
        seen = {
            (layer, h, health.status)
            for layer, items in result.layers.items()
            for h, health in items
        }
        assert ("palettes", "b" * 64, "corrupt") in seen
        assert ("effects", "a" * 64, "ok") in seen

    def test_healthy_entry_meta_untouched(self, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        d = _write_entry(state_root, "effects", "a" * 64, {"e.png": b"e"})
        meta = d / "meta.json"
        before = meta.read_bytes()
        mtime = meta.stat().st_mtime_ns
        VerifyCacheUseCase(
            state_root=state_root,
            lister=InspectCacheUseCase(state_root=state_root),
            verify_entry=lambda p: verify_entry(p, annotate=True),
        ).run()
        assert meta.read_bytes() == before
        assert meta.stat().st_mtime_ns == mtime


class TestCliShape:
    def test_no_flag_output_has_no_health(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        state_root = tmp_path / "state-home" / "dotfiles"
        _write_entry(state_root, "effects", "a" * 64, {"e.png": b"e"})
        result = runner.invoke(app, ["inspect", "cache", "list"])
        assert result.exit_code == 0
        assert "[ok]" not in result.output and "[corrupt]" not in result.output

    def test_verify_healthy_exit_zero_with_status(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        state_root = tmp_path / "state-home" / "dotfiles"
        _write_entry(state_root, "effects", "a" * 64, {"e.png": b"e"})
        result = runner.invoke(app, ["inspect", "cache", "list", "--verify"])
        assert result.exit_code == 0
        assert "[ok]" in result.output

    def test_verify_corrupt_exit_one(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        state_root = tmp_path / "state-home" / "dotfiles"
        _write_entry(
            state_root,
            "palettes",
            "b" * 64,
            {"colors.yaml": b"y"},
            artifact_hashes={"colors.yaml": "cd" * 32},
        )
        result = runner.invoke(app, ["inspect", "cache", "list", "--verify", "--format", "json"])
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["unhealthy"] == 1
        assert payload["layers"]["palettes"][0]["status"] == "corrupt"

    def test_verify_legacy_annotates_and_reports_ok(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        state_root = tmp_path / "state-home" / "dotfiles"
        d = _write_entry(state_root, "effects", "a" * 64, {"e.png": b"e"}, artifact_hashes=None)
        result = runner.invoke(app, ["inspect", "cache", "list", "--verify"])
        assert result.exit_code == 0
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        assert meta["artifact_hashes"] == {"e.png": _h(b"e")}

    def test_verify_missing_meta_exit_one(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        state_root = tmp_path / "state-home" / "dotfiles"
        d = _write_entry(state_root, "effects", "a" * 64, {"e.png": b"e"})
        (d / "meta.json").unlink()
        result = runner.invoke(app, ["inspect", "cache", "list", "--verify"])
        assert result.exit_code == 1
        assert "[missing]" in result.output

    def test_default_json_golden(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        state_root = tmp_path / "state-home" / "dotfiles"
        _write_entry(state_root, "effects", "a" * 64, {"e.png": b"e"})
        result = runner.invoke(app, ["inspect", "cache", "list", "--format", "json"])
        assert result.exit_code == 0
        assert json.loads(result.output) == {
            "layers": {
                "wallpapers": [],
                "palettes": [],
                "effects": ["a" * 64],
                "icons": [],
            },
            "counts": {"wallpapers": 0, "palettes": 0, "effects": 1, "icons": 0},
            "total": 1,
        }


class TestResolveEntryArtifact:
    """Contract keys are filenames; generators nest (WEG)."""

    def test_bare_key_resolves_nested_file(self, tmp_path: Path) -> None:
        nested = tmp_path / "abstract" / "effect"
        nested.mkdir(parents=True)
        target = nested / "blur.jpg"
        target.write_bytes(b"x")
        assert resolve_entry_artifact(tmp_path, "blur.jpg") == target

    def test_relpath_key_resolves_exactly(self, tmp_path: Path) -> None:
        nested = tmp_path / "abstract" / "effect"
        nested.mkdir(parents=True)
        target = nested / "blur.jpg"
        target.write_bytes(b"x")
        assert resolve_entry_artifact(tmp_path, "abstract/effect/blur.jpg") == target

    def test_ambiguous_bare_key_none(self, tmp_path: Path) -> None:
        for sub in ("effect", "composite"):
            d = tmp_path / sub
            d.mkdir(parents=True)
            (d / "blur.jpg").write_bytes(b"x")
        assert resolve_entry_artifact(tmp_path, "blur.jpg") is None

    def test_missing_none(self, tmp_path: Path) -> None:
        assert resolve_entry_artifact(tmp_path, "nope.jpg") is None
