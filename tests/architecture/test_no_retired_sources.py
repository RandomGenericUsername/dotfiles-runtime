"""Mechanical guard: no runtime source may reference a retired in-repo tree.

``src/cli-tools/``, ``src/shared/`` and ``src/gui-tools/`` were deleted by the
cli-tools-repos split; the CLIs and shared libraries now deploy from pinned
repos (``dotfiles/provisioning/cli-tools.yaml`` + each ``pyproject.toml``).

A literal path into one of those trees is a discovery fallback: it resolves
nothing on a provisioned host (the tree is gone) and points at a divergent
checkout on a dev machine. Such a fallback does not fail — it silently degrades
coverage (skip / tmp fallback) and hides a missing input. This guard fails loud
instead, listing every offender.

Scope: production (``src/runtime/src/**/*.py``) *and* the test suite, because
the same retired-tree walk survived in tests after production was fixed
(``test_csg_determinism`` / ``test_itr_adapter_integration`` /
``test_csg_adapter`` / ``test_itr_adapter``). The guard file itself is excluded —
it necessarily contains the forbidden tokens as scan patterns. Detection covers
both the contiguous literal (``src/cli-tools``) and the split path-join form
(``"src" / "cli-tools"``) that ``rg`` misses.

Fix an offender by resolving the input from the provisioning spine
(``runtime.application.derive.resolve_input`` / ``find_templates_dir`` /
``find_icon_templates``) or from the pinned repo / checked-in asset — never by
walking ancestors for a retired tree.
"""

from __future__ import annotations

import re
from pathlib import Path

_RUNTIME_ROOT = Path(__file__).resolve().parents[2]  # src/runtime
_SRC_ROOT = _RUNTIME_ROOT / "src" / "runtime"  # production package
_TESTS_ROOT = _RUNTIME_ROOT / "tests"
_GUARD_PATH = Path(__file__).resolve()

#: Contiguous literals that name a retired tree.
_FORBIDDEN_SUBSTRINGS = (
    "src/cli-tools",
    "src/shared",
    "src/gui-tools",
)

#: Split path-join form, e.g. ``parent / "src" / "cli-tools"`` or
#: ``os.path.join("src", "shared")`` — the shape ``rg`` (line-based) misses.
_FORBIDDEN_SPLIT = re.compile(
    r"""["']src["']\s*(?:/|\+|,)\s*["'](?:cli-tools|shared|gui-tools)["']"""
)

_GUIDANCE = (
    "Retired in-repo trees must not be referenced. Resolve the input from the "
    "provisioning spine (runtime.application.derive.resolve_input / "
    "find_templates_dir / find_icon_templates) or the pinned repo / checked-in "
    "asset — never by walking ancestors for a retired tree. See ARCHITECTURE.md "
    "§12 and the cli-tools-repos split."
)


def _scan_text(text: str) -> list[str]:
    """Return the retired-tree tokens present in ``text`` (deduped, ordered)."""
    found: list[str] = [token for token in _FORBIDDEN_SUBSTRINGS if token in text]
    found.extend(match.group(0) for match in _FORBIDDEN_SPLIT.finditer(text))
    # Stable de-dup preserving first-seen order.
    seen: set[str] = set()
    unique: list[str] = []
    for token in found:
        if token not in seen:
            seen.add(token)
            unique.append(token)
    return unique


def _scanned_files() -> list[Path]:
    """Every production + test source file, minus this guard."""
    files = [*_SRC_ROOT.rglob("*.py"), *_TESTS_ROOT.rglob("*.py")]
    return sorted(f for f in files if f.resolve() != _GUARD_PATH)


def _offenders() -> list[str]:
    """``<path>:<token>`` for every retired-tree reference found."""
    results: list[str] = []
    for path in _scanned_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:  # pragma: no cover - fail loud
            results.append(f"{path}: unreadable ({exc})")
            continue
        for token in _scan_text(text):
            try:
                rel = path.relative_to(_RUNTIME_ROOT)
            except ValueError:  # pragma: no cover - defensive
                rel = path
            results.append(f"{rel}: {token}")
    return results


class TestNoRetiredSources:
    """No runtime source may name a retired in-repo package tree."""

    def test_no_retired_tree_literals(self) -> None:
        offenders = _offenders()
        assert not offenders, (
            "retired in-repo tree referenced in runtime source:\n"
            + "\n".join(f"  - {line}" for line in offenders)
            + f"\n{_GUIDANCE}"
        )

    def test_guard_scans_production_and_tests(self) -> None:
        """The guard actually covers both trees (a silent no-op gate is worse)."""
        scanned = _scanned_files()
        assert any(_SRC_ROOT in f.parents for f in scanned), "production not scanned"
        assert any(_TESTS_ROOT in f.parents for f in scanned), "tests not scanned"
        assert _GUARD_PATH not in scanned, "guard must exclude itself"
        assert scanned, "guard scanned zero files"

    def test_scan_text_self_test(self) -> None:
        """Synthetic inputs: contiguous, split and clean forms."""
        assert _scan_text("x = 'src/cli-tools/y'") == ["src/cli-tools"]
        assert _scan_text("x = 'src/shared/y'") == ["src/shared"]
        assert _scan_text("x = 'src/gui-tools/y'") == ["src/gui-tools"]
        # Split path-join form (multi-line, as the removed walks were written).
        split_walk = 'p = parent\n    / "src"\n    / "cli-tools"\n'
        assert _scan_text(split_walk) == ['"src"\n    / "cli-tools"']
        assert _scan_text('p = os.path.join("src", "shared")') == ['"src", "shared"']
        # Clean forms must not trip the guard.
        assert _scan_text('p = parent / "src" / "runtime"') == []
        assert _scan_text("# cli-tools-repos S5 retired the in-tree tree") == []
        assert _scan_text("src/runtime/tests") == []

    def test_guard_file_excluded(self) -> None:
        """The guard's own tokens (its scan patterns) must not be offenders."""
        assert _scan_text(_GUARD_PATH.read_text(encoding="utf-8")), (
            "guard file should contain the tokens it scans for"
        )
        assert _GUARD_PATH not in _scanned_files()


if __name__ == "__main__":  # pragma: no cover - manual invocation
    _offenders_list = _offenders()
    if _offenders_list:
        print(f"{len(_offenders_list)} retired-tree reference(s) found:")
        for _line in _offenders_list:
            print(f"  - {_line}")
        print(_GUIDANCE)
        raise SystemExit(1)
    print(f"OK: no retired-tree literals in {len(_scanned_files())} runtime source files.")
