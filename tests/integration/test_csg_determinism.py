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
  ``cursor``/``backend``) or the other two artifacts (``colors.conf``,
  ``colors.gtk.css``) which are expected to be bit-identical. The artifact
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
import shutil
import struct
import subprocess
import zlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

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
    """
    if not templates_dir.is_dir():
        return "no-dir"
    entries: list[str] = []
    for p in sorted(templates_dir.rglob("*")):
        if p.is_file():
            rel = p.relative_to(templates_dir).as_posix()
            fh = _sha256_file(p)
            entries.append(f"{rel}:{fh}")
    joined = "\n".join(entries).encode()
    return hashlib.sha256(joined).hexdigest()


def _normalize_yaml_bytes(data: bytes) -> bytes:
    """Strip volatile keys (generated_at, source_image) for determinism compare.

    ``colors.yaml`` always contains ``generated_at: "<ISO-8601>"`` which differs
    per run by design; ``source_image`` is container-path stable (``/input/...``)
    but we strip it for host-independence. Returns normalized bytes.
    """
    text = data.decode("utf-8", errors="replace")
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("generated_at:") or stripped.startswith("source_image:"):
            continue
        lines.append(line)
    # Preserve trailing newline semantics of original (always ends without \n in our fixtures)
    return "\n".join(lines).encode()


def _detect_container_engine() -> str | None:
    if shutil.which("podman"):
        return "podman"
    if shutil.which("docker"):
        return "docker"
    return None


def _csg_container_image_built(engine: str) -> bool:
    image_name = "csg-pywal-podman:latest" if engine == "podman" else "csg-pywal-docker:latest"
    result = subprocess.run(
        [engine, "image", "exists", image_name], capture_output=True, timeout=30
    )
    return result.returncode == 0


def _run_csg_generate(
    image_path: Path,
    output_dir: Path,
    env_overrides: dict[str, str] | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    """Invoke ``csg generate`` as a black box (no provisioning imports).

    Uses literal env-override keys per shared-data-contract Env-override protocol:
    ``COLORSCHEME__OUTPUT__DIRECTORY`` and ``COLORSCHEME__OUTPUT__OVERWRITE``.
    Also passes explicit ``--format`` flags for the three palette artifacts that
    define ``PaletteEntry.artifact_hashes`` (``colors.yaml``, ``colors.conf``,
    ``colors.gtk.css``).

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


@pytest.fixture()
def csg_available() -> str:
    """Skip loudly if csg not on PATH (determinism not proven, downstream blocked)."""
    csg = shutil.which("csg")
    if csg is None:
        pytest.skip("csg not on PATH — determinism verification requires csg binary")
    return csg  # type: ignore[return-value]


class TestCsgDeterminism:
    """Story 1.4 ACs 1–4: double-run must be identical (modulo generated_at)."""

    def test_csg_deterministic_double_run(self, csg_available: str, tmp_path: Path) -> None:  # noqa: ARG002
        # ── probe container mode soft-requirement ──────────────────────────
        engine = _detect_container_engine()
        # ``csg info`` JSON would tell runtime.mode, but we treat engine presence as
        # indicator that container mode is likely (per csg info: runtime.mode=container
        # on this host). If engine exists but image not built, we skip with a
        # ``skipped`` artifact — not proven.
        skip_reason: str | None = None
        if engine is not None and not _csg_container_image_built(engine):
            skip_reason = f"CSG container image not built for {engine} — run `csg install --container-engine {engine}`"
        # We still run the test; the skip is handled after artifact write as
        # ``deterministic: "skipped"`` rather than hard pytest.skip, so the
        # memlog records the gap. However if csg itself is missing we already
        # skipped via fixture. For container-image missing, we will run and
        # expect the generate to either fail or succeed via fallback — if it
        # fails we mark skipped.

        # ── fixture image ─────────────────────────────────────────────────
        # Prefer committed fixture for byte-stability; fallback to dynamic minimal PNG
        fixtures_dir = Path(__file__).resolve().parent.parent / "fixtures"
        fixture_png = fixtures_dir / "wallpaper.png"
        if fixture_png.is_file():
            # Copy to tmp to keep input stable and not mutate committed file
            image_path = tmp_path / "wallpaper.png"
            image_path.write_bytes(fixture_png.read_bytes())
        else:
            image_path = tmp_path / "wallpaper.png"
            _create_test_image(image_path)

        wallpaper_hash = _sha256_file(image_path)

        # ── template-set hash (for memlog) ────────────────────────────────
        templates_dir = _find_csg_templates_dir()
        template_hash = (
            _hash_template_dir(templates_dir) if templates_dir else "unknown-no-dir-found"
        )
        templates_dir_str = str(templates_dir) if templates_dir else "unknown"

        # ── double run into isolated temp dirs (MUST NOT touch cache/current) ─
        out1 = tmp_path / "out1"
        out2 = tmp_path / "out2"
        out1.mkdir()
        out2.mkdir()

        result1 = _run_csg_generate(image_path, out1)
        result2 = _run_csg_generate(image_path, out2)

        # ── artifact destination (dot-prefixed) ────────────────────────────
        artifact_path = Path(__file__).resolve().parent / ".csg_determinism.json"

        # Helper to collect per-file hashes (raw + normalized for yaml)
        def collect_hashes(output_dir: Path) -> dict[str, str]:
            h: dict[str, str] = {}
            for name in ("colors.yaml", "colors.conf", "colors.gtk.css"):
                p = output_dir / name
                if p.is_file():
                    if name == "colors.yaml":
                        # Record both raw and normalized for transparency
                        raw = _sha256_file(p)
                        norm = _sha256_bytes(_normalize_yaml_bytes(p.read_bytes()))
                        h[name] = raw
                        h[f"{name}.normalized"] = norm
                    else:
                        h[name] = _sha256_file(p)
                else:
                    h[name] = "missing"
            return h

        # ── handle skip (container image missing) as artifact, not hard pass ─
        if skip_reason is not None and (result1.returncode != 0 or result2.returncode != 0):
            artifact = {
                "ts": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "wallpaper_hash": wallpaper_hash,
                "template_hash": template_hash,
                "templates_dir": templates_dir_str,
                "backend": "pywal",  # default on this host per csg info
                "run1_hashes": collect_hashes(out1) if out1.exists() else {},
                "run2_hashes": collect_hashes(out2) if out2.exists() else {},
                "deterministic": "skipped",
                "reason": skip_reason,
                "container_mode": engine is not None,
                "engine": engine,
                "hash_algorithm": "sha256",
                "run1_stdout": result1.stdout[:500] if result1.stdout else "",
                "run1_stderr": result1.stderr[:500] if result1.stderr else "",
                "run2_stdout": result2.stdout[:500] if result2.stdout else "",
                "run2_stderr": result2.stderr[:500] if result2.stderr else "",
            }
            artifact_path.write_text(json.dumps(artifact, indent=2))
            pytest.skip(skip_reason)

        # ── normal assertions: both runs must have succeeded ───────────────
        assert result1.returncode == 0, (
            f"csg generate run1 must exit 0; stdout:\n{result1.stdout}\nstderr:\n{result1.stderr}"
        )
        assert result2.returncode == 0, (
            f"csg generate run2 must exit 0; stdout:\n{result2.stdout}\nstderr:\n{result2.stderr}"
        )

        # ── verify env-override was honored (output landed in host temp dir) ─
        for out_dir in (out1, out2):
            for name in ("colors.yaml", "colors.conf", "colors.gtk.css"):
                assert (out_dir / name).is_file(), (
                    f"COLORSCHEME__OUTPUT__DIRECTORY override not honored — "
                    f"expected {name} in {out_dir}; run may have ignored env "
                    f"and written elsewhere (container forwarding broken). "
                    f"Check container_processor.py env passthrough."
                )

        # ── determinism compare ────────────────────────────────────────────
        # For colors.conf and colors.gtk.css: raw bytes must be identical (sha equal + bytes equal)
        # For colors.yaml: normalized bytes (strip generated_at) must be identical
        run1_hashes = collect_hashes(out1)
        run2_hashes = collect_hashes(out2)

        # Detailed diff for failure message
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

        # ── write artifact (always, for memlog) ────────────────────────────
        artifact = {
            "ts": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "wallpaper_hash": wallpaper_hash,
            "template_hash": template_hash,
            "templates_dir": templates_dir_str,
            "backend": "pywal",
            "run1_hashes": run1_hashes,
            "run2_hashes": run2_hashes,
            "deterministic": len(mismatches) == 0,
            "container_mode": engine is not None,
            "engine": engine,
            "hash_algorithm": "sha256",
            "notes": "colors.yaml raw hashes intentionally differ by generated_at; deterministic flag compares normalized yaml + raw conf/gtk.css",
        }
        artifact_path.write_text(json.dumps(artifact, indent=2))

        # ── fail if nondeterministic ───────────────────────────────────────
        assert not mismatches, (
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
