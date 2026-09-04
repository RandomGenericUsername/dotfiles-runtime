"""Unit tests for find_default_effects_catalog and drain_work_dir (rt-3.6).

WEG's own config-assembler discovers the effects catalog at
``$XDG_CONFIG_HOME/weg/effects.yaml`` (via the symlink
``~/.config/weg → <install>/config/weg`` created by the provisioning
``config_links`` role). The runtime's ``_find_default_effects_catalog``
mirrors that same XDG path so its pre-computed ``eeh`` matches WEG's.

``drain_work_dir`` in ``seeder.py`` moves WEG's output (which includes
a ``<wallpaper_stem>/`` subdir per WEG's ``OutputPathService.batch_output_dir``)
into the staging dir. WEG's nested subdirs previously caused
``work_dir.rmdir()`` to fail with ENOTEMPTY — the fix uses
``shutil.rmtree`` instead.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from runtime.adapters.seeder import CacheSeeder
from runtime.adapters.weg_adapter import _find_default_effects_catalog


# --- _find_default_effects_catalog (mirrors WEG's XDG strategy) ---


class TestFindDefaultEffectsCatalogXdgStrategy:
    """WEG's config-assembler XDG strategy: ``$XDG_CONFIG_HOME/weg/effects.yaml``.

    The provisioning ``config_links`` role creates the symlink
    ``~/.config/weg → <install>/config/weg`` so WEG's XDG strategy
    finds the project's catalog through the link. The runtime's
    discovery mirrors the same path.
    """

    def test_xdg_path_returned_when_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        xdg_config = tmp_path / "config"
        weg_dir = xdg_config / "weg"
        weg_dir.mkdir(parents=True)
        (weg_dir / "effects.yaml").write_text("effects: []\n")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
        assert _find_default_effects_catalog() == weg_dir / "effects.yaml"

    def test_xdg_symlink_resolved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Provisioning creates ``~/.config/weg → <install>/config/weg``.

        The runtime should find the catalog via the XDG path; the
        catalog content is the same regardless of symlink resolution.
        """
        install = tmp_path / "install"
        weg = install / "config" / "weg"
        weg.mkdir(parents=True)
        (weg / "effects.yaml").write_text("effects: []\n")
        xdg_config = tmp_path / "config"
        xdg_config.mkdir()
        (xdg_config / "weg").symlink_to(weg)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
        result = _find_default_effects_catalog()
        assert result is not None
        assert result.read_text() == "effects: []\n"


class TestFindDefaultEffectsCatalogEnvOverride:
    """WEG's EnvPathStrategy: ``WALLPAPER_EFFECTS_CONFIG_FILE_PATH`` env var."""

    def test_env_var_takes_priority(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_catalog = tmp_path / "env-catalog.yaml"
        env_catalog.write_text("effects: []\n")
        monkeypatch.setenv("WALLPAPER_EFFECTS_CONFIG_FILE_PATH", str(env_catalog))
        assert _find_default_effects_catalog() == env_catalog

    def test_env_var_nonexistent_falls_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "WALLPAPER_EFFECTS_CONFIG_FILE_PATH", str(tmp_path / "missing.yaml")
        )
        # Falls through to XDG / traversal / repo ancestor. None of these
        # are set up in tmp_path, so the result is whatever the repo
        # ancestor or HOME env produces — not the env var.
        result = _find_default_effects_catalog()
        assert result != tmp_path / "missing.yaml"


class TestFindDefaultEffectsCatalogDirectoryTraversal:
    """WEG's DirectoryTraversalStrategy: ``effects.yaml`` in CWD or up to 2 parents."""

    def test_cwd_effects_yaml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Use a unique XDG path that's guaranteed empty so the test
        # falls through to the directory traversal strategy.
        empty_xdg = tmp_path / "empty_xdg"
        empty_xdg.mkdir()
        monkeypatch.setenv("XDG_CONFIG_HOME", str(empty_xdg))
        monkeypatch.setenv("WALLPAPER_EFFECTS_CONFIG_FILE_PATH", "")
        cwd_catalog = tmp_path / "effects.yaml"
        cwd_catalog.write_text("effects: []\n")
        monkeypatch.chdir(tmp_path)
        assert _find_default_effects_catalog() == cwd_catalog

    def test_parent_effects_yaml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        empty_xdg = tmp_path / "empty_xdg"
        empty_xdg.mkdir()
        monkeypatch.setenv("XDG_CONFIG_HOME", str(empty_xdg))
        monkeypatch.setenv("WALLPAPER_EFFECTS_CONFIG_FILE_PATH", "")
        parent_catalog = tmp_path / "effects.yaml"
        parent_catalog.write_text("effects: []\n")
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        monkeypatch.chdir(subdir)
        assert _find_default_effects_catalog() == parent_catalog


# --- drain_work_dir (handles WEG's nested stem subdir) ---


class TestDrainWorkDir:
    """Move WEG's nested output (``<work>/<stem>/effect/*.png``) into staging.

    WEG's ``OutputPathService.batch_output_dir`` always creates a
    ``<wallpaper_stem>/`` subdir under the output dir. The drain must
    preserve that structure AND clean up the now-empty work-dir
    intermediate subdirs.
    """

    def test_flat_files_only(
        self, tmp_path: Path
    ) -> None:
        work = tmp_path / "work"
        work.mkdir()
        staging = tmp_path / "staging"
        staging.mkdir()
        (work / "a.png").write_bytes(b"a")
        (work / "b.png").write_bytes(b"b")
        CacheSeeder(tmp_path).drain_work_dir(work, staging)
        assert (staging / "a.png").read_bytes() == b"a"
        assert (staging / "b.png").read_bytes() == b"b"
        assert not work.exists()

    def test_nested_weg_structure_preserved(
        self, tmp_path: Path
    ) -> None:
        """WEG writes ``<work>/<stem>/effect/*.png``; drain must preserve the nest.

        This is the real WEG output structure (see
        ``WEG.services.OutputPathService.batch_output_dir``).
        """
        work = tmp_path / "work"
        staging = tmp_path / "staging"
        staging.mkdir()
        # Mirror WEG's actual output structure
        (work / "wave" / "effect").mkdir(parents=True)
        (work / "wave" / "composite").mkdir(parents=True)
        (work / "wave" / "preset").mkdir(parents=True)
        (work / "wave" / "effect" / "blur.png").write_bytes(b"blur-bytes")
        (work / "wave" / "composite" / "x.png").write_bytes(b"x-bytes")
        (work / "wave" / "preset" / "y.png").write_bytes(b"y-bytes")

        CacheSeeder(tmp_path).drain_work_dir(work, staging)

        assert (staging / "wave" / "effect" / "blur.png").read_bytes() == b"blur-bytes"
        assert (staging / "wave" / "composite" / "x.png").read_bytes() == b"x-bytes"
        assert (staging / "wave" / "preset" / "y.png").read_bytes() == b"y-bytes"
        # Work dir completely removed (including empty intermediate subdirs)
        assert not work.exists()
        assert not (work / "wave").exists()
