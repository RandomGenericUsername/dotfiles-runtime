"""One-time clipboard namespace migration from hypr-pano (AD-35)."""

from __future__ import annotations

import json
from pathlib import Path

from runtime.adapters.clipboard_config import default_config_path
from runtime.adapters.clipboard_migration import (
    LEGACY_NAMESPACE,
    NAMESPACE,
    migrate_clipboard_paths,
)
from runtime.adapters.json_history_store import default_history_path, default_image_dir


def _history(items: list[dict[str, object]]) -> dict[str, object]:
    return {"version": 1, "items": items}


class TestNamespacePaths:
    def test_default_paths_use_clipboard_namespace(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
        assert default_config_path() == tmp_path / "config" / NAMESPACE / "config.json"
        assert default_history_path() == tmp_path / "state" / NAMESPACE / "history.json"
        assert default_image_dir() == tmp_path / "cache" / NAMESPACE


class TestMigration:
    def _legacy_fixture(self, tmp_path: Path) -> tuple[Path, Path, Path]:
        config_home = tmp_path / "config"
        state_home = tmp_path / "state"
        cache_home = tmp_path / "cache"
        legacy_config = config_home / LEGACY_NAMESPACE / "config.json"
        legacy_config.parent.mkdir(parents=True)
        legacy_config.write_text(json.dumps({"limits": {"text": 7, "image": 3}}))
        image = cache_home / LEGACY_NAMESPACE / "image-1.png"
        image.parent.mkdir(parents=True)
        image.write_bytes(b"png")
        legacy_history = state_home / LEGACY_NAMESPACE / "history.json"
        legacy_history.parent.mkdir(parents=True)
        legacy_history.write_text(
            json.dumps(
                _history(
                    [
                        {
                            "hash": "h1",
                            "kind": "image",
                            "timestamp": 10,
                            "favorite": True,
                            "path": str(image),
                        }
                    ]
                )
            )
        )
        return config_home, state_home, cache_home

    def test_history_preferences_and_images_survive(self, tmp_path: Path) -> None:
        config_home, state_home, cache_home = self._legacy_fixture(tmp_path)
        result = migrate_clipboard_paths(
            config_home=config_home, state_home=state_home, cache_home=cache_home
        )
        assert result.errors == ()
        assert set(result.migrated) == {"config", "history", "cache"}
        new_config = config_home / NAMESPACE / "config.json"
        assert json.loads(new_config.read_text())["limits"]["text"] == 7
        new_history = state_home / NAMESPACE / "history.json"
        data = json.loads(new_history.read_text())
        assert data["items"][0]["favorite"] is True
        assert data["items"][0]["path"] == str(cache_home / NAMESPACE / "image-1.png")
        assert (cache_home / NAMESPACE / "image-1.png").read_bytes() == b"png"
        # Old paths are removed only after the new copies are proven.
        assert not (config_home / LEGACY_NAMESPACE / "config.json").exists()
        assert not (state_home / LEGACY_NAMESPACE / "history.json").exists()
        assert not (cache_home / LEGACY_NAMESPACE).exists()
        marker = state_home / NAMESPACE / ".migrated-from-hypr-pano.json"
        assert marker.is_file()

    def test_migration_is_one_time(self, tmp_path: Path) -> None:
        config_home, state_home, cache_home = self._legacy_fixture(tmp_path)
        migrate_clipboard_paths(
            config_home=config_home, state_home=state_home, cache_home=cache_home
        )
        # A later legacy file must not overwrite the new namespace.
        legacy = config_home / LEGACY_NAMESPACE / "config.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps({"limits": {"text": 99}}))
        result = migrate_clipboard_paths(
            config_home=config_home, state_home=state_home, cache_home=cache_home
        )
        assert result.skipped is True
        assert json.loads((config_home / NAMESPACE / "config.json").read_text())["limits"][
            "text"
        ] == 7
        assert legacy.exists()

    def test_existing_new_history_merges_favorites(self, tmp_path: Path) -> None:
        config_home, state_home, cache_home = self._legacy_fixture(tmp_path)
        new_history = state_home / NAMESPACE / "history.json"
        new_history.parent.mkdir(parents=True)
        new_history.write_text(
            json.dumps(
                _history(
                    [
                        {
                            "hash": "h1",
                            "kind": "image",
                            "timestamp": 5,
                            "favorite": False,
                            "path": str(cache_home / NAMESPACE / "image-1.png"),
                        },
                        {"hash": "h2", "kind": "text", "timestamp": 20, "text": "new"},
                    ]
                )
            )
        )
        result = migrate_clipboard_paths(
            config_home=config_home, state_home=state_home, cache_home=cache_home
        )
        assert result.errors == ()
        items = {
            item["hash"]: item
            for item in json.loads(new_history.read_text())["items"]
        }
        assert set(items) == {"h1", "h2"}
        assert items["h1"]["favorite"] is True
        assert items["h1"]["timestamp"] == 10

    def test_remove_legacy_can_be_deferred(self, tmp_path: Path) -> None:
        config_home, state_home, cache_home = self._legacy_fixture(tmp_path)
        result = migrate_clipboard_paths(
            config_home=config_home,
            state_home=state_home,
            cache_home=cache_home,
            remove_legacy=False,
        )
        assert result.errors == ()
        assert (config_home / LEGACY_NAMESPACE / "config.json").exists()
        assert (state_home / LEGACY_NAMESPACE / "history.json").exists()
        assert (cache_home / LEGACY_NAMESPACE / "image-1.png").exists()
        assert (config_home / NAMESPACE / "config.json").exists()

    def test_missing_legacy_paths_still_writes_marker(self, tmp_path: Path) -> None:
        result = migrate_clipboard_paths(
            config_home=tmp_path / "config",
            state_home=tmp_path / "state",
            cache_home=tmp_path / "cache",
        )
        assert result.errors == ()
        assert result.migrated == ()
        assert (tmp_path / "state" / NAMESPACE / ".migrated-from-hypr-pano.json").is_file()
