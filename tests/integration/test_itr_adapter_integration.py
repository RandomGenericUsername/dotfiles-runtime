"""Integration test for ItrAdapter with real itr binary (Story 1.9, AC 1-2).

Skipped loudly if itr not on PATH — proves env override writes SVGs
to output_dir and IconsEntry artifact_hashes match hash_file.

Hermeticity: the mapping file, the palette and the output live under
``tmp_path``. Only the *template corpus* is real, taken from the icon-templates
root (``dotfiles/assets/icon-templates`` or ``DOTFILES_ICON_TEMPLATES_DIR``).

The mapping uses the real catalog group shape: ``variants[].template`` is
resolved by ``itr`` against *its own* templates root (``PathResolutionService``
joins ``template_dir`` — default ``"."`` — under ``roots.template_root``,
``yaml_icon_config_loader.py:78,101``). ``ItrAdapter`` never passes
``--template-dir`` and never sets ``ICON_RENDERER__TEMPLATES__DIR`` itself, so
on a provisioned host ``itr`` renders from the settings chain's spine
(``~/.config/itr/settings.toml`` ``[templates] dir``), which is a *different*
input from the ``templates_dir`` the adapter hashes for the entry hash. This
test closes that gap without touching production code: it exports
``ICON_RENDERER__TEMPLATES__DIR`` for the subprocess (``ItrAdapter`` forwards
allowlisted ``ICON_RENDERER__*`` host vars via ``build_env``), so ``itr``
renders from exactly the corpus the adapter hashed.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from conftest import resolve_icon_templates_dir

from runtime.adapters.hashing import canonical_hash_dir, hash_file, icons_entry_hash
from runtime.adapters.itr_adapter import ItrAdapter

pytestmark = pytest.mark.integration

#: Real catalog template, relative to the icon-templates root. Same spelling as
#: the production mapping file (``dotfiles/config/icon-template-color-scheme-mappings/
#: icons.yaml``), so ``itr`` resolves it inside its own templates root.
_TEMPLATE_ROOT_GROUP = "status-bar/battery"

#: Placeholder carried by the real battery templates.
_PLACEHOLDER = "{{COLOR_FOREGROUND}}"

#: Palette value the placeholder must resolve to; also proves substitution ran.
_FOREGROUND = "#e0e0e0"


def _make_mappings(tmp_path: Path) -> Path:
    """Write a minimal icons mapping with the real catalog group shape.

    Group ``battery`` maps ``COLOR_FOREGROUND`` (the only placeholder the real
    templates carry) to the palette's ``foreground`` special key, and points its
    variants at the real ``status-bar/battery/...`` templates — no invented
    ``battery/`` directory, no per-group ``template_dir`` override. ``itr``
    resolves each ``template:`` under its templates root.
    """
    icons_yaml = tmp_path / "icons.yaml"
    icons_yaml.write_text(
        "battery:\n"
        "  color_mappings:\n"
        "    COLOR_FOREGROUND: foreground\n"
        "  variants:\n"
        "    - name: battery-0\n"
        "      template: status-bar/battery/battery-0/default/icon.svg\n"
        "      output: battery-0.svg\n"
        "    - name: battery-100\n"
        "      template: status-bar/battery/battery-100/default/icon.svg\n"
        "      output: battery-100.svg\n"
    )
    return icons_yaml


def test_itr_adapter_integration_real_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if shutil.which("itr") is None:
        pytest.skip("itr not on PATH — integration requires itr binary")

    # Resolve a real templates dir — env override (DOTFILES_ICON_TEMPLATES_DIR),
    # else the checked-in dotfiles icon-template assets. No retired-tree walk:
    # an absent input skips loudly instead of silently falling back.
    templates_dir = resolve_icon_templates_dir()
    if templates_dir is None:
        pytest.skip("icon templates dir not provided; discovery fallback removed")

    missing = [
        rel
        for rel in ("battery-0", "battery-100")
        if not (templates_dir / _TEMPLATE_ROOT_GROUP / rel / "default" / "icon.svg").is_file()
    ]
    if missing:
        pytest.skip(
            f"icon templates corpus {templates_dir} lacks the real battery group "
            f"({', '.join(missing)}) — cannot exercise a real render"
        )

    # Pin itr's own render root to the exact corpus we hash below, so the
    # entry hash inputs and the rendered inputs cannot disagree.
    monkeypatch.setenv("ICON_RENDERER__TEMPLATES__DIR", str(templates_dir))

    palette_hash = "a" * 64
    palette_entry = tmp_path / "state" / "cache" / "palettes" / palette_hash
    palette_entry.mkdir(parents=True, exist_ok=True)
    (palette_entry / "colors.yaml").write_text(
        f'special:\n  background: "#1a1a2e"\n  foreground: "{_FOREGROUND}"\ncolors: []\n'
    )

    mappings_path = _make_mappings(tmp_path)
    templates_hash = canonical_hash_dir(templates_dir)
    mappings_hash = hash_file(mappings_path)
    ih = icons_entry_hash(palette_hash, templates_hash, mappings_hash)

    output_dir = tmp_path / "state" / "cache" / "icons" / ih

    # Adapter with explicit palette_cache_dir pointing to entry
    adapter = ItrAdapter(
        templates_dir=templates_dir,
        mappings_path=mappings_path,
        palette_cache_dir=palette_entry,
    )
    entry = adapter.render(palette_hash, templates_dir, mappings_path, output_dir)

    # Verify SVGs exist, cover every variant, and were really rendered (the
    # placeholder is substituted, not copied through).
    svg_files = sorted(output_dir.rglob("*.svg"))
    assert svg_files, f"expected SVGs in {output_dir}"
    assert {p.name for p in svg_files} == {"battery-0.svg", "battery-100.svg"}
    for p in svg_files:
        assert p.is_file()
        assert not p.is_dir()
        body = p.read_text(encoding="utf-8")
        assert _PLACEHOLDER not in body, f"placeholder left unsubstituted in {p}"
        assert _FOREGROUND in body, f"palette foreground missing from {p}"

    assert entry.entry_hash == ih
    assert entry.hash_algorithm == "sha256"
    assert entry.kind == "icons"
    assert entry.source_palette_hash == palette_hash
    assert entry.input_templates_hash == templates_hash
    assert entry.input_mappings_hash == mappings_hash
    assert len(entry.artifact_hashes) == len(svg_files)
    for key, h in entry.artifact_hashes.items():
        assert len(h) == 64
        p = output_dir / key if (output_dir / key).is_file() else next(output_dir.rglob(key))
        assert h == hash_file(p)

    assert output_dir.is_dir()
