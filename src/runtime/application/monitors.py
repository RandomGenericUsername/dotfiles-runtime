"""Shared monitor-config preservation (AD-18) for wallpaper use cases.

Both ``ApplyWallpaperUseCase`` (full derive + persist) and
``SwapVisibleUseCase`` (visible-first wallpaper-only persist) preserve
existing per-monitor backend/fit/mpv settings and only refresh
``source_hash`` — one implementation, two callers (the same sharing
shape as ``DerivationPipeline`` in ``derive.py``).

Pure function over injected data (no I/O, no adapters): callers pass
the already-detected monitor names from their injected
``IMonitorSource``.
"""

from __future__ import annotations

from collections.abc import Iterable

from runtime.domain.models import (
    DEFAULT_MONITOR,
    BackendType,
    DesktopState,
    FitMode,
    MonitorWallpaperConfig,
)
from runtime.ports.monitor_catalog import MonitorInfo


def preserve_monitors(
    existing: DesktopState | None,
    wallpaper_hash: str,
    detected: list[str],
) -> dict[str, MonitorWallpaperConfig]:
    """Preserve existing monitor configs, reconciled against live outputs.

    When ``detected`` (live ``IMonitorSource`` output) is non-empty, that
    set is AUTHORITATIVE for monitor names: an entry whose name still
    exists keeps its per-monitor backend/fit/mpv settings (AD-18: never
    silently reset a user's config) and only ``source_hash`` updates; a
    detected name with no stored entry (new or RENAMED output) gets a
    default ``hyprpaper``/``cover`` entry; a stored name no longer
    detected (stale) is dropped.

    When detection is unavailable or empty (headless/CI), the stored set
    is preserved as-is (no detection to trust), and when ``existing`` is
    None or has no monitors the legacy ``DEFAULT_MONITOR`` default
    applies.
    """
    if existing is not None and existing.monitors:
        if not detected:
            return {
                name: MonitorWallpaperConfig(
                    backend=cfg.backend,
                    source_hash=wallpaper_hash,
                    fit_mode=cfg.fit_mode,
                    mpv_options=cfg.mpv_options,
                    ipc_socket=cfg.ipc_socket,
                )
                for name, cfg in existing.monitors.items()
            }
        rebuilt: dict[str, MonitorWallpaperConfig] = {}
        for name in detected:
            cfg = existing.monitors.get(name)
            if cfg is not None:
                rebuilt[name] = MonitorWallpaperConfig(
                    backend=cfg.backend,
                    source_hash=wallpaper_hash,
                    fit_mode=cfg.fit_mode,
                    mpv_options=cfg.mpv_options,
                    ipc_socket=cfg.ipc_socket,
                )
            else:
                rebuilt[name] = MonitorWallpaperConfig(
                    backend=BackendType.hyprpaper,
                    source_hash=wallpaper_hash,
                    fit_mode=FitMode.cover,
                    mpv_options=None,
                    ipc_socket=None,
                )
        return rebuilt
    names = detected or [DEFAULT_MONITOR]
    return {
        name: MonitorWallpaperConfig(
            backend=BackendType.hyprpaper,
            source_hash=wallpaper_hash,
            fit_mode=FitMode.cover,
            mpv_options=None,
            ipc_socket=None,
        )
        for name in names
    }


def active_monitors(monitors: Iterable[MonitorInfo]) -> list[MonitorInfo]:
    """Filter disabled/mirrored outputs and sort by x, y, connector (REQ-009).

    This is the pure catalog projection: the adapter reports every output
    (with diagnostics); this function owns the contract's targetable set and
    canonical order (capture EPIC-002 ``design.md`` §2.2).
    """
    active = [monitor for monitor in monitors if not monitor.disabled and monitor.mirror_of is None]
    active.sort(key=lambda monitor: (monitor.geometry.x, monitor.geometry.y, monitor.connector))
    return active


def monitor_to_dict(monitor: MonitorInfo) -> dict[str, object]:
    """Serialize one descriptor to the ``monitors[]`` contract shape."""
    return {
        "connector": monitor.connector,
        "label": monitor.label,
        "description": monitor.description,
        "make": monitor.make,
        "model": monitor.model,
        "serial": monitor.serial,
        "geometry": {
            "x": monitor.geometry.x,
            "y": monitor.geometry.y,
            "width": monitor.geometry.width,
            "height": monitor.geometry.height,
        },
        "scale": monitor.scale,
        "transform": monitor.transform,
        "focused": monitor.focused,
        "mirror_of": monitor.mirror_of,
    }


def catalog_payload(monitors: Iterable[MonitorInfo]) -> dict[str, object]:
    """Build the ``schema_version: 1`` catalog object for ``monitors list``.

    An empty targetable set is reported as ``source: "unavailable"``: that
    is the uniform headless/no-output degradation shape the consumer
    contract expects (it never distinguishes "no compositor" from "no
    targetable output"). Detection never raises here.
    """
    active = active_monitors(monitors)
    return {
        "schema_version": 1,
        "source": "hyprland" if active else "unavailable",
        "monitors": [monitor_to_dict(monitor) for monitor in active],
    }
