"""Centralized env builder for CSG/WEG/ITR adapters (Story 1.7 review).

Implements least-privilege allowlist for subprocess env forwarded into
OCI containers via ``csg``'s ``container_processor``. Replaces
``dict(os.environ)`` passthrough to avoid secret exfiltration
(``GITHUB_TOKEN``, ``AWS_*``, ``*_SECRET*`` etc.) and handles
``E2BIG`` (argument list too long) explicitly.

Scalable: single source for all three adapters (CSG/WEG/ITR) — future
adapters reuse ``build_env`` without duplicating allowlists.

AD-7: adapter only sets host ``env=``; ``csg`` forwards into
``RunConfig``. This module builds the host env dict.
"""

from __future__ import annotations

import errno
import os

# Exact keys always allowed (needed for csg/shim/poc + hermetic determinism)
_ALLOWED_EXACT: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
        "TERM",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "https_proxy",
        "http_proxy",
        "no_proxy",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
    }
)

# Prefixes allowed — covers all env-override protocol keys and XDG
_ALLOWED_PREFIXES: tuple[str, ...] = (
    "COLORSCHEME__",
    "WALLPAPER__",
    "ICON_RENDERER__",
    "XDG_",
)

# Substrings that indicate secrets — block even if prefix/exact would allow
_BLOCKED_SUBSTRINGS: tuple[str, ...] = (
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "AWS_",
    "GITHUB_",
)


def _is_allowed(key: str) -> bool:
    # Block secrets first (takes precedence)
    if any(s in key for s in _BLOCKED_SUBSTRINGS):
        return False
    if key in _ALLOWED_EXACT:
        return True
    return any(key.startswith(p) for p in _ALLOWED_PREFIXES)


def build_env(overrides: dict[str, str]) -> dict[str, str]:
    """Build allowlisted env dict for subprocess.

    Args:
        overrides: literal override keys to inject (e.g.
            ``COLORSCHEME__OUTPUT__DIRECTORY``).

    Returns:
        Filtered env dict containing allowlisted host vars + overrides.

    Raises:
        RuntimeError: if env size would likely hit ``E2BIG`` (ARG_MAX).
    """
    filtered: dict[str, str] = {k: v for k, v in os.environ.items() if _is_allowed(k)}
    filtered.update(overrides)

    # E2BIG guard — estimate size (keys + values + overhead). Linux ARG_MAX
    # is typically 128k-2M; env alone exceeding ~100k is a strong signal.
    # We raise early with a clear message instead of generic "failed to spawn".
    try:
        total = sum(len(k) + len(v) + 1 for k, v in filtered.items())
        # 100k is conservative; actual limit is sysconf(_SC_ARG_MAX) minus args
        if total > 100_000:
            raise OSError(errno.E2BIG, "env too large for subprocess")
    except OSError as exc:
        if exc.errno == errno.E2BIG:
            raise RuntimeError(
                f"env too large for subprocess (estimated {total} bytes, "
                f"{len(filtered)} vars); output_dir may be too long or host env huge"
            ) from exc
        raise
    return filtered
