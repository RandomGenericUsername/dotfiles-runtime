"""Watch-set input hash tests (AD-36/AD-39): bounded and total."""

from __future__ import annotations

from pathlib import Path

from runtime.adapters.converge_inputs import compute_watch_input_hash
from runtime.adapters.watch_roots import WatchRoot


def _file_root(path: Path) -> WatchRoot:
    return WatchRoot(path, is_directory=False)


def _dir_root(path: Path, depth: int) -> WatchRoot:
    return WatchRoot(path, is_directory=True, depth=depth)


def test_absent_inputs_yield_stable_hash(tmp_path: Path) -> None:
    roots = (_file_root(tmp_path / "missing.yaml"), _dir_root(tmp_path / "missing-dir", 2))
    first = compute_watch_input_hash(roots)
    second = compute_watch_input_hash(roots)
    assert first == second


def test_file_content_change_changes_hash(tmp_path: Path) -> None:
    target = tmp_path / "effects.yaml"
    target.write_text("a: 1\n", encoding="utf-8")
    roots = (_file_root(target),)
    before = compute_watch_input_hash(roots)
    target.write_text("a: 2\n", encoding="utf-8")
    assert compute_watch_input_hash(roots) != before


def test_dir_content_change_changes_hash(tmp_path: Path) -> None:
    tree = tmp_path / "templates"
    tree.mkdir()
    (tree / "a.j2").write_text("x", encoding="utf-8")
    roots = (_dir_root(tree, 2),)
    before = compute_watch_input_hash(roots)
    (tree / "b.j2").write_text("y", encoding="utf-8")
    assert compute_watch_input_hash(roots) != before


def test_hash_is_bounded_to_watch_depth(tmp_path: Path) -> None:
    """A change deeper than the watch depth must not change the hash."""
    tree = tmp_path / "templates"
    deep = tree / "l1" / "l2" / "l3"
    deep.mkdir(parents=True)
    (deep / "deep.j2").write_text("x", encoding="utf-8")
    roots = (_dir_root(tree, 1),)
    before = compute_watch_input_hash(roots)
    (deep / "deep.j2").write_text("changed", encoding="utf-8")
    assert compute_watch_input_hash(roots) == before
    # ... but a change within depth does.
    (tree / "shallow.j2").write_text("y", encoding="utf-8")
    assert compute_watch_input_hash(roots) != before


def test_symlinked_intent_is_not_followed(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    real.write_text('{"version": 1}', encoding="utf-8")
    link = tmp_path / "desired.json"
    link.symlink_to(real)
    roots = (_file_root(link),)
    before = compute_watch_input_hash(roots)
    real.write_text('{"version": 2}', encoding="utf-8")
    assert compute_watch_input_hash(roots) == before  # sentinel, target ignored


def test_wrong_kind_is_sentinel_not_crash(tmp_path: Path) -> None:
    file_where_dir_expected = tmp_path / "not-a-dir"
    file_where_dir_expected.write_text("x", encoding="utf-8")
    roots = (_dir_root(file_where_dir_expected, 2),)
    assert compute_watch_input_hash(roots)  # total, no raise
