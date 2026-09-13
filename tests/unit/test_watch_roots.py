"""AD-39 watch-root enumeration tests.

The watched roots are an explicit, hard-coded spine allowlist — never the
result of ``derive.find_*``. These tests pin the exact set, the bounded
depths, and the exclusions (consumer pointers and all of ``state_root``).
"""

from __future__ import annotations

from pathlib import Path

from runtime.adapters.watch_roots import (
    CSG_TEMPLATES_DEPTH,
    ICON_MAPPINGS_DEPTH,
    ICON_TEMPLATES_DEPTH,
    enumerate_watch_roots,
)

SPINE = Path("/install")
INTENT = Path("/cfg/dotfiles/desired.json")


def _roots() -> tuple[object, ...]:
    return enumerate_watch_roots(SPINE, INTENT)


def test_exact_ad39_set_in_order() -> None:
    roots = _roots()
    paths = [r.path for r in roots]
    assert paths == [
        Path("/install/config/color-scheme-generator/templates"),
        Path("/install/config/weg/effects.yaml"),
        Path("/install/icon-templates"),
        Path("/install/icon-mappings"),
        Path("/cfg/dotfiles/desired.json"),
    ]


def test_bounded_depths() -> None:
    roots = _roots()
    by_path = {r.path: r for r in roots}
    assert by_path[Path("/install/config/color-scheme-generator/templates")].depth == (
        CSG_TEMPLATES_DEPTH
    )
    assert by_path[Path("/install/icon-templates")].depth == ICON_TEMPLATES_DEPTH
    assert by_path[Path("/install/icon-mappings")].depth == ICON_MAPPINGS_DEPTH
    assert by_path[Path("/install/config/weg/effects.yaml")].is_directory is False
    assert by_path[Path("/cfg/dotfiles/desired.json")].is_directory is False


def test_consumer_pointers_and_state_root_are_not_watched() -> None:
    watched = {str(r.path) for r in _roots()}
    forbidden_fragments = (
        "/config/ags/colors.css",
        "/config/gtk-3.0/colors.css",
        "/config/gtk-4.0/colors.css",
        "/config/rofi/colors.rasi",
        "/state",
    )
    for path in watched:
        assert not any(fragment in path for fragment in forbidden_fragments), path


def test_intent_path_is_used_verbatim_not_derived() -> None:
    roots = _roots()
    assert roots[-1].path == INTENT


def test_enumeration_never_uses_repo_resolution() -> None:
    """Static guard: the enumerator must not consult derive.find_* (AD-43)."""
    import ast

    import runtime.adapters.watch_roots as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            assert "application" not in node.module
            assert "derive" not in node.module
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            assert not name.startswith("find_"), f"unexpected discovery call: {name}"
