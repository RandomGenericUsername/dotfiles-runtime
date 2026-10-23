#!/usr/bin/env python3
"""ci-gates-parity — the workflow's job set must equal `make gates` plus declared CI-only jobs.

WHY THIS EXISTS. A hand-maintained job list drifts from the gate list the first time one side is
edited: a gate that runs locally and never on a push is invisible, and a CI-only job is invisible
to a developer running `make gates`. Both directions are checked here, and the authoritative set
is parsed from the `Makefile` (`GATES`), never copied.

DECLARED DIFFERENCES. A job that is deliberately not a gate is an entry in `CI_ONLY_EXEMPT` with
a reason; a stale entry (naming a job that no longer exists) fails rather than lingering. There is
no gate exclusion today, so no `CI_EXCLUDED_GATES` table exists.

The workflow is parsed as YAML with the standard library: this repo's workflow is a flat `jobs:`
mapping of two-space-indented keys, which is all this check needs. It fails on a missing or
unreadable input rather than skipping.

Usage: ``python3 scripts/ci-gates-parity.py [--root REPO_ROOT]``
Exit 0 = the two sets are equal modulo `CI_ONLY_EXEMPT`.
Exit 1 = an unmatched entry in either direction, a stale declared entry, or a missing input.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

MAKEFILE = "Makefile"
WORKFLOW = ".github/workflows/gates.yml"

# Jobs that are deliberately not `make` gates, with the reason they exist.
CI_ONLY_EXEMPT: dict[str, str] = {
    "ci-gates-parity": "this check: it compares the workflow to GATES and is not itself a gate",
    "release": "tag-only artifact publisher; it needs a GitHub token, not a local gate",
}


def parse_gates(root: Path) -> set[str]:
    text = (root / MAKEFILE).read_text(encoding="utf-8")
    for line in text.splitlines():
        match = re.match(r"^GATES\s*:?=\s*(.+)$", line)
        if match:
            gates = match.group(1).split()
            if not gates:
                raise SystemExit(f"{MAKEFILE}: GATES is empty")
            return set(gates)
    raise SystemExit(f"{MAKEFILE}: could not find a `GATES := ...` assignment")


def parse_jobs(root: Path) -> set[str]:
    text = (root / WORKFLOW).read_text(encoding="utf-8")
    jobs: set[str] = set()
    in_jobs = False
    for line in text.splitlines():
        if not in_jobs:
            if re.match(r"^jobs:\s*$", line):
                in_jobs = True
            continue
        if line and not line.startswith(" "):
            break
        match = re.match(r"^  ([A-Za-z0-9_.-]+):\s*$", line)
        if match:
            jobs.add(match.group(1))
    if not jobs:
        raise SystemExit(f"{WORKFLOW}: no jobs parsed")
    return jobs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args()
    root = Path(args.root)

    gates = parse_gates(root)
    jobs = parse_jobs(root)

    missing_jobs = sorted(gates - jobs)
    undeclared = sorted(jobs - gates - set(CI_ONLY_EXEMPT))
    stale_exempt = sorted(set(CI_ONLY_EXEMPT) - jobs)

    failed = False
    for gate in missing_jobs:
        print(f"FAIL gate `{gate}` is in GATES but has no workflow job", file=sys.stderr)
        failed = True
    for job in undeclared:
        print(
            f"FAIL workflow job `{job}` is not in GATES and not declared in CI_ONLY_EXEMPT",
            file=sys.stderr,
        )
        failed = True
    for job in stale_exempt:
        print(f"FAIL CI_ONLY_EXEMPT names `{job}`, which is not a workflow job", file=sys.stderr)
        failed = True

    if failed:
        print(f"gates={sorted(gates)} jobs={sorted(jobs)}", file=sys.stderr)
        return 1

    print(f"PASS gates={sorted(gates)} jobs={sorted(jobs)} exempt={sorted(CI_ONLY_EXEMPT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
