"""AD-42/AD-44 repo guard: no unknown trigger can be written to history.

Executable source scan over every ``append_history`` call site: every literal
``trigger=`` value must be a member of the single-sourced enum. This is the
repo-level assertion that no code path can smuggle a new trigger past the
schema/validator (the reactive writer and the prune writer are both present).
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

from runtime.domain.history import HISTORY_TRIGGERS

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src"


def _append_history_trigger_literals() -> Iterator[tuple[Path, int, str]]:
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else func.id
                if isinstance(func, ast.Name)
                else ""
            )
            if name != "append_history":
                continue
            for keyword in node.keywords:
                if keyword.arg == "trigger" and isinstance(keyword.value, ast.Constant):
                    value = keyword.value.value
                    if isinstance(value, str):
                        yield path, node.lineno, value


def test_every_written_trigger_is_in_the_enum() -> None:
    unknown = [
        (path, lineno, value)
        for path, lineno, value in _append_history_trigger_literals()
        if value not in HISTORY_TRIGGERS
    ]
    assert not unknown, f"unknown history trigger(s) written: {unknown}"


def test_reactive_and_prune_writers_exist() -> None:
    values = {value for _path, _lineno, value in _append_history_trigger_literals()}
    assert "reactive" in values, "the daemon reactive writer must write trigger='reactive'"
    assert "prune" in values, "the prune writer must write trigger='prune'"


def test_reconcile_use_case_rejects_unknown_trigger() -> None:
    from runtime.application.reconcile import ReconcileDesktopStateUseCase

    use_case = ReconcileDesktopStateUseCase(
        state_repo=None,  # type: ignore[arg-type]
        csg=None,  # type: ignore[arg-type]
        weg=None,  # type: ignore[arg-type]
        itr=None,  # type: ignore[arg-type]
        install_spine=Path("/install"),
        state_root=Path("/state"),
        seeder=None,  # type: ignore[arg-type]
        mutex=None,  # type: ignore[arg-type]
    )
    try:
        use_case.run(trigger="bogus-trigger")
    except ValueError as exc:
        assert "invalid history trigger" in str(exc)
    else:  # pragma: no cover - a missing guard is the failure
        raise AssertionError("unknown trigger was accepted")
