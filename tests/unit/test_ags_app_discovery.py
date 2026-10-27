"""Discovery/identity/failure tests for deployed standalone AGS apps (AD-26).

All fixtures are fake directories/symlinks and injected process/name oracles:
no GUI, no live AGS, no host process is touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from runtime.adapters.ags_app_discovery import AppDiscovery
from runtime.adapters.ags_reloader import AgsAppProcess


def _manifest(
    *,
    app: str,
    instance: str,
    config_dir: str,
    mode: str = "live",
    holds_unsaved: bool = False,
) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "app": app,
        "instance": instance,
        "configDir": config_dir,
        "mode": mode,
        "holdsUnsavedState": holds_unsaved,
    }


def _deploy(
    spine: Path,
    link_name: str,
    manifest: dict[str, object] | None,
    *,
    app_tsx_instance: str | None = None,
    target_name: str | None = None,
) -> Path:
    app_dir = spine / "apps" / (target_name or link_name) / "v0.1.0"
    app_dir.mkdir(parents=True)
    if manifest is not None:
        (app_dir / "app.manifest.json").write_text(json.dumps(manifest))
    instance = app_tsx_instance
    if instance is None:
        instance = str(manifest.get("instance")) if manifest is not None else "missing"
    (app_dir / "app.tsx").write_text(f'app.start({{ instanceName: "{instance}" }})\n')
    link = spine / "config" / link_name
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(app_dir, target_is_directory=True)
    return link


def _process(pid: int, link: Path, instance: str) -> AgsAppProcess:
    return AgsAppProcess(
        pid=pid,
        argv=("ags", "run", "-d", str(link)),
        config_dir=link,
        instance=instance,
    )


def _discover(
    spine: Path,
    live: tuple[str, ...] = (),
    processes: tuple[AgsAppProcess, ...] = (),
):
    return AppDiscovery(spine, process_lister=lambda: processes).discover(live)


class TestDiscoverySuccess:
    def test_discovers_deployed_link_and_identity(self, tmp_path: Path) -> None:
        spine = tmp_path / "install"
        link = _deploy(
            spine,
            "ags-clipboard",
            _manifest(app="clipboard", instance="clipboard", config_dir="ags-clipboard"),
        )
        result = _discover(spine)
        assert result.failures == ()
        assert len(result.apps) == 1
        identity = result.apps[0].identity
        assert identity.app == "clipboard"
        assert identity.instance == "clipboard"
        assert identity.config_dir == "ags-clipboard"
        assert identity.link_path == str(link)
        assert result.apps[0].running is False

    def test_running_link_owner_is_recorded(self, tmp_path: Path) -> None:
        spine = tmp_path / "install"
        link = _deploy(
            spine,
            "ags-capture",
            _manifest(app="capture", instance="capture", config_dir="ags-capture"),
        )
        result = _discover(
            spine, live=("capture",), processes=(_process(42, link, "capture"),)
        )
        assert result.failures == ()
        assert result.apps[0].running is True
        assert result.apps[0].pids == (42,)

    def test_bar_is_discovered_from_config_ags(self, tmp_path: Path) -> None:
        spine = tmp_path / "install"
        _deploy(
            spine,
            "ags",
            _manifest(
                app="ags",
                instance="ags",
                config_dir="ags",
                mode="restart",
            ),
        )
        result = _discover(spine)
        assert [app.identity.instance for app in result.apps] == ["ags"]
        assert result.apps[0].identity.mode == "restart"

    def test_down_restart_app_is_included(self, tmp_path: Path) -> None:
        spine = tmp_path / "install"
        _deploy(
            spine,
            "ags",
            _manifest(app="ags", instance="ags", config_dir="ags", mode="restart"),
        )
        result = _discover(spine, live=())
        assert len(result.apps) == 1
        assert result.apps[0].running is False

    def test_down_live_app_is_included_but_not_running(self, tmp_path: Path) -> None:
        spine = tmp_path / "install"
        _deploy(
            spine,
            "ags-clipboard",
            _manifest(app="clipboard", instance="clipboard", config_dir="ags-clipboard"),
        )
        result = _discover(spine, live=())
        assert len(result.apps) == 1
        assert result.apps[0].running is False

    def test_dev_process_without_deployed_name_is_ignored(self, tmp_path: Path) -> None:
        spine = tmp_path / "install"
        _deploy(
            spine,
            "ags-clipboard",
            _manifest(app="clipboard", instance="clipboard", config_dir="ags-clipboard"),
        )
        dev = AgsAppProcess(
            pid=7,
            argv=("ags", "run", "-d", "/home/u/Dev/ags-clipboard"),
            config_dir=Path("/home/u/Dev/ags-clipboard"),
            instance="clipboard-dev",
        )
        result = _discover(spine, live=(), processes=(dev,))
        assert result.failures == ()
        assert [app.identity.instance for app in result.apps] == ["clipboard"]


class TestDiscoveryFailures:
    def test_missing_manifest_is_structured_failure(self, tmp_path: Path) -> None:
        spine = tmp_path / "install"
        _deploy(spine, "ags-clipboard", None)
        result = _discover(spine)
        assert result.apps == ()
        assert len(result.failures) == 1
        assert "missing app.manifest.json" in result.failures[0].reason
        assert result.failures[0].config_dir == "ags-clipboard"

    def test_invalid_manifest_is_structured_failure(self, tmp_path: Path) -> None:
        spine = tmp_path / "install"
        link = _deploy(
            spine,
            "ags-clipboard",
            _manifest(app="clipboard", instance="clipboard", config_dir="ags-clipboard"),
        )
        (link / "app.manifest.json").write_text("{not json")
        result = _discover(spine)
        assert result.apps == ()
        assert "not valid JSON" in result.failures[0].reason

    def test_unknown_schema_version_is_structured_failure(self, tmp_path: Path) -> None:
        spine = tmp_path / "install"
        manifest = _manifest(
            app="clipboard", instance="clipboard", config_dir="ags-clipboard"
        )
        manifest["schemaVersion"] = 2
        _deploy(spine, "ags-clipboard", manifest)
        result = _discover(spine)
        assert result.apps == ()
        assert "invalid app.manifest.json" in result.failures[0].reason

    def test_config_dir_must_equal_link_basename(self, tmp_path: Path) -> None:
        spine = tmp_path / "install"
        _deploy(
            spine,
            "ags-clipboard",
            _manifest(app="clipboard", instance="clipboard", config_dir="clipboard"),
        )
        result = _discover(spine)
        assert result.apps == ()
        assert "does not match deployed link" in result.failures[0].reason

    def test_instance_must_match_app_tsx(self, tmp_path: Path) -> None:
        spine = tmp_path / "install"
        _deploy(
            spine,
            "ags-clipboard",
            _manifest(app="clipboard", instance="clipboard", config_dir="ags-clipboard"),
            app_tsx_instance="other",
        )
        result = _discover(spine)
        assert result.apps == ()
        assert "app.tsx instanceName" in result.failures[0].reason

    def test_uppercase_identity_is_rejected(self, tmp_path: Path) -> None:
        spine = tmp_path / "install"
        _deploy(
            spine,
            "ags-clipboard",
            _manifest(app="Clipboard", instance="clipboard", config_dir="ags-clipboard"),
        )
        result = _discover(spine)
        assert result.apps == ()
        assert "invalid app.manifest.json" in result.failures[0].reason

    def test_duplicate_instance_marks_all_entries_failed(self, tmp_path: Path) -> None:
        spine = tmp_path / "install"
        _deploy(
            spine,
            "ags-clipboard",
            _manifest(app="clipboard", instance="clipboard", config_dir="ags-clipboard"),
        )
        _deploy(
            spine,
            "ags-clipboard-ui",
            _manifest(app="clipboard-ui", instance="clipboard", config_dir="ags-clipboard-ui"),
        )
        result = _discover(spine)
        assert result.apps == ()
        assert len(result.failures) == 2
        assert all("duplicate instance" in failure.reason for failure in result.failures)

    def test_duplicate_app_marks_all_entries_failed(self, tmp_path: Path) -> None:
        spine = tmp_path / "install"
        _deploy(
            spine,
            "ags-clipboard",
            _manifest(app="clipboard", instance="clipboard", config_dir="ags-clipboard"),
        )
        _deploy(
            spine,
            "ags-clipboard-2",
            _manifest(app="clipboard", instance="clipboard-2", config_dir="ags-clipboard-2"),
        )
        result = _discover(spine)
        assert result.apps == ()
        assert all("duplicate app" in failure.reason for failure in result.failures)

    def test_two_links_to_the_same_target_are_duplicates(self, tmp_path: Path) -> None:
        spine = tmp_path / "install"
        manifest = _manifest(
            app="clipboard", instance="clipboard", config_dir="ags-clipboard"
        )
        link = _deploy(spine, "ags-clipboard", manifest)
        alias = spine / "config" / "ags-clipboard-alias"
        alias.symlink_to(link.resolve(), target_is_directory=True)
        result = _discover(spine)
        assert result.apps == ()
        assert len(result.failures) == 2
        assert all("duplicate deployed link" in failure.reason for failure in result.failures)

    def test_foreign_owner_of_deployed_name_is_conflict(self, tmp_path: Path) -> None:
        spine = tmp_path / "install"
        _deploy(
            spine,
            "ags-clipboard",
            _manifest(app="clipboard", instance="clipboard", config_dir="ags-clipboard"),
        )
        foreign = AgsAppProcess(
            pid=9,
            argv=("ags", "run", "-d", "/home/u/Dev/ags-clipboard"),
            config_dir=Path("/home/u/Dev/ags-clipboard"),
            instance="clipboard",
        )
        result = _discover(spine, live=("clipboard",), processes=(foreign,))
        assert result.apps == ()
        assert "foreign/dev process" in result.failures[0].reason

    def test_name_owned_without_a_link_process_is_conflict(self, tmp_path: Path) -> None:
        spine = tmp_path / "install"
        _deploy(
            spine,
            "ags-clipboard",
            _manifest(app="clipboard", instance="clipboard", config_dir="ags-clipboard"),
        )
        result = _discover(spine, live=("clipboard",), processes=())
        assert result.apps == ()
        assert "owned by a process outside" in result.failures[0].reason

    def test_restart_mode_with_unsaved_state_is_rejected(self, tmp_path: Path) -> None:
        spine = tmp_path / "install"
        _deploy(
            spine,
            "ags-editor",
            _manifest(
                app="editor",
                instance="editor",
                config_dir="ags-editor",
                mode="restart",
                holds_unsaved=True,
            ),
        )
        result = _discover(spine)
        assert result.apps == ()
        assert "holdsUnsavedState" in result.failures[0].reason

    def test_dangling_link_is_structured_failure(self, tmp_path: Path) -> None:
        spine = tmp_path / "install"
        link = spine / "config" / "ags-missing"
        link.parent.mkdir(parents=True)
        link.symlink_to(spine / "does-not-exist")
        result = _discover(spine)
        assert result.apps == ()
        assert "dangling" in result.failures[0].reason

    def test_missing_config_root_is_no_apps_not_failure(self, tmp_path: Path) -> None:
        result = _discover(tmp_path / "unprovisioned")
        assert result.apps == ()
        assert result.failures == ()

    def test_non_app_consumer_directory_is_ignored(self, tmp_path: Path) -> None:
        spine = tmp_path / "install"
        consumer = spine / "config" / "ags"
        consumer.mkdir(parents=True)
        (consumer / "colors.css").write_text("colors {}")
        result = _discover(spine)
        assert result.apps == ()
        assert result.failures == ()


@pytest.mark.parametrize("field", ["app", "instance", "configDir"])
def test_identity_must_be_one_path_segment(tmp_path: Path, field: str) -> None:
    spine = tmp_path / "install"
    manifest = _manifest(app="clipboard", instance="clipboard", config_dir="ags-clipboard")
    manifest[field] = "bad/segment"
    _deploy(spine, "ags-clipboard", manifest)
    result = _discover(spine)
    assert result.apps == ()
    assert "invalid app.manifest.json" in result.failures[0].reason
