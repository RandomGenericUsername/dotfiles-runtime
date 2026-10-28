"""Palette producer selector: csg default, tpr opt-in, unknown fails loud."""

from __future__ import annotations

from pathlib import Path

import pytest

from runtime.adapters.csg_adapter import CsgAdapter
from runtime.adapters.tpr_adapter import TprAdapter
from runtime.application.palette_producer import (
    PALETTE_PRODUCER_ENV,
    create_palette_producer,
    palette_producer_name,
    tpr_templates_dir,
)


def _spine(tmp_path: Path, *, with_csg_templates: bool = True) -> Path:
    spine = tmp_path / "install"
    if with_csg_templates:
        csg = spine / "config" / "color-scheme-generator" / "templates"
        csg.mkdir(parents=True)
        (csg / "default.yaml").write_text("window: {}\n")
    tpr = tpr_templates_dir(spine)
    tpr.mkdir(parents=True)
    (tpr / "colors.yaml.j2").write_text("background: {{ bg }}\n")
    return spine


def test_absent_selector_defaults_to_csg(tmp_path: Path) -> None:
    spine = _spine(tmp_path)
    assert palette_producer_name({}) == "csg"
    producer = create_palette_producer(spine, env={})
    assert isinstance(producer, CsgAdapter)
    assert producer.producer_name == "csg"
    assert producer.templates_dir == spine / "config" / "color-scheme-generator" / "templates"


def test_blank_selector_defaults_to_csg(tmp_path: Path) -> None:
    spine = _spine(tmp_path)
    assert palette_producer_name({PALETTE_PRODUCER_ENV: "  "}) == "csg"
    assert isinstance(create_palette_producer(spine, env={PALETTE_PRODUCER_ENV: ""}), CsgAdapter)


def test_tpr_selector_composes_tpr_adapter(tmp_path: Path) -> None:
    spine = _spine(tmp_path)
    producer = create_palette_producer(
        spine,
        env={
            PALETTE_PRODUCER_ENV: "tpr",
            "RUNTIME__PALETTE__TPR_ENGINE": "matugen",
        },
    )
    assert isinstance(producer, TprAdapter)
    assert producer.producer_name == "tpr"
    assert producer.templates_dir == spine / "config" / "theme-producer" / "templates"
    assert producer.engine == "matugen"


def test_tpr_engine_defaults_to_native(tmp_path: Path) -> None:
    spine = _spine(tmp_path)
    producer = create_palette_producer(spine, env={PALETTE_PRODUCER_ENV: "tpr"})
    assert isinstance(producer, TprAdapter)
    assert producer.engine == "native"


def test_unknown_selector_fails_with_clear_error(tmp_path: Path) -> None:
    spine = _spine(tmp_path)
    with pytest.raises(ValueError, match="unknown palette producer 'bogus'"):
        create_palette_producer(spine, env={PALETTE_PRODUCER_ENV: "bogus"})
