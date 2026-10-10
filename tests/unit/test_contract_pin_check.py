"""Negative test for the cross-repo contract pin gate (REQ-004, spine AD-6).

`make contract-pin-check` (`scripts/contract-pin-check.py`) diffs each VENDORED
generated binding against the `contracts` repo's `generated/` artifact. A gate
that cannot fail is worse than no gate, so this suite proves the gate FAILS on a
hand-edited copy and PASSES on a clean one.

The proof is HERMETIC: it builds a synthetic dotfiles repo + contracts repo in a
tmp dir, so it needs no external `contracts` checkout and therefore runs on a
stock CI runner (it is wired into `make contracts-check`). The gate itself is
loaded from the real repo, not reimplemented, so the test cannot pass against a
gate that no longer exists.

The synthetic trees use the gate's OWN `VENDORED` table and header builders, so
the expected header is defined once (in the gate) and the test cannot drift from
it. A real-tree pass is asserted only when the pinned contracts checkout is
present, and skipped otherwise rather than silently passing.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

GATE_REL = Path("scripts") / "contract-pin-check.py"
SYNTHETIC_PIN = "deadbeef"
#: Byte bodies for the synthetic `contracts/generated/` artifacts, keyed by the
#: generated filename the gate's `VENDORED` table names.
SYNTHETIC_GENERATED = {
    "contract.py": b'"""synthetic generated binding"""\nVALUE = 1\n',
    "contract.ts": b"// synthetic generated binding\nexport const VALUE = 1;\n",
    "events.schema.json": b'{"$defs": {"Topic_x": {"type": "object"}}}\n',
}


def _load_gate(repo_root: Path) -> ModuleType:
    """Import `scripts/contract-pin-check.py` (hyphenated name -> importlib)."""
    path = repo_root / GATE_REL
    spec = importlib.util.spec_from_file_location("_contract_pin_check_gate", path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _build_synthetic_tree(gate: ModuleType, tmp_path: Path) -> tuple[Path, Path]:
    """Create a clean synthetic (repo_root, contracts_repo) pair."""
    repo_root = tmp_path / "dotfiles-v3"
    contracts_repo = tmp_path / "contracts"
    generated_dir = contracts_repo / "generated"
    generated_dir.mkdir(parents=True)
    for name, body in SYNTHETIC_GENERATED.items():
        (generated_dir / name).write_bytes(body)

    for vendored_rel, generated_name, header in gate.VENDORED:
        target = repo_root / vendored_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(header(SYNTHETIC_PIN) + SYNTHETIC_GENERATED[generated_name])

    pin_path = repo_root / gate.PIN_FILE
    pin_path.parent.mkdir(parents=True, exist_ok=True)
    pin_path.write_text(SYNTHETIC_PIN + "\n", encoding="utf-8")
    return repo_root, contracts_repo


def _run_gate(gate_script: Path, repo_root: Path, contracts_repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(gate_script),
            "--repo-root",
            str(repo_root),
            "--contracts-repo",
            str(contracts_repo),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def gate(repo_root: Path) -> ModuleType:
    return _load_gate(repo_root)


def test_gate_passes_on_a_clean_tree(repo_root: Path, gate: ModuleType, tmp_path: Path) -> None:
    synthetic_root, contracts_repo = _build_synthetic_tree(gate, tmp_path)
    result = _run_gate(repo_root / GATE_REL, synthetic_root, contracts_repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS" in result.stdout


def test_gate_fails_on_a_hand_edited_body(repo_root: Path, gate: ModuleType, tmp_path: Path) -> None:
    """THE negative test: a single hand-edit to a vendored copy must fail."""
    synthetic_root, contracts_repo = _build_synthetic_tree(gate, tmp_path)
    vendored_rel, _generated_name, _header = gate.VENDORED[0]
    vendored_path = synthetic_root / vendored_rel
    vendored_path.write_bytes(vendored_path.read_bytes() + b"\n# hand-edited\n")

    result = _run_gate(repo_root / GATE_REL, synthetic_root, contracts_repo)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "FAIL" in result.stdout
    assert str(vendored_rel) in result.stdout


def test_gate_fails_on_a_hand_edited_header(repo_root: Path, gate: ModuleType, tmp_path: Path) -> None:
    """The vendored header is part of the expected content, not a skipped prefix."""
    synthetic_root, contracts_repo = _build_synthetic_tree(gate, tmp_path)
    vendored_rel, _generated_name, _header = gate.VENDORED[0]
    vendored_path = synthetic_root / vendored_rel
    # Change the recorded pin in the header only; the generated body is untouched.
    vendored_path.write_bytes(
        vendored_path.read_bytes().replace(SYNTHETIC_PIN.encode(), b"0badc0de", 1)
    )

    result = _run_gate(repo_root / GATE_REL, synthetic_root, contracts_repo)
    assert result.returncode == 1, result.stdout + result.stderr


def test_gate_fails_when_a_contracts_artifact_is_missing(
    repo_root: Path, gate: ModuleType, tmp_path: Path
) -> None:
    """A present contracts repo with an incomplete generated/ is a failure, not a skip."""
    synthetic_root, contracts_repo = _build_synthetic_tree(gate, tmp_path)
    (contracts_repo / "generated" / "contract.ts").unlink()

    result = _run_gate(repo_root / GATE_REL, synthetic_root, contracts_repo)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "incomplete" in result.stdout


def test_gate_skips_when_the_contracts_repo_is_absent(
    repo_root: Path, gate: ModuleType, tmp_path: Path
) -> None:
    """No sibling contracts checkout -> a clear SKIP, exit 0 (never a hard fail)."""
    synthetic_root, _contracts_repo = _build_synthetic_tree(gate, tmp_path)
    absent = tmp_path / "does-not-exist"

    result = _run_gate(repo_root / GATE_REL, synthetic_root, absent)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SKIP" in result.stdout


def test_real_vendored_copies_match_the_pinned_contracts_repo(
    repo_root: Path, gate: ModuleType
) -> None:
    """End-to-end: the committed copies match the real contracts checkout.

    Skipped (not silently passed) when the pinned sibling checkout is absent, so
    this asserts the real pin on a developer machine without breaking CI.
    """
    import os

    contracts_repo = Path(
        os.environ.get("CONTRACTS_REPO", gate.DEFAULT_CONTRACTS_REPO)
    )
    if not contracts_repo.is_dir():
        pytest.skip(f"contracts repo not present at {contracts_repo}")

    errors = gate.check(repo_root, contracts_repo, gate.read_pin(repo_root))
    assert errors == []
