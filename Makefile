# dotfiles-runtime — fresh-clone rebuild with make.
#
# Fresh machine path:
#   1. `make sync`     (uv-managed env from the committed lock)
#   2. `make test`     (fast suite; integration tests are opt-in)
#   3. `make gates`    (test + lint + contracts-check + hermetic + lock-check + docs-check)
#   4. `make build`    (sdist + wheel in dist/)
#   5. `make install`  (install the built package into the local .venv)
#
# Every target is anchored on $(CURDIR); none assumes a parent checkout.

UV     ?= uv
PYTHON := $(CURDIR)/.venv/bin/python

# The authoritative gate set. `make gates` runs it for a developer; CI runs one
# job per member (`.github/workflows/gates.yml`) and `make ci-gates-parity`
# fails when the two sets differ. Add a gate here AND a job there in one commit.
GATES := test lint contracts-check hermetic lock-check docs-check

# The fast suite: the integration marker is for a real installed CLI or a
# provisioned desktop spine (developer proofs, not stock-runner gates).
FAST := -m "not integration"

.PHONY: help sync install test test-integration lint format gates gates-ci hermetic lock-check docs-check contract-pin-check build ci-gates-parity clean

help:
	@echo "sync              Install the env from uv.lock (uv sync --frozen)"
	@echo "test              Run the fast suite (excludes integration)"
	@echo "test-integration  Run the full suite, including integration tests"
	@echo "lint              Ruff check src/ (imported tests carry pre-existing findings)"
	@echo "format            Ruff format src/"
	@echo "contracts-check   Event-contract conformance + vendored-pin negative test"
	@echo "contract-pin-check Diff vendored bindings against a sibling contracts checkout (optional)"
	@echo "hermetic          Run the fast suite with empty HOME/XDG from a foreign cwd"
	@echo "lock-check        uv lock --check (the committed lock is current)"
	@echo "docs-check        README/CHANGELOG/LICENSE/AGENTS + CLAUDE symlink exist"
	@echo "gates             $(GATES)"
	@echo "ci-gates-parity   Workflow jobs equal GATES + declared CI-only jobs"
	@echo "build             Build sdist + wheel into dist/"
	@echo "install           Install the built package into .venv"
	@echo "clean             Remove dist/ and local caches"

sync:
	$(UV) sync --frozen

install: sync
	$(UV) pip install --reinstall .

test:
	$(UV) run pytest -q $(FAST)

test-integration:
	$(UV) run pytest -q

# Lint covers src/ only, matching the in-tree Makefile this package was
# extracted with. The imported test modules carry pre-existing findings;
# modernizing them is a separate ticket, not part of a birth slice.
lint:
	$(UV) run ruff check src

format:
	$(UV) run ruff format src

# AD-44 conformance: embedded schemas equal the canonical copies, the D-Bus
# wire XML agrees with the event-contract JSON, the baked tables equal the
# vendored generated binding, and the pin-check's negative test still fails on
# a hand-edit. Hermetic — no sibling checkout required.
contracts-check:
	$(UV) run pytest -q \
		tests/unit/test_contract_schema_conformance.py \
		tests/unit/test_event_contract_conformance.py \
		tests/unit/test_event_contract_drift.py \
		tests/unit/test_history_trigger_enum.py \
		tests/unit/test_contract_pin_check.py

# The positive cross-repo check. It needs a sibling `contracts` checkout and
# SKIPS (exit 0) without one, so it is NOT in GATES: a stock runner has no
# `generated/` to diff against. The negative test above proves the gate fails
# on a hand-edit and runs in CI.
contract-pin-check:
	python3 scripts/contract-pin-check.py

hermetic:
	@test -x "$(PYTHON)" || { echo "run 'make sync' first"; exit 1; }
	@tmp=$$(mktemp -d); trap 'rm -rf "$$tmp"' EXIT; \
	cd "$$tmp"; \
	env HOME="$$tmp" \
	    XDG_CONFIG_HOME="$$tmp/config" XDG_CACHE_HOME="$$tmp/cache" \
	    XDG_DATA_HOME="$$tmp/data" XDG_STATE_HOME="$$tmp/state" \
	    TMPDIR="$$tmp" \
	    "$(PYTHON)" -m pytest -q "$(CURDIR)/tests" $(FAST)

lock-check:
	$(UV) lock --check

docs-check:
	@test -f README.md      || { echo "missing README.md"; exit 1; }
	@test -f CHANGELOG.md   || { echo "missing CHANGELOG.md"; exit 1; }
	@test -f LICENSE        || { echo "missing LICENSE"; exit 1; }
	@test -f AGENTS.md      || { echo "missing AGENTS.md"; exit 1; }
	@test -L CLAUDE.md      || { echo "CLAUDE.md must be a symlink to AGENTS.md"; exit 1; }
	@grep -q "MIT License" LICENSE || { echo "LICENSE is not the MIT text"; exit 1; }

gates: $(GATES)

# Alias kept so a CI parity check and a local run name the same set.
gates-ci: $(GATES)

build:
	$(UV) build

ci-gates-parity:
	python3 scripts/ci-gates-parity.py

clean:
	rm -rf dist build .pytest_cache .ruff_cache .mypy_cache
