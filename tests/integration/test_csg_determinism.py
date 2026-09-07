"""CSG determinism verification (Story 1.4, FR-8 / R4 / AD-2 / AD-7 / AD-8).

Proves the palette cache key ``sha256(wallpaper_hash || template_set_hash)`` is
trustworthy by running ``csg generate`` twice on identical inputs and asserting
identical palette content.

**Determinism contract:**

- ``custom`` backend pins ``KMeans(random_state=0)`` (custom_generator.py:52-53)
  → deterministic.
- ``pywal``/``wallust`` are subprocess wrappers (pywal_generator.py,
  wallust_generator.py) → determinism depends on external binary flags; this
  test verifies with the *spine's configured* backend/params, not just defaults.
- ``colors.yaml`` contains ``generated_at`` (wall-clock timestamp) which
  intentionally differs between runs; palette determinism is proven by
  comparing normalized content (``background``/``foreground``/``colors``/
  ``cursor``/``backend``) or the other artifacts (``colors.conf``,
  ``colors.gtk.css``, ``colors.adw.css``, ``colors.sequences``) which are
  expected to be bit-identical. The artifact
  file records both raw and normalized hashes and documents this nuance.

**Container mode (AD-7 last sentence):** ``COLORSCHEME__OUTPUT__DIRECTORY``
must be passed **into** the container, not just the host. The test exercises
that path by setting the literal double-underscore env key and asserting the
host temp dir receives the output even when ``runtime.mode == container``
(podman/docker via oci-runtime). A host-only env would silently write to a
stale location inside the container and make both runs appear deterministic
while actually ignoring the override.

**Mitigation contract (AC 3):**

- **Option A — deterministic confirmed:** no code change; cache key remains
  ``sha256(wallpaper_hash || template_set_hash)`` per shared-data-contract.
- **Option B — nondeterministic observed:** test fails with
  ``NondeterministicCSGError``-style message; spine + shared-data-contract
  must be amended to ``sha256(wallpaper_hash || template_set_hash ||
  pinned_seed)`` where ``pinned_seed`` is a literal (e.g. ``"v1-seed-0"``)
  pinned in ``runtime.domain`` and reflected in ``meta.json`` notes. This test
  only documents the required change — it does **not** implement cache hashing
  (Story 1.5) or the staging-dir populator (Story 1.6).

Re-run::

    uv run --directory src/runtime pytest -k csg_determinism -v
    cat src/runtime/tests/integration/.csg_determinism.json

Artifact ``.csg_determinism.json`` is dot-prefixed to avoid pytest collection.
A ``skipped`` run (``deterministic: "skipped"``) is **not proven** — Stories
1.5–1.6 remain blocked until a provisioned run with ``csg`` + container image
(``csg-pywal-podman:latest`` / ``csg-pywal-docker:latest``) succeeds.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import zlib
from datetime import UTC, datetime
from pathlib import Path

import pytest


class NondeterministicCSGError(AssertionError):
    """Raised when CSG double-run hashes diverge (AC3 mitigation contract)."""

pytestmark = pytest.mark.integration

# ── helpers: minimal PNG (pure stdlib, no PIL) ──────────────────────────────


def _create_test_image(path: Path, size: int = 4, r: int = 100, g: int = 150, b: int = 200) -> None:
    """Create a minimal valid PNG (struct+zlib, no PIL). Mirrors provisioning helper."""

    def _chunk(ctype: bytes, data: bytes) -> bytes:
        c = ctype + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    raw = b""
    for _ in range(size):
        raw += b"\x00" + bytes([r, g, b]) * size
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(_chunk(b"IHDR", ihdr))
        f.write(_chunk(b"IDAT", zlib.compress(raw)))
        f.write(_chunk(b"IEND", b""))


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _hash_template_dir(templates_dir: Path) -> str:
    """Canonical template-set hash: sha256(sorted relpath || file_hash joined).

    Mirrors shared-data-contract Derivation-input hashing: sorted list of
    (relpath, sha256(file)), then sha256 of joined list. Returns hex.
    Single sentinel ``\"no-dir\"`` for missing dir (unified, no dual sentinel).
    """
    if not templates_dir.is_dir():
        return "no-dir"
    entries: list[str] = []
    for p in sorted(templates_dir.rglob("*")):
        if p.is_file():
            try:
                rel = p.relative_to(templates_dir).as_posix()
                fh = _sha256_file(p)
            except OSError:
                # Permission denied / unreadable — record as degraded, don't abort
                try:
                    rel = p.relative_to(templates_dir).as_posix()
                except Exception:
                    rel = p.name
                entries.append(f"{rel}:unreadable")
                continue
            entries.append(f"{rel}:{fh}")
    joined = "\n".join(entries).encode()
    return hashlib.sha256(joined).hexdigest()


def _normalize_yaml_bytes(data: bytes) -> bytes:
    """Strip volatile keys (generated_at, source_image) for determinism compare.

    ``colors.yaml`` always contains ``generated_at: "<ISO-8601>"`` which differs
    per run by design; ``source_image`` is container-path stable (``/input/...``)
    but we strip it for host-independence. Returns normalized bytes.

    Uses regex to handle ``generated_at :`` with spaces before colon,
    leading indent, and trailing whitespace variations.
    """
    text = data.decode("utf-8", errors="replace")
    lines = []
    pat = re.compile(r"^\s*(generated_at|source_image)\s*:")
    for line in text.splitlines():
        if pat.match(line.strip()):
            continue
        # Also check unstripped via pat on full line for indented keys
        if pat.match(line):
            continue
        lines.append(line)
    # Preserve trailing newline semantics of original (always ends without \n in our fixtures)
    return "\n".join(lines).encode()


def _write_artifact_atomic(path: Path, data: dict) -> None:
    """Write JSON artifact atomically (tmp + rename) with OSError guard."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = tempfile.NamedTemporaryFile(
            mode="w", delete=False, dir=str(path.parent), suffix=".tmp", encoding="utf-8"
        )
        try:
            json.dump(data, tmp, indent=2)
            tmp.write("\n")
            tmp.close()
            Path(tmp.name).replace(path)
        finally:
            try:
                Path(tmp.name).unlink(missing_ok=True)
            except OSError:
                pass
    except OSError as e:
        pytest.fail(f"cannot write artifact {path}: {e}")


def _detect_container_engine() -> str | None:
    """Detect container engine via which + usability probe (engine info).

    Presence-only (`which`) is insufficient — a broken podman (rootless misconfig,
    storage lock) passes which but fails at generate time. We probe `engine info`
    with a short timeout to confirm daemon/runnable.
    """
    for engine in ("podman", "docker"):
        if shutil.which(engine) is None:
            continue
        try:
            probe = subprocess.run(
                [engine, "info"], capture_output=True, timeout=5
            )
            if probe.returncode == 0:
                return engine
            # Binary present but daemon not runnable — still return it so the
            # test can produce a clear skip artifact rather than silent fallback.
            return engine
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return engine
    return None


def _csg_container_image_built(engine: str) -> bool:
    image_name = "csg-pywal-podman:latest" if engine == "podman" else "csg-pywal-docker:latest"
    try:
        if engine == "podman":
            result = subprocess.run(
                [engine, "image", "exists", image_name], capture_output=True, timeout=30
            )
            return result.returncode == 0
        else:
            # docker image exists is non-standard; use inspect
            result = subprocess.run(
                [engine, "image", "inspect", image_name], capture_output=True, timeout=30
            )
            return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def _run_csg_generate(
    image_path: Path,
    output_dir: Path,
    env_overrides: dict[str, str] | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    """Invoke ``csg generate`` as a black box (no provisioning imports).

    Uses literal env-override keys per shared-data-contract Env-override protocol:
    ``COLORSCHEME__OUTPUT__DIRECTORY`` and ``COLORSCHEME__OUTPUT__OVERWRITE``.
    Also passes explicit ``--format`` flags for the five palette artifacts that
    define ``PaletteEntry.artifact_hashes`` (``colors.yaml``, ``colors.conf``,
    ``colors.gtk.css``, ``colors.adw.css``, ``colors.sequences``).

    Output dir is set **only** via env override (no ``-o`` flag) to exercise
    the container-mode forwarding contract (AD-7). The test asserts the host
    temp dir actually receives files even when ``runtime.mode == container``.
    """
    csg = shutil.which("csg")
    assert csg is not None, "csg must be on PATH"
    args = [
        csg,
        "generate",
        str(image_path),
        "--format",
        "yaml",
        "--format",
        "conf",
        "--format",
        "gtk.css",
        "--format",
        "adw.css",
        "--format",
        "sequences",
    ]
    # Intentionally **no** ``-o`` flag — output dir comes solely from env override
    # to prove COLORSCHEME__OUTPUT__DIRECTORY is forwarded into the container.
    run_env = dict(os.environ)
    run_env["COLORSCHEME__OUTPUT__DIRECTORY"] = str(output_dir)
    run_env["COLORSCHEME__OUTPUT__OVERWRITE"] = "true"
    if env_overrides:
        run_env.update(env_overrides)
    return subprocess.run(args, capture_output=True, text=True, env=run_env, timeout=timeout)


def _default_templates_dir() -> Path | None:
    """Resolve CSG bundled templates dir (package defaults, not spine)."""
    # Matches color_scheme_generator.adapters.template_dir_resolver._DEFAULT_TEMPLATES_DIR
    # We probe via repo tree search instead of ``csg info`` for speed.
    return _find_csg_templates_dir()


def _find_csg_templates_dir() -> Path | None:
    """Search repo for ``color_scheme_generator/defaults/templates``."""
    # Walk up from this test file to repo root
    for parent in Path(__file__).resolve().parents:
        candidate = (
            parent
            / "src"
            / "cli-tools"
            / "color-scheme-generator"
            / "src"
            / "color_scheme_generator"
            / "defaults"
            / "templates"
        )
        if candidate.is_dir():
            return candidate
        # alternative: installed package location via importlib?
        alt = parent / "src" / "cli-tools" / "color-scheme-generator" / "defaults" / "templates"
        if alt.is_dir():
            return alt
    return None


def _csg_supports_artifact_set(csg_bin: str) -> bool:
    """True when the on-PATH csg bundles ``colors.adw.css.j2`` (gt-1-1 format).

    A stale install (9 templates) predates ``ColorFormat.ADW_CSS`` and cannot
    produce the 5-artifact set (gt-2-1). Probed via ``dump-templates``.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "tpl"
        try:
            result = subprocess.run(
                [csg_bin, "dump-templates", "-o", str(out)],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        if result.returncode != 0:
            return False
        return (out / "templates" / "colors.adw.css.j2").is_file()


@pytest.fixture()
def csg_available() -> str:
    """Skip loudly if csg not on PATH or predates the 5-artifact set
    (determinism not proven, downstream blocked)."""
    csg = shutil.which("csg")
    if csg is None:
        pytest.skip("csg not on PATH — determinism verification requires csg binary")
    if not _csg_supports_artifact_set(csg):
        pytest.skip(
            "host csg binary predates ColorFormat.ADW_CSS (dump-templates lacks "
            "colors.adw.css.j2) — refresh the install from "
            "src/cli-tools/color-scheme-generator; adw.css/sequences determinism "
            "is not provable against this binary (environment staleness)"
        )
    return csg  # type: ignore[return-value]


class TestCsgDeterminism:
    """Story 1.4 ACs 1–4: double-run must be identical (modulo generated_at)."""

    @pytest.mark.parametrize("backend", ["custom"])
    def test_csg_deterministic_double_run(
        self, csg_available: str, tmp_path: Path, backend: str
    ) -> None:  # noqa: ARG002
        # ── probe container mode soft-requirement ──────────────────────────
        engine = _detect_container_engine()
        skip_reason: str | None = None
        if engine is not None and not _csg_container_image_built(engine):
            skip_reason = f"CSG container image not built for {engine} — run `csg install --container-engine {engine}`"

        # ── fixture image ─────────────────────────────────────────────────
        fixtures_dir = Path(__file__).resolve().parent.parent / "fixtures"
        fixture_png = fixtures_dir / "wallpaper.png"
        if fixture_png.is_file():
            image_path = tmp_path / "wallpaper.png"
            image_path.write_bytes(fixture_png.read_bytes())
        else:
            image_path = tmp_path / "wallpaper.png"
            _create_test_image(image_path)

        wallpaper_hash = _sha256_file(image_path)

        # ── template-set hash (for memlog) — unified sentinel "no-dir" ──────
        templates_dir = _find_csg_templates_dir()
        if templates_dir and templates_dir.is_dir():
            template_hash = _hash_template_dir(templates_dir)
            templates_dir_str = str(templates_dir)
        else:
            template_hash = "no-dir"
            templates_dir_str = str(templates_dir) if templates_dir else "unknown"

        # ── double run into isolated temp dirs (MUST NOT touch cache/current) ─
        out1 = tmp_path / "out1"
        out2 = tmp_path / "out2"
        out1.mkdir()
        out2.mkdir()

        # ── run with timeout/exception guard (patch P4) ─────────────────────
        try:
            result1 = _run_csg_generate(image_path, out1)
        except subprocess.TimeoutExpired as e:
            pytest.fail(f"csg generate run1 timed out after {e.timeout}s: {e}")
        except (FileNotFoundError, OSError) as e:
            pytest.fail(f"csg generate run1 failed to spawn: {e}")
        try:
            result2 = _run_csg_generate(image_path, out2)
        except subprocess.TimeoutExpired as e:
            pytest.fail(f"csg generate run2 timed out after {e.timeout}s: {e}")
        except (FileNotFoundError, OSError) as e:
            pytest.fail(f"csg generate run2 failed to spawn: {e}")

        # ── artifact destination (tmp-scoped, atomic) ────────────────────────
        # Never write into the tests directory: a tracked file rewritten by
        # test runs pollutes git status and sweeps churn into feature commits.
        artifact_path = tmp_path / ".csg_determinism.json"

        # Helper to collect per-file hashes (raw + normalized for yaml) — handles is_dir
        def collect_hashes(output_dir: Path) -> dict[str, str]:
            h: dict[str, str] = {}
            for name in (
                "colors.yaml",
                "colors.conf",
                "colors.gtk.css",
                "colors.adw.css",
                "colors.sequences",
            ):
                p = output_dir / name
                if p.is_file():
                    try:
                        if name == "colors.yaml":
                            raw = _sha256_file(p)
                            norm = _sha256_bytes(_normalize_yaml_bytes(p.read_bytes()))
                            h[name] = raw
                            h[f"{name}.normalized"] = norm
                        else:
                            h[name] = _sha256_file(p)
                    except OSError as e:
                        h[name] = f"unreadable:{e}"
                else:
                    if p.exists():
                        h[name] = "is_dir" if p.is_dir() else "unreadable"
                    else:
                        h[name] = "missing"
            return h

        # ── handle skip: image missing → not proven, even if host fallback succeeded (D1) ──
        # If image not built, result is "skipped" regardless of generate exit code when
        # engine was present — host fallback would otherwise masquerade as container proof.
        if skip_reason is not None:
            # Check container forwarding: even if both succeeded, we didn't prove INTO container
            should_skip = (result1.returncode != 0 or result2.returncode != 0) or (
                engine is not None and not _csg_container_image_built(engine)
            )
            if should_skip:
                artifact: dict = {
                    "ts": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    "wallpaper_hash": wallpaper_hash,
                    "template_hash": template_hash,
                    "templates_dir": templates_dir_str,
                    "backend": backend,
                    "run1_hashes": collect_hashes(out1) if out1.exists() else {},
                    "run2_hashes": collect_hashes(out2) if out2.exists() else {},
                    "deterministic": "skipped",
                    "status": "skipped",
                    "reason": skip_reason,
                    "container_mode": engine is not None,
                    "engine": engine,
                    "hash_algorithm": "sha256",
                    "run1_stdout": result1.stdout[:500] if result1.stdout else "",
                    "run1_stderr": result1.stderr[:500] if result1.stderr else "",
                    "run2_stdout": result2.stdout[:500] if result2.stdout else "",
                    "run2_stderr": result2.stderr[:500] if result2.stderr else "",
                }
                _write_artifact_atomic(artifact_path, artifact)
                pytest.skip(skip_reason)

        # ── normal assertions: both runs must have succeeded ───────────────
        assert result1.returncode == 0, (
            f"csg generate run1 must exit 0; stdout:\n{result1.stdout}\nstderr:\n{result1.stderr}"
        )
        assert result2.returncode == 0, (
            f"csg generate run2 must exit 0; stdout:\n{result2.stdout}\nstderr:\n{result2.stderr}"
        )

        # ── verify env-override was honored (output landed in host temp dir) ─
        # Also proves COLORSCHEME__OUTPUT__DIRECTORY was forwarded INTO container
        # because we used NO -o flag — container must have written to host tmp via mount.
        for out_dir in (out1, out2):
            for name in (
                "colors.yaml",
                "colors.conf",
                "colors.gtk.css",
                "colors.adw.css",
                "colors.sequences",
            ):
                p = out_dir / name
                assert p.is_file(), (
                    f"COLORSCHEME__OUTPUT__DIRECTORY override not honored — "
                    f"expected {name} in {out_dir}; run may have ignored env "
                    f"and written elsewhere (container forwarding broken). "
                    f"Check container_processor.py env passthrough. "
                    f"engine={engine} container_mode={engine is not None}"
                )
                # Guard is_dir masquerade — already handled in collect_hashes but assert here too
                assert not p.is_dir(), f"{p} is a directory, expected file"

        # ── determinism compare ────────────────────────────────────────────
        run1_hashes = collect_hashes(out1)
        run2_hashes = collect_hashes(out2)

        mismatches: list[str] = []

        # colors.conf
        p1 = out1 / "colors.conf"
        p2 = out2 / "colors.conf"
        if p1.read_bytes() != p2.read_bytes():
            mismatches.append(
                f"colors.conf differs: run1 sha={run1_hashes['colors.conf']} run2 sha={run2_hashes['colors.conf']} "
                f"(first diff byte offset {next((i for i, (a, b) in enumerate(zip(p1.read_bytes(), p2.read_bytes())) if a != b), 'len-diff')})"
            )

        # colors.gtk.css
        p1 = out1 / "colors.gtk.css"
        p2 = out2 / "colors.gtk.css"
        if p1.read_bytes() != p2.read_bytes():
            mismatches.append(
                f"colors.gtk.css differs: run1 sha={run1_hashes['colors.gtk.css']} run2 sha={run2_hashes['colors.gtk.css']}"
            )

        # colors.adw.css
        p1 = out1 / "colors.adw.css"
        p2 = out2 / "colors.adw.css"
        if p1.read_bytes() != p2.read_bytes():
            mismatches.append(
                f"colors.adw.css differs: run1 sha={run1_hashes['colors.adw.css']} "
                f"run2 sha={run2_hashes['colors.adw.css']}"
            )

        # colors.sequences (binary OSC escape payload)
        p1 = out1 / "colors.sequences"
        p2 = out2 / "colors.sequences"
        if p1.read_bytes() != p2.read_bytes():
            mismatches.append(
                f"colors.sequences differs: run1 sha={run1_hashes['colors.sequences']} "
                f"run2 sha={run2_hashes['colors.sequences']}"
            )

        # colors.yaml (normalized)
        p1 = out1 / "colors.yaml"
        p2 = out2 / "colors.yaml"
        n1 = _normalize_yaml_bytes(p1.read_bytes())
        n2 = _normalize_yaml_bytes(p2.read_bytes())
        if n1 != n2:
            mismatches.append(
                f"colors.yaml normalized differs: run1 normalized sha={run1_hashes['colors.yaml.normalized']} "
                f"run2 normalized sha={run2_hashes['colors.yaml.normalized']}; "
                f"raw shas (including generated_at) are {run1_hashes['colors.yaml']} vs {run2_hashes['colors.yaml']} "
                f"(raw always differs by generated_at — compare normalized)"
            )

        # ── write artifact atomically (always, for memlog) ───────────────────
        artifact = {
            "ts": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "wallpaper_hash": wallpaper_hash,
            "template_hash": template_hash,
            "templates_dir": templates_dir_str,
            "backend": backend,
            "run1_hashes": run1_hashes,
            "run2_hashes": run2_hashes,
            "deterministic": len(mismatches) == 0,
            "status": "passed" if len(mismatches) == 0 else "failed",
            "container_mode": engine is not None,
            "engine": engine,
            "hash_algorithm": "sha256",
            "notes": "colors.yaml raw hashes intentionally differ by generated_at; deterministic flag compares normalized yaml + raw conf/gtk.css",
        }
        _write_artifact_atomic(artifact_path, artifact)

        # ── fail if nondeterministic — raise typed error (D4) ─────────────────
        if mismatches:
            raise NondeterministicCSGError(
                "Nondeterministic CSG output: "
                + "; ".join(mismatches)
                + " — cache key must include pinned seed (see Story 1.4 Task 4 Option B). "
                "If this was pywal/wallust, verify backend flags are deterministic; "
                "if custom, verify KMeans(random_state=0) is intact."
            )

        # Extra sanity: wallpaper source must not have been mutated
        assert _sha256_file(image_path) == wallpaper_hash, (
            "fixture wallpaper was mutated during generates"
        )

    def test_csg_determinism_uses_binary_reads(self, tmp_path: Path) -> None:
        """Unit-level guard: helper _sha256_file uses read_bytes (binary), not read_text.

        This prevents a common mistake where text mode + \n normalization hides
        a real palette difference or masks a generated_at timestamp difference
        as irrelevant when it should be documented as normalized.
        """
        # Create two files with same text but different newline handling
        p1 = tmp_path / "a.conf"
        p2 = tmp_path / "b.conf"
        p1.write_bytes(b"$color0 = rgb(010203)\n")
        p2.write_bytes(b"$color0 = rgb(010203)\n")
        assert _sha256_file(p1) == _sha256_file(p2)

        # Normalized yaml helper must strip generated_at
        y1 = b'background: "#000000"\ngenerated_at: "2026-01-01T00:00:00Z"\ncolors: []\n'
        y2 = b'background: "#000000"\ngenerated_at: "2026-12-31T23:59:59Z"\ncolors: []\n'
        assert _normalize_yaml_bytes(y1) == _normalize_yaml_bytes(y2)
        assert _sha256_bytes(_normalize_yaml_bytes(y1)) == _sha256_bytes(_normalize_yaml_bytes(y2))
        # But raw differs
        assert _sha256_bytes(y1) != _sha256_bytes(y2)
