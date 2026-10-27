"""Discovery of deployed standalone AGS apps from the installed spine.

AD-26 makes the deployed config link the single discovery source: the runtime
enumerates every deployed link under ``<install>/config`` (including
``config/ags``), reads the app's ``app.manifest.json`` and validates identity
against ``app.tsx`` and the live AGS name oracle. There is no hardcoded app
list and no composition-checkout read.

Failure policy: a deployed link whose manifest is missing/invalid, whose
identity disagrees, or whose name is owned by a foreign/dev process is a
structured failure. It is never a silent skip and never a restart fallback.
Live apps that are down are skipped for this operation; restart apps stay
configured and are included even when their process is down (AD-30).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Sequence
from pathlib import Path

import fastjsonschema

from runtime.adapters.ags_reloader import AgsAppProcess, _instance_name_for_dir
from runtime.adapters.contract_schemas import ags_app_manifest_validator
from runtime.domain.ags_apps import (
    AppDiscoveryFailure,
    AppDiscoveryResult,
    DeployedAppIdentity,
    DiscoveredApp,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AppDiscovery",
    "discover_deployed_apps",
    "manifest_identity",
]

_PATH_SEGMENT_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")


def _is_path_segment(value: object) -> bool:
    return isinstance(value, str) and _PATH_SEGMENT_RE.fullmatch(value) is not None


def _link_segment(path: Path) -> str:
    """The deployed link basename when valid, else a safe label for failures."""
    name = path.name
    return name if _is_path_segment(name) else "invalid-config-link"


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


def _is_candidate(entry: Path) -> bool:
    """A deployed app link, or a real directory that carries app identity.

    A bare ``config/ags`` directory containing only ``colors.css`` is a
    consumer pointer directory, not a deployed app; real deployments are
    symlinks into an app install (or carry ``app.manifest.json``/``app.tsx``).
    """
    if entry.name.startswith("."):
        return False
    try:
        if entry.is_symlink():
            return True
        return (entry / "app.manifest.json").is_file() or (entry / "app.tsx").is_file()
    except OSError:
        return False


class _ManifestError(Exception):
    """Internal validation failure with the fields we could recover."""

    def __init__(
        self, reason: str, app: str = "", instance: str = "", config_dir: str = ""
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.app = app
        self.instance = instance
        self.config_dir = config_dir


def _read_manifest(link: Path) -> dict[str, object]:
    path = link / "app.manifest.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise _ManifestError("missing app.manifest.json") from exc
    except OSError as exc:
        raise _ManifestError(f"cannot read app.manifest.json: {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise _ManifestError(f"app.manifest.json is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise _ManifestError("app.manifest.json is not a JSON object")
    try:
        ags_app_manifest_validator()(data)
    except fastjsonschema.JsonSchemaValueException as exc:
        app = data.get("app") if _is_path_segment(data.get("app")) else ""
        instance = data.get("instance") if _is_path_segment(data.get("instance")) else ""
        config_dir = data.get("configDir") if _is_path_segment(data.get("configDir")) else ""
        raise _ManifestError(
            f"invalid app.manifest.json: {exc.message}", app or "", instance or "", config_dir or ""
        ) from exc
    return data


class AppDiscovery:
    """Validate deployed config links against their manifest and live owner."""

    def __init__(
        self,
        install_spine: Path,
        *,
        process_lister: Callable[[], Sequence[AgsAppProcess]] | None = None,
    ) -> None:
        self._config_root = install_spine / "config"
        self._process_lister = process_lister

    def discover(self, live_names: Sequence[str]) -> AppDiscoveryResult:
        """Return validated apps and structured failures for one discovery pass."""
        try:
            entries = sorted(
                (entry for entry in self._config_root.iterdir() if _is_candidate(entry)),
                key=lambda entry: entry.name,
            )
        except FileNotFoundError:
            return AppDiscoveryResult()
        except OSError as exc:
            logger.warning("ags discovery: cannot read %s: %s", self._config_root, exc)
            return AppDiscoveryResult(
                failures=(
                    AppDiscoveryFailure(
                        link=str(self._config_root),
                        reason=f"cannot read installed config root: {exc}",
                        discovery_index=0,
                    ),
                )
            )

        processes = self._processes()
        names = set(live_names)
        valid: list[tuple[DeployedAppIdentity, list[AgsAppProcess]]] = []
        failures: list[AppDiscoveryFailure] = []

        # Two links pointing at the same deployed app are a duplicate
        # deployment, not two apps (AD-26).
        target_groups: dict[Path, list[Path]] = {}
        for entry in entries:
            if entry.is_symlink():
                target_groups.setdefault(_resolved(entry), []).append(entry)
        duplicate_targets = {
            target for target, links in target_groups.items() if len(links) > 1
        }

        for index, link in enumerate(entries):
            segment = _link_segment(link)
            if link.is_symlink() and _resolved(link) in duplicate_targets:
                failures.append(
                    AppDiscoveryFailure(
                        link=str(link),
                        reason=f"duplicate deployed link target: {_resolved(link)}",
                        discovery_index=index,
                        app=segment,
                        instance=segment,
                        config_dir=segment,
                    )
                )
                continue
            try:
                identity = self._validate_link(link, segment, index)
            except _ManifestError as exc:
                failures.append(
                    AppDiscoveryFailure(
                        link=str(link),
                        reason=exc.reason,
                        discovery_index=index,
                        app=exc.app or segment,
                        instance=exc.instance or segment,
                        config_dir=exc.config_dir or segment,
                    )
                )
                continue
            owners = [
                process for process in processes if _resolved(process.config_dir) == _resolved(link)
            ]
            valid.append((identity, owners))

        # Cross-entry uniqueness is contract data (apps.yaml x-uniqueKeys):
        # duplicate app/instance/configDir values are all structured failures.
        for field_name in ("app", "instance", "config_dir"):
            groups: dict[str, list[int]] = {}
            for position, (identity, _owners) in enumerate(valid):
                groups.setdefault(getattr(identity, field_name), []).append(position)
            duplicated = {
                value: positions for value, positions in groups.items() if len(positions) > 1
            }
            if not duplicated:
                continue
            drop: set[int] = set()
            for value, positions in duplicated.items():
                for position in positions:
                    identity, _owners = valid[position]
                    failures.append(
                        AppDiscoveryFailure(
                            link=identity.link_path,
                            reason=f"duplicate {field_name.replace('_', 'Dir')}: {value!r}",
                            discovery_index=identity.discovery_index,
                            app=identity.app,
                            instance=identity.instance,
                            config_dir=identity.config_dir,
                        )
                    )
                    drop.add(position)
            valid = [item for position, item in enumerate(valid) if position not in drop]

        apps: list[DiscoveredApp] = []
        for identity, owners in valid:
            owned = identity.instance in names
            if owned and not owners:
                foreign = [
                    process for process in processes if process.instance == identity.instance
                ]
                if foreign:
                    failures.append(
                        AppDiscoveryFailure(
                            link=identity.link_path,
                            reason=(
                                f"AGS name {identity.instance!r} is owned by a foreign/dev "
                                f"process ({foreign[0].config_dir})"
                            ),
                            discovery_index=identity.discovery_index,
                            app=identity.app,
                            instance=identity.instance,
                            config_dir=identity.config_dir,
                        )
                    )
                else:
                    failures.append(
                        AppDiscoveryFailure(
                            link=identity.link_path,
                            reason=(
                                f"AGS name {identity.instance!r} is owned by a process outside "
                                f"the deployed link {identity.config_dir!r}"
                            ),
                            discovery_index=identity.discovery_index,
                            app=identity.app,
                            instance=identity.instance,
                            config_dir=identity.config_dir,
                        )
                    )
                continue
            if owned and owners:
                foreign = [
                    process
                    for process in processes
                    if process.instance == identity.instance
                    and _resolved(process.config_dir) != _resolved(Path(identity.link_path))
                ]
                if foreign:
                    failures.append(
                        AppDiscoveryFailure(
                            link=identity.link_path,
                            reason=(
                                f"AGS name {identity.instance!r} is owned by a foreign/dev "
                                f"process ({foreign[0].config_dir})"
                            ),
                            discovery_index=identity.discovery_index,
                            app=identity.app,
                            instance=identity.instance,
                            config_dir=identity.config_dir,
                        )
                    )
                    continue
            apps.append(
                DiscoveredApp(
                    identity=identity,
                    running=owned and bool(owners),
                    pids=tuple(process.pid for process in owners),
                )
            )

        apps.sort(key=lambda app: app.identity.discovery_index)
        failures.sort(key=lambda failure: failure.discovery_index)
        return AppDiscoveryResult(apps=tuple(apps), failures=tuple(failures))

    def _processes(self) -> list[AgsAppProcess]:
        if self._process_lister is None:
            from runtime.adapters.ags_reloader import _discover_ags_apps

            try:
                return list(_discover_ags_apps())
            except OSError:
                return []
        try:
            return list(self._process_lister())
        except Exception as exc:  # adapter boundary: discovery failure is structured
            logger.warning("ags discovery: process lister failed: %s", exc)
            return []

    def _validate_link(self, link: Path, segment: str, index: int) -> DeployedAppIdentity:
        try:
            if link.is_symlink() and not link.exists():
                raise _ManifestError("deployed config link is dangling")
            manifest = _read_manifest(link)
        except _ManifestError:
            raise
        except OSError as exc:
            raise _ManifestError(f"cannot inspect deployed link: {exc}") from exc

        app = manifest.get("app")
        instance = manifest.get("instance")
        config_dir = manifest.get("configDir")
        mode = manifest.get("mode")
        holds_unsaved = manifest.get("holdsUnsavedState")
        for field_name, value in (
            ("app", app),
            ("instance", instance),
            ("configDir", config_dir),
        ):
            if not _is_path_segment(value):
                raise _ManifestError(
                    f"{field_name} must be exactly one lowercase path segment",
                    app=app if _is_path_segment(app) else "",
                    instance=instance if _is_path_segment(instance) else "",
                    config_dir=config_dir if _is_path_segment(config_dir) else "",
                )
        assert isinstance(app, str) and isinstance(instance, str) and isinstance(config_dir, str)
        if mode not in ("live", "restart"):
            raise _ManifestError(f"invalid mode {mode!r}", app, instance, config_dir)
        if not isinstance(holds_unsaved, bool):
            raise _ManifestError("holdsUnsavedState must be a boolean", app, instance, config_dir)
        if mode == "restart" and holds_unsaved:
            raise _ManifestError(
                "mode 'restart' is invalid when holdsUnsavedState is true",
                app,
                instance,
                config_dir,
            )
        if config_dir != segment:
            raise _ManifestError(
                f"configDir {config_dir!r} does not match deployed link {segment!r}",
                app,
                instance,
                config_dir,
            )

        app_tsx_instance = _instance_name_for_dir(link)
        if app_tsx_instance is None:
            raise _ManifestError(
                "app.tsx is missing or has no instanceName",
                app,
                instance,
                config_dir,
            )
        if app_tsx_instance != instance:
            raise _ManifestError(
                f"instance {instance!r} does not match app.tsx instanceName {app_tsx_instance!r}",
                app,
                instance,
                config_dir,
            )

        return DeployedAppIdentity(
            app=app,
            instance=instance,
            config_dir=config_dir,
            mode=mode,  # type: ignore[arg-type]
            holds_unsaved_state=holds_unsaved,
            link_path=str(link),
            discovery_index=index,
        )


def manifest_identity(manifest: dict[str, object]) -> tuple[str, str, str]:
    """Return ``(app, instance, configDir)`` when all are one path segment."""
    values = (manifest.get("app"), manifest.get("instance"), manifest.get("configDir"))
    if not all(_is_path_segment(value) for value in values):
        raise ValueError("manifest identity is not three lowercase path segments")
    return values  # type: ignore[return-value]


def discover_deployed_apps(
    install_spine: Path,
    live_names: Sequence[str],
    *,
    process_lister: Callable[[], Sequence[AgsAppProcess]] | None = None,
) -> AppDiscoveryResult:
    """Convenience wrapper around :class:`AppDiscovery`."""
    return AppDiscovery(install_spine, process_lister=process_lister).discover(live_names)
