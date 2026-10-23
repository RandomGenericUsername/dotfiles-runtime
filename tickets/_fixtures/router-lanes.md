# Router fixtures + expected lanes

Dry-run inputs for tasks.md 2.1: each fixture maps to one lane of the `/triage` rubric. Cite the covering spec scenario per fixture.

> Location note: this file lives under `tickets/_fixtures/`, which is excluded from item counting — the INDEX completeness check only counts item files (`tickets/*/ticket.md`), so fixtures here never appear in INDEX.md and do not break completeness.

## Fixture A — XS fix → Lane 1 (XS direct)

**Request**: "Fix the typo in the README header (`Environemnt` → `Environment`)."

- `kind:` ops-task (single trivial edit, no parent, no uncertainty)
- `lane:` 1 — XS direct
- Why: trivial, fully understood, one small edit; cheapest safe path is a ticket worked directly with no OpenSpec change.

## Fixture B — single feature → Lane 2 (openspec-single)

**Request**: "Add a `--dry-run` flag to the build command that prints planned steps without executing them."

- `kind:` requirement (one bounded behavior addition)
- `lane:` 2 — openspec-single (one item + one OpenSpec change)
- Why: single bounded feature, understood, fits one proposal/spec/design/tasks slice. (Covers lane-rubric scenario: "single bounded feature → one OpenSpec change".)

## Fixture C — multi-part understood → Lane 3 (ticket-multi, no BMAD)

**Request**: "Add three known checklist items to the ticket template: `repro:`, `observed:`, `expected:` fields, the INDEX entry format for bugs, and the validation rule rejecting repro-less bugs."

- `kind:` requirement (multi-part, all parts specified, no unknowns)
- `lane:` 3 — ticket-multi, no BMAD (one item + N OpenSpec changes)
- Why: multi-part but fully understood with no architectural unknowns, so BMAD SHALL NOT be invoked. (Covers task-router spec "Scenario: BMAD-overkill lane" — WHEN work is multi-part but fully understood THEN no BMAD, plan one item plus N changes.)

## Fixture D — architectural → Lane 4 (bmad-advisory)

**Request**: "Design the universal device framework: device-agnostic core + device packs + user config overlay, starting with STM32 as reference pack."

- `kind:` epic (broad, multi-ticket scope with architectural unknowns)
- `lane:` 4 — bmad-advisory (BMAD specialist advice → spec → changes)
- Why: broad/uncertain/architectural; needs advisory exploration before planning. Triage recommends BMAD vs plain split with reasons and the user selects; BMAD advice never auto-executes. A small-looking epic would still decompose, never build (covers "Scenario: Epic-shaped small request").
