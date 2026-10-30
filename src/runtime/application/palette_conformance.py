"""Palette vocabulary (``palette.v1``) conformance checking (REQ-006).

The producer emits eight palette artifacts; the vocabulary contract only
covers the two named-token projections:

- ``colors.yaml`` — every declared role must be a top-level key with a valid
  ``#rrggbb`` hex value.
- ``colors.gtk.css`` — every declared role must have
  ``@define-color <role> <hex>``.

Extra keys are additive and allowed: they are reported (information only)
and never fail conformance. Missing, renamed, or malformed declared roles are
a typed :class:`runtime.domain.palette_vocabulary.PaletteVocabularyError`.

The runtime deliberately has no YAML dependency (``pyproject.toml`` carries
no pyyaml; importing one would cross AD-15), so the parsers below cover the
exact production shape tpr emits: top-level scalars and one
``@define-color`` declaration per line. They are total (never raise) — the
caller decides between reporting (inspect/doctor) and failing closed (cache
build).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from runtime.domain.palette_vocabulary import (
    PALETTE_VOCABULARY_ROLES,
    PALETTE_VOCABULARY_V1,
    PALETTE_VOCABULARY_VERSION,
    PaletteVocabularyError,
)

__all__ = [
    "ArtifactVocabularyCheck",
    "PaletteConformance",
    "check_palette_artifacts",
    "check_palette_entry",
    "is_semantic_palette",
    "require_palette_conformance",
]

#: Valid hex colors accepted in a palette role value: #rgb, #rrggbb, #rrggbbaa.
_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")

#: Top-level YAML scalar: ``key: value`` (same subset as derive's parser).
_TOP_KEY_RE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_.\-]*):(?:[ \t]+(.*?))?[ \t]*$")

#: One GTK declaration: ``@define-color <role> <value>;`` with optional comment.
_GTK_DEFINE_RE = re.compile(r"^\s*@define-color\s+([^\s;]+)\s+([^;]*?)\s*;\s*(?:/\*.*\*/)?\s*$")


def _clean_yaml_scalar(raw: str) -> str | None:
    """Return a plain scalar value; ``None`` for comments/blocks/collections."""
    text = raw.strip()
    if not text or text.startswith("#"):
        return None
    if text[0] in ('"', "'"):
        end = text.find(text[0], 1)
        if end == -1:
            return None
        return text[1:end]
    if text[0] in ("|", ">", "[", "{", "&", "*", "!", "@", "`"):
        return None
    token = text.split(None, 1)[0]
    if token.startswith("#"):
        return None
    return token


def _parse_colors_yaml(text: str) -> dict[str, str]:
    """Parse top-level ``key: value`` scalars from a ``colors.yaml``.

    Nested blocks (the legacy ``colors:`` list, ``special:``) are skipped by
    construction: only lines with no leading whitespace contribute. A
    key declared without a scalar (a block start) is not a role value and is
    ignored — the role then reports missing, which is the correct verdict.
    """
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if raw_line[0] in (" ", "\t"):
            continue
        match = _TOP_KEY_RE.match(raw_line.strip())
        if match is None:
            continue
        key, raw_value = match.group(1), (match.group(2) or "").strip()
        cleaned = _clean_yaml_scalar(raw_value)
        if cleaned is not None:
            values[key] = cleaned
    return values


def _parse_gtk_css(text: str) -> dict[str, str]:
    """Parse ``@define-color <role> <value>;`` declarations from GTK CSS."""
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith(("/*", "*")):
            continue
        match = _GTK_DEFINE_RE.match(raw_line)
        if match is None:
            continue
        value = match.group(2).split("/*", 1)[0].strip()
        if value:
            values[match.group(1)] = value
    return values


def _check_roles(artifact: str, values: dict[str, str]) -> ArtifactVocabularyCheck:
    present = tuple(role for role in PALETTE_VOCABULARY_V1 if role in values)
    missing = tuple(role for role in PALETTE_VOCABULARY_V1 if role not in values)
    malformed = tuple(
        role for role in PALETTE_VOCABULARY_V1 if role in values and not _HEX_RE.match(values[role])
    )
    extra = tuple(sorted(key for key in values if key not in PALETTE_VOCABULARY_ROLES))
    return ArtifactVocabularyCheck(
        artifact=artifact,
        present_roles=present,
        missing_roles=missing,
        malformed_roles=malformed,
        extra_roles=extra,
    )


@dataclass(frozen=True, slots=True)
class ArtifactVocabularyCheck:
    """Per-artifact conformance verdict (one of the two named-token files)."""

    artifact: str
    present_roles: tuple[str, ...]
    missing_roles: tuple[str, ...]
    malformed_roles: tuple[str, ...]
    extra_roles: tuple[str, ...]
    error: str | None = None
    """Read/parse failure (``None`` when the artifact was read)."""

    @property
    def conforming(self) -> bool:
        return self.error is None and not self.missing_roles and not self.malformed_roles


@dataclass(frozen=True, slots=True)
class PaletteConformance:
    """Combined ``palette.v1`` conformance for one palette entry."""

    vocabulary_version: str
    artifacts: tuple[ArtifactVocabularyCheck, ...]

    @property
    def conforming(self) -> bool:
        return all(check.conforming for check in self.artifacts)

    @property
    def artifact_names(self) -> tuple[str, ...]:
        return tuple(check.artifact for check in self.artifacts)

    @property
    def missing_roles(self) -> tuple[str, ...]:
        """Declared roles missing from at least one artifact, declaration order."""
        return tuple(
            role
            for role in PALETTE_VOCABULARY_V1
            if any(role in check.missing_roles for check in self.artifacts)
        )

    @property
    def malformed_roles(self) -> tuple[str, ...]:
        """Declared roles with a non-hex value in at least one artifact."""
        return tuple(
            role
            for role in PALETTE_VOCABULARY_V1
            if any(role in check.malformed_roles for check in self.artifacts)
        )

    @property
    def extra_roles(self) -> tuple[str, ...]:
        """Union of additive keys across artifacts (sorted, info only)."""
        return tuple(sorted({role for check in self.artifacts for role in check.extra_roles}))

    @property
    def present_roles(self) -> tuple[str, ...]:
        """Union of declared roles found in at least one artifact."""
        return tuple(
            role
            for role in PALETTE_VOCABULARY_V1
            if any(role in check.present_roles for check in self.artifacts)
        )

    @property
    def semantic(self) -> bool:
        """True when the palette carries the semantic signature (tpr shape).

        Legacy csg palettes expose only ``background``/``foreground``/16 ANSI
        colors (csg retires in REQ-007); they are not ``palette.v1``
        producers. Reporting uses this to say "not applicable" instead of
        dirtying a healthy legacy desktop.
        """
        return "on_surface" in self.present_roles and "surface" in self.present_roles

    def errors(self) -> tuple[str, ...]:
        return tuple(check.error for check in self.artifacts if check.error)

    def to_dict(self) -> dict[str, object]:
        """JSON-shaped projection for inspect/doctor output."""
        return {
            "vocabulary_version": self.vocabulary_version,
            "conforming": self.conforming,
            "semantic": self.semantic,
            "missing_roles": list(self.missing_roles),
            "malformed_roles": list(self.malformed_roles),
            "extra_roles": list(self.extra_roles),
            "artifacts": [
                {
                    "artifact": check.artifact,
                    "conforming": check.conforming,
                    "missing_roles": list(check.missing_roles),
                    "malformed_roles": list(check.malformed_roles),
                    "extra_roles": list(check.extra_roles),
                    "error": check.error,
                }
                for check in self.artifacts
            ],
        }

    def require(self) -> None:
        """Raise the typed fail-closed error unless every artifact conforms."""
        if self.conforming:
            return
        parts: list[str] = []
        for check in self.artifacts:
            details: list[str] = []
            if check.error:
                details.append(check.error)
            if check.missing_roles:
                details.append("missing role(s): " + ", ".join(check.missing_roles))
            if check.malformed_roles:
                details.append("malformed role(s): " + ", ".join(check.malformed_roles))
            if details:
                parts.append(f"{check.artifact}: " + "; ".join(details))
        message = f"palette vocabulary v{self.vocabulary_version} nonconforming: " + " | ".join(
            parts
        )
        raise PaletteVocabularyError(
            message,
            vocabulary_version=self.vocabulary_version,
            missing_roles=self.missing_roles,
            malformed_roles=self.malformed_roles,
            artifact_errors=self.errors(),
        )


def check_palette_artifacts(*, colors_yaml: str, colors_gtk_css: str) -> PaletteConformance:
    """Check both named-token artifacts from their text (pure, no I/O)."""
    return PaletteConformance(
        vocabulary_version=PALETTE_VOCABULARY_VERSION,
        artifacts=(
            _check_roles("colors.yaml", _parse_colors_yaml(colors_yaml)),
            _check_roles("colors.gtk.css", _parse_gtk_css(colors_gtk_css)),
        ),
    )


def _check_artifact_text(artifact: str, text: str) -> ArtifactVocabularyCheck:
    values = _parse_colors_yaml(text) if artifact == "colors.yaml" else _parse_gtk_css(text)
    return _check_roles(artifact, values)


def _read_artifact(path: Path) -> tuple[str | None, str | None]:
    try:
        return path.read_text(encoding="utf-8"), None
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"cannot read {path.name}: {exc}"


def check_palette_entry(entry_dir: Path) -> PaletteConformance:
    """Read + check a built palette entry directory (read-only).

    A missing/unreadable artifact is reported as nonconforming with the full
    declared vocabulary missing and a populated ``error`` — never silently
    treated as "legacy". Only :attr:`PaletteConformance.semantic` classifies
    a readable palette as a non-v1 producer shape.
    """
    checks: list[ArtifactVocabularyCheck] = []
    for artifact in ("colors.yaml", "colors.gtk.css"):
        text, error = _read_artifact(entry_dir / artifact)
        if error is not None or text is None:
            checks.append(
                ArtifactVocabularyCheck(
                    artifact=artifact,
                    present_roles=(),
                    missing_roles=PALETTE_VOCABULARY_V1,
                    malformed_roles=(),
                    extra_roles=(),
                    error=error or f"cannot read {artifact}",
                )
            )
            continue
        checks.append(_check_artifact_text(artifact, text))
    return PaletteConformance(
        vocabulary_version=PALETTE_VOCABULARY_VERSION,
        artifacts=tuple(checks),
    )


def require_palette_conformance(entry_dir: Path) -> PaletteConformance:
    """Fail closed unless the palette entry conforms to ``palette.v1``."""
    conformance = check_palette_entry(entry_dir)
    conformance.require()
    return conformance


def is_semantic_palette(entry_dir: Path) -> bool:
    """True when the entry's artifacts carry the semantic palette signature."""
    return check_palette_entry(entry_dir).semantic
