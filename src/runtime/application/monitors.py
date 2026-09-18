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

from runtime.domain.models import (
    DEFAULT_MONITOR,
    BackendType,
    DesktopState,
    FitMode,
    MonitorWallpaperConfig,
)


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
