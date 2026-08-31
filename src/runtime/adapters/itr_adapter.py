"""ITR adapter with env override (Story 1.9, AD-7).

Directs ``itr render`` output via ``ICON_RENDERER__OUTPUT__OUTPUT_DIR``
and palette input via ``ICON_RENDERER__COLOR_SCHEME__PATH`` env overrides
into ``cache/icons/<ih>/`` without editing provisioning's
``settings.toml``.

Container forwarding (AD-7 last sentence):
``itr`` runs in container mode (podman/docker via ``oci-runtime``):
both overrides must be passed **INTO** the container environment,
not just the host process. This adapter only sets the host env dict and
passes it to ``subprocess.run(env=)`` — ``itr``'s own
``container_processor`` forwards the env into ``RunConfig`` via
``oci_runtime.ContainerRuntimePort``. Verified by zero ``-o`` flag pattern
where host ``tmp/out`` still receives artifacts when
``runtime.mode == container``. This adapter never sets Docker/Podman
``-e`` flags or mounts volumes itself.

Future composition (cache-model.md, AD-9):
``populate_via_staging(cache_entry_path(state_root, "icons", ih),
lambda staging: ItrAdapter().render(palette_hash, templates_dir, mappings_path, staging))``
satisfies container forwarding automatically because ``render``'s env
injection works identically per-call. The adapter itself never calls
``populate_via_staging`` — caller owns staging lifecycle and cleanup.
"""

from __future__ import annotations

import errno
import os
import shutil
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from runtime.adapters.env import build_env
from runtime.adapters.hashing import (
    HASH_ALGORITHM,
    canonical_hash_dir,
    hash_file,
    icons_entry_hash,
)
from runtime.domain.models import IconsArtifacts, IconsEntry
from runtime.ports.icon_renderer import IIconRenderer

# Guard — fail-closed on algorithm drift (not assert)
if HASH_ALGORITHM != "sha256":  # pragma: no cover
    raise AssertionError(f"HASH_ALGORITHM must be sha256, got {HASH_ALGORITHM}")

_EMPTY_DIR_HASH = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _find_default_icon_templates() -> Path | None:
    """Search repo for icon-templates defaults."""
    for parent in Path(__file__).resolve().parents:
        candidates = [
            parent
            / "src"
            / "cli-tools"
            / "icon-templates-renderer"
            / "src"
            / "icon_templates_renderer"
            / "defaults"
            / "templates",
            parent / "src" / "cli-tools" / "icon-templates-renderer" / "defaults" / "templates",
            parent / "icon-templates",
            parent / "icon_templates",
        ]
        for candidate in candidates:
            try:
                if candidate.is_dir():
                    return candidate
            except OSError:
                continue
    return None


def _find_default_icon_mappings() -> Path | None:
    """Search repo for icon-mappings defaults."""
    for parent in Path(__file__).resolve().parents:
        candidates = [
            parent
            / "src"
            / "cli-tools"
            / "icon-templates-renderer"
            / "src"
            / "icon_templates_renderer"
            / "defaults"
            / "mappings",
            parent / "src" / "cli-tools" / "icon-templates-renderer" / "defaults" / "mappings",
            parent
            / "src"
            / "cli-tools"
            / "icon-templates-renderer"
            / "src"
            / "icon_templates_renderer"
            / "defaults"
            / "icons.yaml",
            parent / "icon-mappings",
            parent / "icon_mappings",
        ]
        for candidate in candidates:
            try:
                if candidate.exists():
                    return candidate
            except OSError:
                continue
    return None


def _validate_itr_bin(itr_bin: str | None) -> str | None:
    if itr_bin is None:
        return None
    if itr_bin == "":
        raise ValueError("itr_bin cannot be empty string (use None for default 'itr')")
    if "\x00" in itr_bin or "\n" in itr_bin or "\r" in itr_bin:
        raise ValueError(f"itr_bin contains control character: {itr_bin!r}")
    if any(c in itr_bin for c in " \t;|&`$><*?~!()[]{}#\\'\""):
        raise ValueError(f"itr_bin contains shell/whitespace metacharacter: {itr_bin!r}")
    return itr_bin


def _validate_output_dir(output_dir: Path, expected_hash: str) -> None:
    s = str(output_dir)
    if "\x00" in s:
        raise ValueError(f"output_dir contains null byte: {output_dir!r}")
    if s in ("", "."):
        raise ValueError(f"output_dir must be a non-empty path, got {output_dir!r}")
    if ".." in output_dir.parts or "." in output_dir.parts:
        raise ValueError(f"output_dir must not contain '.' or '..': {output_dir}")
    if output_dir.is_symlink():
        raise ValueError(f"output_dir must not be a symlink: {output_dir}")
    if output_dir.parent.is_symlink():
        raise ValueError(f"output_dir parent must not be a symlink: {output_dir.parent}")
    try:
        for p in (output_dir.parent, output_dir.parent.parent):
            if p != Path(".") and p.exists() and p.is_symlink():
                raise ValueError(f"output_dir ancestor must not be a symlink: {p}")
    except OSError as exc:
        raise RuntimeError(f"cannot validate output_dir symlink: {exc}") from exc
    if output_dir.name != expected_hash:
        raise ValueError(
            f"output_dir hash mismatch: expected {expected_hash}, got {output_dir.name}"
        )
    if len(str(output_dir)) > 4000:
        raise ValueError(f"output_dir path too long ({len(str(output_dir))} chars): {output_dir}")


class ItrAdapter(IIconRenderer):
    """Adapter that invokes ``itr render`` via env overrides.

    Location: ``src/runtime/src/runtime/adapters/itr_adapter.py``
    (mirrors ``csg_adapter.py``/``weg_adapter.py`` for layered cache).
    """

    def __init__(
        self,
        timeout: int = 60,
        itr_bin: str | None = None,
        templates_dir: Path | None = None,
        mappings_path: Path | None = None,
        palette_cache_dir: Path | None = None,
    ) -> None:
        if type(timeout) is not int:
            raise ValueError(f"timeout must be int, got {type(timeout).__name__}")
        if timeout <= 0:
            raise ValueError(f"timeout must be > 0, got {timeout}")
        if templates_dir is not None and not isinstance(templates_dir, Path):
            raise TypeError(
                f"templates_dir must be Path or None, got {type(templates_dir).__name__}"
            )
        if mappings_path is not None and not isinstance(mappings_path, Path):
            raise TypeError(
                f"mappings_path must be Path or None, got {type(mappings_path).__name__}"
            )
        if palette_cache_dir is not None and not isinstance(palette_cache_dir, Path):
            raise TypeError(
                f"palette_cache_dir must be Path or None, got {type(palette_cache_dir).__name__}"
            )
        self._timeout = timeout
        self._itr_bin = _validate_itr_bin(itr_bin)
        self._templates_dir = templates_dir
        self._mappings_path = mappings_path
        self._palette_cache_dir = palette_cache_dir

    def is_available(self) -> bool:
        """Return True if ``itr`` binary is on PATH and executable."""
        bin_name = self._itr_bin or "itr"
        if (
            os.path.sep in bin_name
            or (os.path.altsep and os.path.altsep in bin_name)
            or "\\" in bin_name
        ):
            p = Path(bin_name)
            if p.is_file():
                try:
                    return os.access(p, os.X_OK)
                except OSError:
                    return False
            return shutil.which(bin_name) is not None
        result = shutil.which(bin_name)
        if result is None:
            return False
        try:
            return os.access(result, os.X_OK)
        except OSError:
            return False

    def render(
        self,
        palette_hash: str,
        templates_dir: Path,
        mappings_path: Path,
        output_dir: Path,
    ) -> IconsEntry:
        """Render icons from palette via ``itr`` with env overrides.

        Args:
            palette_hash: 64-char hex of palette entry (ph from CsgAdapter).
            templates_dir: path to icon templates dir (READ-ONLY spine).
            mappings_path: path to icon mappings file or dir (READ-ONLY).
            output_dir: target icons entry dir (state_root/cache/icons/<ih>),
                must have name == icons_entry_hash(palette_hash, templates_hash, mappings_hash).

        Returns:
            IconsEntry with artifact_hashes + generated_at.

        Raises:
            FileNotFoundError: if templates/mappings not found or itr not on PATH,
                or palette colors.yaml missing.
            ValueError: if palette_hash not hex, output_dir mismatch, or symlink.
            RuntimeError: on non-zero itr exit with stderr truncated to 2 KiB.
            TimeoutError: if itr hangs beyond timeout.
        """
        # 1. Validate palette_hash 64-hex
        if not isinstance(palette_hash, str):
            raise TypeError(f"palette_hash must be str, got {type(palette_hash).__name__}")
        if "\x00" in palette_hash:
            raise ValueError(f"palette_hash contains null byte: {palette_hash!r}")
        # Use hashing validation (raises ValueError if not hex64)
        from runtime.adapters.hashing import _validate_hex64  # noqa: PLC0415

        _validate_hex64("palette_hash", palette_hash)

        # 2. Resolve templates_dir — prefer explicit render arg, fallback to
        # __init__, else discovery
        # Render arg is authoritative; if it fails we raise, not silently fallback.
        # But if render arg is placeholder, also consider constructor default.
        effective_templates: Path | None = None
        # If caller passed empty string path treat as missing and fallback.
        # Distinguish by checking if render arg equals constructor default?
        # Simpler: use render arg if it exists and is not None.
        # Since port requires Path, render arg is always Path, so we use it.
        # However if constructor had value and render arg differs, prioritize
        # render arg.
        if templates_dir is not None:
            effective_templates = templates_dir
        elif self._templates_dir is not None:
            effective_templates = self._templates_dir
        else:
            effective_templates = _find_default_icon_templates()

        if effective_templates is None:
            raise FileNotFoundError(
                "ITR templates dir not found: no default templates dir discovered"
            )
        if "\x00" in str(effective_templates):
            raise ValueError(f"templates_dir contains null byte: {effective_templates!r}")
        if not isinstance(effective_templates, Path):
            raise TypeError(f"templates_dir must be Path, got {type(effective_templates).__name__}")
        try:
            if effective_templates.is_symlink():
                raise ValueError(f"templates_dir must not be a symlink: {effective_templates}")
        except OSError as exc:
            raise RuntimeError(f"cannot validate templates_dir symlink: {exc}") from exc
        if not effective_templates.exists():
            raise FileNotFoundError(f"ITR templates dir not found: {effective_templates}")
        if not effective_templates.is_dir():
            raise NotADirectoryError(f"ITR templates dir is not a directory: {effective_templates}")
        try:
            if not any(effective_templates.iterdir()):
                raise FileNotFoundError(f"ITR templates dir is empty: {effective_templates}")
        except OSError as exc:
            raise RuntimeError(f"cannot read templates dir {effective_templates}: {exc}") from exc
        try:
            templates_hash = canonical_hash_dir(effective_templates)
        except OSError as exc:
            raise RuntimeError(f"cannot hash templates dir {effective_templates}: {exc}") from exc
        if templates_hash == _EMPTY_DIR_HASH:
            raise FileNotFoundError(
                f"ITR templates dir is empty (hash is empty): {effective_templates}"
            )

        # 3. Resolve mappings_path — prefer render arg
        effective_mappings: Path | None = None
        if mappings_path is not None:
            effective_mappings = mappings_path
        elif self._mappings_path is not None:
            effective_mappings = self._mappings_path
        else:
            effective_mappings = _find_default_icon_mappings()

        if effective_mappings is None:
            raise FileNotFoundError("ITR mappings not found: no default mappings discovered")
        if "\x00" in str(effective_mappings):
            raise ValueError(f"mappings_path contains null byte: {effective_mappings!r}")
        if not isinstance(effective_mappings, Path):
            raise TypeError(f"mappings_path must be Path, got {type(effective_mappings).__name__}")
        try:
            if effective_mappings.is_symlink():
                raise ValueError(f"mappings_path must not be a symlink: {effective_mappings}")
        except OSError as exc:
            raise RuntimeError(f"cannot validate mappings symlink: {exc}") from exc
        if not effective_mappings.exists():
            raise FileNotFoundError(f"ITR mappings not found: {effective_mappings}")
        # mappings can be file or dir
        try:
            if effective_mappings.is_dir():
                try:
                    if not any(effective_mappings.iterdir()):
                        raise FileNotFoundError(f"ITR mappings dir is empty: {effective_mappings}")
                except OSError as exc:
                    raise RuntimeError(
                        f"cannot read mappings dir {effective_mappings}: {exc}"
                    ) from exc
                try:
                    mappings_hash = canonical_hash_dir(effective_mappings)
                except OSError as exc:
                    raise RuntimeError(
                        f"cannot hash mappings dir {effective_mappings}: {exc}"
                    ) from exc
                if mappings_hash == _EMPTY_DIR_HASH:
                    raise FileNotFoundError(
                        f"ITR mappings dir is empty (hash is empty): {effective_mappings}"
                    )
            elif effective_mappings.is_file():
                try:
                    st = effective_mappings.stat()
                    if not stat.S_ISREG(st.st_mode):
                        raise ValueError(
                            f"mappings_path is not a regular file: {effective_mappings}"
                        )
                    if st.st_size == 0:
                        raise FileNotFoundError(
                            f"ITR mappings file is empty (0 bytes): {effective_mappings}"
                        )
                except OSError as exc:
                    if isinstance(exc, (FileNotFoundError, ValueError)):
                        raise
                    raise RuntimeError(f"cannot stat mappings {effective_mappings}: {exc}") from exc
                try:
                    mappings_hash = hash_file(effective_mappings)
                except (PermissionError, OSError) as exc:
                    raise RuntimeError(f"cannot hash mappings {effective_mappings}: {exc}") from exc
            else:
                raise ValueError(
                    f"mappings_path is not a regular file or directory: {effective_mappings}"
                )
        except OSError as exc:
            if isinstance(
                exc, (FileNotFoundError, NotADirectoryError, IsADirectoryError, ValueError)
            ):
                raise
            raise RuntimeError(f"cannot validate mappings {effective_mappings}: {exc}") from exc

        # 4. Compute entry hash and validate output_dir
        ih = icons_entry_hash(palette_hash, templates_hash, mappings_hash)
        _validate_output_dir(output_dir, ih)

        # 5. Ensure parent exists; create output_dir
        try:
            output_dir.parent.mkdir(parents=True, exist_ok=True)
        except FileExistsError as exc:
            raise RuntimeError(f"output_dir parent exists as file: {output_dir.parent}") from exc
        except OSError as exc:
            if exc.errno in (errno.ENOTEMPTY, errno.EEXIST, errno.EISDIR, errno.ENOTDIR):
                raise RuntimeError(
                    f"output_dir parent collision: {output_dir.parent}: {exc}"
                ) from exc
            raise RuntimeError(
                f"cannot create output_dir parent {output_dir.parent}: {exc}"
            ) from exc
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except FileExistsError as exc:
            raise RuntimeError(f"output_dir exists as file, not dir: {output_dir}") from exc
        except OSError as exc:
            if exc.errno in (errno.ENOTEMPTY, errno.EEXIST, errno.EISDIR, errno.ENOTDIR):
                raise RuntimeError(f"output_dir collision: {output_dir}: {exc}") from exc
            raise RuntimeError(f"cannot create output_dir {output_dir}: {exc}") from exc

        # 6. Resolve colors.yaml path for palette chaining
        colors_yaml_path: Path
        if self._palette_cache_dir is not None:
            base = self._palette_cache_dir
            if "\x00" in str(base):
                raise ValueError(f"palette_cache_dir contains null byte: {base!r}")
            try:
                if base.is_symlink():
                    raise ValueError(f"palette_cache_dir must not be a symlink: {base}")
            except OSError as exc:
                raise RuntimeError(f"cannot validate palette_cache_dir symlink: {exc}") from exc
            # If base name equals palette_hash, it's the entry dir itself
            if base.name == palette_hash:
                colors_yaml_path = base / "colors.yaml"
            elif (base / palette_hash).exists():
                # base is palettes base dir
                colors_yaml_path = base / palette_hash / "colors.yaml"
            else:
                # Assume base is state/cache/palettes base, construct
                colors_yaml_path = base / palette_hash / "colors.yaml"
                # Also try base directly if it ends with palettes
                if base.name == "palettes":
                    colors_yaml_path = base / palette_hash / "colors.yaml"
                else:
                    # Fallback: base / colors.yaml (when base is already entry dir)
                    alt = base / "colors.yaml"
                    if alt.exists():
                        colors_yaml_path = alt
                    else:
                        colors_yaml_path = base / palette_hash / "colors.yaml"
            # If base itself contains colors.yaml (entry dir without hash
            # in name), prefer it
            if (
                base.is_dir()
                and (base / "colors.yaml").is_file()
                and base.name != "palettes"
            ):
                # Heuristic: if base directly has colors.yaml, treat as entry dir
                # Overrides previous logic when base is entry dir but name != hash
                if (base / "colors.yaml").exists():
                    # If palette_hash matches entry hash, keep base/colors.yaml
                    # Already handled via name check, but handle tmp entry case
                    if base.name != palette_hash and not (
                        base / palette_hash
                    ).exists():
                        colors_yaml_path = base / "colors.yaml"
        else:
            # Derive from output_dir: .../cache/icons/<ih> -> .../cache/palettes/<ph>/colors.yaml
            try:
                colors_yaml_path = (
                    output_dir.parent.parent / "palettes" / palette_hash / "colors.yaml"
                )
            except Exception as exc:
                raise RuntimeError(
                    f"cannot derive palette cache path from output_dir: {exc}"
                ) from exc

        if "\x00" in str(colors_yaml_path):
            raise ValueError(f"colors_yaml_path contains null byte: {colors_yaml_path!r}")
        try:
            if colors_yaml_path.is_symlink():
                raise ValueError(f"colors_yaml_path must not be a symlink: {colors_yaml_path}")
        except OSError as exc:
            raise RuntimeError(f"cannot validate colors_yaml symlink: {exc}") from exc
        if not colors_yaml_path.exists():
            raise FileNotFoundError(f"palette colors.yaml not found: {colors_yaml_path}")
        if not colors_yaml_path.is_file():
            raise IsADirectoryError(f"palette colors.yaml is not a file: {colors_yaml_path}")
        try:
            st = colors_yaml_path.stat()
            if not stat.S_ISREG(st.st_mode):
                raise ValueError(f"colors_yaml_path is not a regular file: {colors_yaml_path}")
            if st.st_size == 0:
                raise FileNotFoundError(
                    f"palette colors.yaml is empty (0 bytes): {colors_yaml_path}"
                )
        except OSError as exc:
            if isinstance(exc, (FileNotFoundError, IsADirectoryError, ValueError)):
                raise
            raise RuntimeError(
                f"cannot stat palette colors.yaml {colors_yaml_path}: {exc}"
            ) from exc

        # 7. Build env with literal double-underscore keys (AD-7)
        env = build_env(
            {
                "ICON_RENDERER__OUTPUT__OUTPUT_DIR": str(output_dir),
                "ICON_RENDERER__COLOR_SCHEME__PATH": str(colors_yaml_path),
            }
        )

        # 8. Build args — intentionally NO -o/--output flag, NO --color-scheme flag
        bin_name = self._itr_bin or "itr"
        args = [bin_name, "render"]
        # If mappings is a file (icons.yaml), pass as positional yaml arg for real itr
        # Dir case uses discovery via env/templates; unit tests mock ignores extra args
        try:
            if effective_mappings.is_file():
                args.append(str(effective_mappings))
        except OSError:
            pass

        # 9. Run subprocess with timeout
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                env=env,
                timeout=self._timeout,
                errors="replace",
            )
        except FileNotFoundError as exc:
            raise FileNotFoundError("itr not on PATH") from exc
        except PermissionError as exc:
            raise FileNotFoundError("itr not on PATH (permission denied)") from exc
        except subprocess.TimeoutExpired as exc:
            raw_err = exc.stderr
            if isinstance(raw_err, bytes):
                try:
                    stderr_part = raw_err.decode(errors="replace")[:2048]
                except Exception:
                    stderr_part = str(raw_err)[:2048]
            else:
                stderr_part = str(raw_err or "")[:2048]
            raw_out = getattr(exc, "stdout", None)
            if isinstance(raw_out, bytes):
                try:
                    stdout_part = raw_out.decode(errors="replace")[:500]
                except Exception:
                    stdout_part = str(raw_out)[:500]
            else:
                stdout_part = str(raw_out or "")[:500]
            msg = f"itr render timed out after {self._timeout}s"
            if stderr_part:
                msg += f": {stderr_part}"
            if stdout_part:
                msg += f" stdout:{stdout_part}"
            raise TimeoutError(msg) from exc
        except (ValueError, UnicodeDecodeError) as exc:
            raise RuntimeError(f"itr render failed to spawn (bad arg/encoding): {exc}") from exc
        except OSError as exc:
            if exc.errno == errno.E2BIG:
                raise RuntimeError(f"itr render env too large (E2BIG): {exc}") from exc
            if exc.errno == errno.ENOENT:
                raise FileNotFoundError("itr not on PATH") from exc
            raise RuntimeError(f"itr render failed to spawn: {exc}") from exc

        if result.returncode != 0:
            if result.returncode < 0:
                sig = -result.returncode
                try:
                    import signal as _signal

                    sig_name = _signal.Signals(sig).name
                except Exception:
                    sig_name = str(sig)
                stderr = (result.stderr or "")[:2048]
                stdout_part = (result.stdout or "")[:500]
                raise RuntimeError(
                    f"itr killed by signal {sig_name} ({sig}): {stderr} stdout:{stdout_part}"
                )
            stderr = (result.stderr or "")[:2048]
            stdout = (result.stdout or "")[:500]
            raise RuntimeError(
                f"itr render failed (exit {result.returncode}): {stderr} stdout:{stdout}"
            )

        # 10. Verify SVG artifacts exist (recursive, case-insensitive)
        try:
            all_files = [p for p in output_dir.rglob("*") if p.is_file()]
        except OSError as exc:
            raise RuntimeError(f"cannot list itr output_dir {output_dir}: {exc}") from exc
        svg_files = [p for p in all_files if p.suffix.lower() == ".svg"]
        if not svg_files:
            raise RuntimeError(
                f"itr did not write expected SVG artifacts: {output_dir} "
                f"(found {len(all_files)} files, 0 svg)"
            )

        # 11. Compute artifact hashes
        basename_counts: dict[str, int] = {}
        for p in svg_files:
            basename_counts[p.name] = basename_counts.get(p.name, 0) + 1

        artifact_hashes: dict[str, str] = {}
        try:
            for p in svg_files:
                h = hash_file(p)
                if basename_counts[p.name] == 1:
                    key = p.name
                else:
                    try:
                        key = p.relative_to(output_dir).as_posix()
                    except ValueError as exc:
                        raise RuntimeError(
                            f"artifact outside output_dir (symlink escape): {p} not in {output_dir}"
                        ) from exc
                if key in artifact_hashes:
                    key = f"{key}:{h[:8]}"
                artifact_hashes[key] = h
        except (FileNotFoundError, PermissionError, IsADirectoryError, OSError) as exc:
            raise RuntimeError(f"cannot hash itr artifacts in {output_dir}: {exc}") from exc
        except ValueError as exc:
            if "outside output_dir" in str(exc):
                raise
            raise RuntimeError(f"cannot hash itr artifacts in {output_dir}: {exc}") from exc

        for k, v in artifact_hashes.items():
            if len(v) != 64 or not all(c in "0123456789abcdef" for c in v.lower()):
                raise RuntimeError(f"artifact hash for {k} is not 64-char hex: {v!r}")

        generated_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")

        return IconsEntry(
            hash_algorithm=HASH_ALGORITHM,
            kind="icons",
            entry_hash=ih,
            source_palette_hash=palette_hash,
            input_templates_hash=templates_hash,
            input_mappings_hash=mappings_hash,
            artifact_hashes=cast(IconsArtifacts, artifact_hashes),
            generated_at=generated_at,
        )
