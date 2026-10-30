"""Palette producer resolution: runtime config, stale env, one-shot override.

REQ-004: the producer is owned by
``<install_spine>/config/dotfiles-runtime/producer.json``. Ambient
``RUNTIME__PALETTE__*`` values are reported but never authoritative.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from runtime.adapters.csg_adapter import CsgAdapter
from runtime.adapters.tpr_adapter import TprAdapter
from runtime.application.palette_producer import (
    PALETTE_PRODUCER_ENV,
    ProducerResolution,
    create_palette_producer,
    palette_producer_name,
    producer_config_path,
    resolve_palette_producer,
    tpr_templates_dir,
)

TPR_ENGINE_ENV = "RUNTIME__PALETTE__TPR_ENGINE"


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


def _write_config(
    spine: Path,
    *,
    producer: str = "tpr",
    engine: str = "native",
    runtime: str = "container",
    schema_version: int = 1,
    extra: dict[str, object] | None = None,
) -> Path:
    path = producer_config_path(spine)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "schema_version": schema_version,
        "producer": producer,
        "tpr_engine": engine,
        "tpr_runtime": runtime,
    }
    if extra is not None:
        payload.update(extra)
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    return path


def test_absent_config_defaults_to_csg_with_warning(tmp_path: Path) -> None:
    spine = _spine(tmp_path)
    resolution = resolve_palette_producer(spine, env={})

    assert resolution.producer == "csg"
    assert resolution.source == "default"
    assert resolution.engine is None
    assert resolution.runtime is None
    assert any("defaulting to csg" in warning for warning in resolution.warnings)
    assert isinstance(create_palette_producer(spine, env={}), CsgAdapter)
    assert palette_producer_name(spine, env={}) == "csg"


def test_config_tpr_wins_over_ambient_csg(tmp_path: Path) -> None:
    spine = _spine(tmp_path)
    _write_config(spine)

    resolution = resolve_palette_producer(
        spine,
        env={PALETTE_PRODUCER_ENV: "csg", TPR_ENGINE_ENV: "matugen"},
    )

    assert resolution.producer == "tpr"
    assert resolution.source == "config"
    assert resolution.engine == "native"
    assert resolution.runtime == "container"
    assert resolution.ignored_env == {
        PALETTE_PRODUCER_ENV: "csg",
        TPR_ENGINE_ENV: "matugen",
    }
    assert any(PALETTE_PRODUCER_ENV in warning for warning in resolution.warnings)
    assert any(TPR_ENGINE_ENV in warning for warning in resolution.warnings)

    producer = create_palette_producer(
        spine,
        env={PALETTE_PRODUCER_ENV: "csg", TPR_ENGINE_ENV: "matugen"},
    )
    assert isinstance(producer, TprAdapter)
    assert producer.templates_dir == spine / "config" / "theme-producer" / "templates"
    assert producer.engine == "native"
    assert producer.runtime == "container"


def test_explicit_override_wins_over_config(tmp_path: Path) -> None:
    spine = _spine(tmp_path)
    _write_config(spine, producer="tpr")

    resolution = resolve_palette_producer(spine, producer="csg")
    assert resolution.producer == "csg"
    assert resolution.source == "cli"
    assert isinstance(create_palette_producer(spine, producer="csg"), CsgAdapter)

    _write_config(spine, producer="csg")
    tpr = resolve_palette_producer(spine, producer="tpr")
    assert tpr.producer == "tpr"
    assert tpr.source == "cli"
    assert tpr.engine == "native"
    assert tpr.runtime == "container"


def test_config_engine_and_runtime_are_used(tmp_path: Path) -> None:
    spine = _spine(tmp_path)
    _write_config(spine, engine="matugen", runtime="local")

    producer = create_palette_producer(spine, env={})
    assert isinstance(producer, TprAdapter)
    assert producer.engine == "matugen"
    assert producer.runtime == "local"


def test_ambient_engine_matching_config_is_not_ignored(tmp_path: Path) -> None:
    spine = _spine(tmp_path)
    _write_config(spine, engine="native", runtime="container")
    resolution = resolve_palette_producer(spine, env={TPR_ENGINE_ENV: "native"})
    assert resolution.ignored_env == {}
    assert resolution.warnings == ()


@pytest.mark.parametrize(
    "payload",
    [
        "{not json",
        json.dumps([]),
        json.dumps({"schema_version": 2, "producer": "tpr"}),
        json.dumps({"schema_version": 1, "producer": "bogus"}),
        json.dumps({"schema_version": 1, "producer": "tpr", "tpr_engine": ""}),
        json.dumps({"schema_version": 1, "producer": "tpr", "tpr_runtime": "vm"}),
        json.dumps(
            {"schema_version": 1, "producer": "tpr", "unknown_field": True}
        ),
    ],
)
def test_malformed_config_fails_loud(tmp_path: Path, payload: str) -> None:
    spine = _spine(tmp_path)
    path = producer_config_path(spine)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")

    with pytest.raises(ValueError, match="invalid palette producer config"):
        resolve_palette_producer(spine)
    with pytest.raises(ValueError, match="invalid palette producer config"):
        create_palette_producer(spine)


def test_unknown_explicit_producer_fails_loud(tmp_path: Path) -> None:
    spine = _spine(tmp_path)
    with pytest.raises(ValueError, match="unknown palette producer 'bogus'"):
        resolve_palette_producer(spine, producer="bogus")


def test_resolution_to_dict_is_the_inspect_shape(tmp_path: Path) -> None:
    spine = _spine(tmp_path)
    _write_config(spine)
    resolution = resolve_palette_producer(
        spine, env={PALETTE_PRODUCER_ENV: "csg"}
    )
    assert isinstance(resolution, ProducerResolution)
    payload = resolution.to_dict()
    assert set(payload) == {
        "producer",
        "source",
        "engine",
        "runtime",
        "config_path",
        "ambient_env",
        "ignored_env",
        "warnings",
    }
    assert payload["producer"] == "tpr"
    assert payload["source"] == "config"
    assert payload["config_path"] == str(producer_config_path(spine))
    assert payload["ignored_env"] == {PALETTE_PRODUCER_ENV: "csg"}
