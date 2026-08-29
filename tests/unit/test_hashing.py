"""Unit tests for canonical hashing and per-layer cache keys."""

from __future__ import annotations

import hashlib
import typing
from pathlib import Path

import pytest

from runtime.adapters.hashing import (
    HASH_ALGORITHM,
    canonical_hash_dir,
    canonical_hash_file,
    effects_entry_hash,
    hash_file,
    hash_file_bytes,
    icons_entry_hash,
    palette_entry_hash,
)
from runtime.domain.models import WallpaperEntry

# ---------------------------------------------------------------------------
# Task 3 table: each row maps to a test
# ---------------------------------------------------------------------------


def test_hash_file_bytes_identical_vs_differing() -> None:
    assert hash_file_bytes(b"hello") == hashlib.sha256(b"hello").hexdigest()
    assert hash_file_bytes(b"hello") == hash_file_bytes(b"hello")
    assert hash_file_bytes(b"hello") != hash_file_bytes(b"hellx")
    # One-byte change diverges
    assert hashlib.sha256(b"hello").hexdigest() != hashlib.sha256(b"hellx").hexdigest()


def test_canonical_hash_dir_sorts_relpaths(tmp_path: Path) -> None:
    # Two dirs with same files created in different insertion order → same hash
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    # Create in order alpha, beta vs beta, alpha
    (dir_a / "alpha.j2").write_bytes(b"alpha content")
    (dir_a / "beta.j2").write_bytes(b"beta content")
    (dir_b / "beta.j2").write_bytes(b"beta content")
    (dir_b / "alpha.j2").write_bytes(b"alpha content")
    assert canonical_hash_dir(dir_a) == canonical_hash_dir(dir_b)

    # Absolute parent does NOT affect hash: same relpaths+contents → same
    dir_c = tmp_path / "deep" / "c"
    dir_d = tmp_path / "other" / "d"
    dir_c.mkdir(parents=True)
    dir_d.mkdir(parents=True)
    (dir_c / "colors.conf.j2").write_bytes(b"same")
    (dir_d / "colors.conf.j2").write_bytes(b"same")
    assert canonical_hash_dir(dir_c) == canonical_hash_dir(dir_d)


def test_canonical_hash_dir_empty_dir(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    expected = hashlib.sha256(b"").hexdigest()
    assert expected == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert canonical_hash_dir(empty) == expected


def test_canonical_hash_dir_file_content_change_invalidates(tmp_path: Path) -> None:
    d = tmp_path / "d"
    d.mkdir()
    p = d / "file.j2"
    p.write_bytes(b"v1")
    hash_v1 = canonical_hash_dir(d)
    p.write_bytes(b"v2")
    hash_v2 = canonical_hash_dir(d)
    assert hash_v1 != hash_v2


def test_canonical_hash_dir_add_remove_file_invalidates(tmp_path: Path) -> None:
    d = tmp_path / "d"
    d.mkdir()
    (d / "a.j2").write_bytes(b"a")
    hash_one = canonical_hash_dir(d)
    (d / "b.j2").write_bytes(b"b")
    hash_two = canonical_hash_dir(d)
    assert hash_one != hash_two
    (d / "b.j2").unlink()
    assert canonical_hash_dir(d) == hash_one


def test_palette_entry_hash_deterministic() -> None:
    wh = "a" * 64
    th = "b" * 64
    h1 = palette_entry_hash(wh, th)
    h2 = palette_entry_hash(wh, th)
    assert h1 == h2
    assert len(h1) == 64
    assert h1 != palette_entry_hash("c" * 64, th)
    assert h1 != palette_entry_hash(wh, "c" * 64)


def test_effects_entry_hash_deterministic() -> None:
    wh = "a" * 64
    ch = "b" * 64
    assert effects_entry_hash(wh, ch) == effects_entry_hash(wh, ch)
    assert effects_entry_hash(wh, ch) != effects_entry_hash("c" * 64, ch)
    assert effects_entry_hash(wh, ch) != effects_entry_hash(wh, "c" * 64)


def test_icons_entry_hash_deterministic() -> None:
    ph = "a" * 64
    th = "b" * 64
    mh = "c" * 64
    assert icons_entry_hash(ph, th, mh) == icons_entry_hash(ph, th, mh)
    assert icons_entry_hash(ph, th, mh) != icons_entry_hash("d" * 64, th, mh)
    assert icons_entry_hash(ph, th, mh) != icons_entry_hash(ph, "e" * 64, mh)
    assert icons_entry_hash(ph, th, mh) != icons_entry_hash(ph, th, "f" * 64)


def test_canonical_hash_dir_unreadable_graceful(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = tmp_path / "d"
    d.mkdir()
    (d / "good.j2").write_bytes(b"good")
    (d / "bad.j2").write_bytes(b"bad")
    # Patch helper instead of global Path.open to avoid contaminating tmp_path internals.
    from runtime.adapters import hashing as hashing_module

    original = hashing_module._hash_file_chunked

    def fake_hash(path: Path) -> str:
        if path.name == "bad.j2":
            raise OSError("simulated unreadable")
        return original(path)

    monkeypatch.setattr(hashing_module, "_hash_file_chunked", fake_hash)
    h = canonical_hash_dir(d)
    # Still returns 64-char lowercase hex, not raise
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)
    # Sentinel must have been used — hash differs from the all-good case
    monkeypatch.undo()
    h_good = canonical_hash_dir(d)
    assert h != h_good


def test_read_bytes_not_read_text(tmp_path: Path) -> None:
    p = tmp_path / "file.bin"
    p.write_bytes(b"line\r\nline\r\n")
    # hash_file must be binary — \r\n must not be normalized to \n
    expected = hashlib.sha256(b"line\r\nline\r\n").hexdigest()
    assert hash_file(p) == expected
    assert hash_file(p) != hashlib.sha256(b"line\nline\n").hexdigest()


def test_hash_file_chunked_large(tmp_path: Path) -> None:
    p = tmp_path / "large.bin"
    # 1 MB via repeated chunks to exercise chunked loop
    chunk = b"0123456789abcdef" * 4096  # 64 KiB
    data = chunk * 16  # 1 MiB
    p.write_bytes(data)
    expected = hashlib.sha256(data).hexdigest()
    assert hash_file(p) == expected
    assert canonical_hash_file(p) == expected


def test_canonical_hash_file_alias(tmp_path: Path) -> None:
    p = tmp_path / "effects.yaml"
    p.write_bytes(b"effects: []")
    assert canonical_hash_file(p) == hash_file(p)


def test_canonical_hash_dir_raises_on_missing(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    with pytest.raises(FileNotFoundError):
        canonical_hash_dir(missing)


def test_canonical_hash_dir_raises_on_file(tmp_path: Path) -> None:
    f = tmp_path / "file.txt"
    f.write_bytes(b"x")
    with pytest.raises(NotADirectoryError):
        canonical_hash_dir(f)


def test_hash_file_raises_on_missing(tmp_path: Path) -> None:
    missing = tmp_path / "missing.png"
    with pytest.raises(FileNotFoundError):
        hash_file(missing)


def test_hash_algorithm_constant_matches_domain() -> None:
    assert HASH_ALGORITHM == "sha256"
    # Domain literal via get_type_hints (future annotations are strings)
    hints = typing.get_type_hints(WallpaperEntry)
    args = typing.get_args(hints["hash_algorithm"])
    assert "sha256" in args
    assert HASH_ALGORITHM in args


def test_hashes_are_lowercase_hex(tmp_path: Path) -> None:
    p = tmp_path / "f.bin"
    p.write_bytes(b"test")
    for h in [
        hash_file(p),
        hash_file_bytes(b"test"),
        canonical_hash_dir(tmp_path),
        palette_entry_hash("a" * 64, "b" * 64),
        effects_entry_hash("a" * 64, "b" * 64),
        icons_entry_hash("a" * 64, "b" * 64, "c" * 64),
    ]:
        assert len(h) == 64
        assert h == h.lower()
        assert all(c in "0123456789abcdef" for c in h)


def test_wallpaper_fixture_hash(tmp_path: Path) -> None:
    # Reuse deterministic 4x4 PNG from Story 1.4 — must fail loudly if missing.
    candidates = [
        Path("tests/fixtures/wallpaper.png"),
        Path("src/runtime/tests/fixtures/wallpaper.png"),
        Path(__file__).parent.parent / "fixtures" / "wallpaper.png",
        Path(__file__).parent / "fixtures" / "wallpaper.png",
    ]
    fixture = next((p for p in candidates if p.exists()), None)
    assert fixture is not None, f"wallpaper.png fixture not found, tried {candidates}"
    h = hash_file(fixture)
    assert h == "619cd350283e11c3cc9ee7b3d67dce93738e87e7fefa16144840ebd716534381"
    assert h == hash_file_bytes(fixture.read_bytes())


def test_canonical_hash_dir_ignores_build_noise(tmp_path: Path) -> None:
    d = tmp_path / "d"
    d.mkdir()
    (d / "a.j2").write_bytes(b"a")
    hash_clean = canonical_hash_dir(d)
    # Add build noise that must be filtered
    (d / "__pycache__").mkdir()
    (d / "__pycache__" / "a.pyc").write_bytes(b"noise")
    (d / "b.pyc").write_bytes(b"noise")
    (d / "temp.swp").write_bytes(b"noise")
    (d / "backup~").write_bytes(b"noise")
    assert canonical_hash_dir(d) == hash_clean
