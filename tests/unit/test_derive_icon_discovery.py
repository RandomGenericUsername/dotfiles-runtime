"""Unit tests for find_icon_templates / find_icon_mappings discovery order (rt-3.5).

The primary provisioning path is `<install>/icon-templates/` and
`<install>/icon-mappings/icons.yaml` (per `shared-data-contract.md`
line 118, Story 2-6 AC 3/4, and the docs architecture). The legacy
`config/icon-templates-renderer/...` path is preserved as a fallback
for ad-hoc dev checkouts that bypass the assets role.
"""

from __future__ import annotations

from pathlib import Path

from runtime.application.derive import find_icon_mappings, find_icon_templates


class TestFindIconTemplates:
    """Provisioning path (primary) is found when present."""

    def test_provisioning_path_returned_when_present(self, tmp_path: Path) -> None:
        templates = tmp_path / "icon-templates"
        templates.mkdir()
        (templates / "battery.svg").write_text("<svg/>")
        assert find_icon_templates(tmp_path) == templates

    def test_legacy_config_path_returned_as_fallback(self, tmp_path: Path) -> None:
        legacy = tmp_path / "config" / "icon-templates-renderer" / "templates"
        legacy.mkdir(parents=True)
        (legacy / "battery.svg").write_text("<svg/>")
        assert find_icon_templates(tmp_path) == legacy

    def test_provisioning_path_wins_over_legacy(self, tmp_path: Path) -> None:
        provisioning = tmp_path / "icon-templates"
        provisioning.mkdir()
        (provisioning / "battery.svg").write_text("<svg/>")
        legacy = tmp_path / "config" / "icon-templates-renderer" / "templates"
        legacy.mkdir(parents=True)
        (legacy / "battery.svg").write_text("<svg/>")
        assert find_icon_templates(tmp_path) == provisioning

    def test_none_when_no_path_present(self, tmp_path: Path) -> None:
        assert find_icon_templates(tmp_path) is None


class TestFindIconMappings:
    """Provisioning file (`<install>/icon-mappings/icons.yaml`) is the primary."""

    def test_provisioning_yaml_returned_when_present(self, tmp_path: Path) -> None:
        mappings = tmp_path / "icon-mappings"
        mappings.mkdir()
        (mappings / "icons.yaml").write_text("icons: {}\n")
        assert find_icon_mappings(tmp_path) == mappings / "icons.yaml"

    def test_legacy_config_yaml_returned_as_fallback(self, tmp_path: Path) -> None:
        legacy = tmp_path / "config" / "icon-templates-renderer" / "icons.yaml"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("icons: {}\n")
        assert find_icon_mappings(tmp_path) == legacy

    def test_provisioning_yaml_wins_over_legacy(self, tmp_path: Path) -> None:
        mappings = tmp_path / "icon-mappings"
        mappings.mkdir()
        (mappings / "icons.yaml").write_text("icons: {}\n")
        legacy = tmp_path / "config" / "icon-templates-renderer" / "icons.yaml"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("icons: {}\n")
        assert find_icon_mappings(tmp_path) == mappings / "icons.yaml"

    def test_provisioning_dir_returned_when_no_yaml(self, tmp_path: Path) -> None:
        mappings = tmp_path / "icon-mappings"
        mappings.mkdir()
        assert find_icon_mappings(tmp_path) == mappings

    def test_none_when_no_path_present(self, tmp_path: Path) -> None:
        assert find_icon_mappings(tmp_path) is None
