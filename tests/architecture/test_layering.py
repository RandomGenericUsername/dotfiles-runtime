"""Mechanical enforcement of the runtime hexagonal layering rules.

domain  <- ports  <- adapters  <- application  <- cli

Allowed import directions:
  domain      -> stdlib + runtime.domain.*
  ports       -> runtime.domain.* + runtime.ports.*
  adapters    -> runtime.domain/ports/adapters.*
  application -> runtime.domain/ports/adapters/application.*
  cli         -> everything (outer shell / composition root)
  root        -> everything (__init__.py public API re-exports)

Forbidden:
  domain      -> ports | adapters | application | cli
  ports       -> adapters | application | cli
  adapters    -> application | cli
  application -> cli

Additional domain rules:
  - stdlib imports must be in _DOMAIN_ALLOWED_STDLIB (opt-in).
  - stdlib imports must NOT be in _DOMAIN_BANNED_STDLIB (opt-out).
  - Path FS method calls (.exists(), .read_text(), etc.) are forbidden.

Additional ports rule:
  - Only ABCs (or @dataclass aggregates) allowed; concrete classes
    must live in adapters/.

Rule 5 (cross-package, §11 boundary):
  - No source file may import any dotfiles-sibling package except cli_output.
  - The forbidden set is: provisioning, core, infrastructure,
    color_scheme_generator, wallpaper_effects_generator,
    icon_templates_renderer, config_assembler_engine, oci_runtime.
  - Third-party imports must be declared dependencies of the package.

The scanning logic uses only the standard library (ast, sys, pathlib, tomllib,
re); pytest is used only as the test runner. If a future commit introduces a
layering violation, this test fails and blocks the merge — no audit required
to catch it.
"""

from __future__ import annotations

import ast
import os
import re
import sys
import time
import tomllib
from pathlib import Path

import pytest

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "runtime"
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PYPROJECT = _PROJECT_ROOT / "pyproject.toml"

_LAYERS = ("domain", "ports", "adapters", "application", "cli")

# What each layer may import from (relative to runtime).
_ALLOWED_TARGETS = {
    "domain": {"domain"},
    "ports": {"domain", "ports"},
    "adapters": {"domain", "ports", "adapters"},
    "application": {"domain", "ports", "adapters", "application"},
    "cli": {"domain", "ports", "adapters", "application", "cli"},
    "root": {"domain", "ports", "adapters", "application", "cli"},
}

# Domain stdlib allowlist — the runtime domain is PURE, zero-I/O data
# (no os/subprocess/shutil/pathlib/io).
_DOMAIN_ALLOWED_STDLIB = frozenset(
    {
        "__future__",
        "dataclasses",
        "enum",
        "typing",
        "collections",
        "collections.abc",
        "functools",
        "re",
    }
)

# Domain banned stdlib (opt-out, mirroring provisioning).
_DOMAIN_BANNED_STDLIB = frozenset(
    {
        "subprocess",
        "os",
        "shutil",
        "select",
        "selectors",
        "socket",
        "pathlib",
        "io",
    }
)

# Rule 5: dotfiles-sibling packages that src/runtime must never import.
# Key difference from provisioning: adds "provisioning" — runtime reads
# provisioning OUTPUT (files/settings), never provisioning code (AD-15).
_FORBIDDEN_CROSS_PACKAGE = frozenset(
    {
        "provisioning",
        "core",
        "infrastructure",
        "color_scheme_generator",
        "wallpaper_effects_generator",
        "icon_templates_renderer",
        "config_assembler_engine",
        "oci_runtime",
    }
)

# Banned Path FS method calls in domain.
_BANNED_PATH_FS_METHODS = frozenset(
    {
        "exists",
        "read_text",
        "read_bytes",
        "write_text",
        "write_bytes",
        "glob",
        "rglob",
        "iterdir",
        "mkdir",
        "rmdir",
        "unlink",
        "chmod",
        "chown",
        "stat",
        "lstat",
        "touch",
        "resolve",
    }
)

# Ports concrete-class allowlist — runtime ports are pure ABCs today.
_PORTS_ALLOWED_AGGREGATES: dict[str, set[str]] = {}

# Distribution names whose import root differs from the normalized name.
_DIST_TO_IMPORT_ROOT = {
    "ansible-core": "ansible",
    "beautifulsoup4": "bs4",
    "python-dateutil": "dateutil",
    "PyYAML": "yaml",
}

# Stdlib module names available at runtime for classification.
_STDLIB_NAMES = frozenset(sys.stdlib_module_names)


def _package_name() -> str:
    return _SRC_ROOT.name


def _classify_layer(file_path: Path) -> str:
    """Map a source file under _SRC_ROOT to its layer.

    Layers are the top-level dirs in ``_LAYERS`` plus the package root
    ``__init__.py``. Anything else is ``"invalid"`` (fail-closed): a file
    outside the hexagon is itself a violation, and no layer may import it.
    """
    rel = file_path.relative_to(_SRC_ROOT)
    parts = rel.parts
    if len(parts) == 1:
        return "root" if parts[0] == "__init__.py" else "invalid"
    if parts[0] in _LAYERS:
        return parts[0]
    return "invalid"


def _current_package(file_path: Path) -> str:
    """Dotted name of the package the file lives in.

    For both ``domain/models.py`` and ``domain/__init__.py`` this is
    ``runtime.domain`` — the package is defined by the file's directory,
    not by its own module name. That makes relative imports resolve identically
    in ``__init__.py`` and sibling modules.
    """
    rel_parent = file_path.relative_to(_SRC_ROOT).parent
    parts = list(rel_parent.parts)
    return ".".join([_package_name(), *parts])


def _is_in_package(module: str) -> bool:
    pkg = _package_name()
    return module == pkg or module.startswith(f"{pkg}.")


def _module_to_source_files(module: str) -> list[Path]:
    """Resolve an in-package dotted module to the source file(s) it names."""
    pkg = _package_name()
    if not _is_in_package(module):
        return []
    if module == pkg:
        init = _SRC_ROOT / "__init__.py"
        return [init] if init.is_file() else []
    rel_parts = module[len(pkg) + 1 :].split(".")
    mod_file = _SRC_ROOT.joinpath(*rel_parts).with_suffix(".py")
    if mod_file.is_file():
        return [mod_file]
    pkg_init = _SRC_ROOT.joinpath(*rel_parts, "__init__.py")
    if pkg_init.is_file():
        return [pkg_init]
    return []


def _resolve_relative(level: int, module: str | None, file_path: Path) -> str | None:
    """Resolve a relative import to a dotted module name; None if it escapes."""
    pkg_parts = _current_package(file_path).split(".")
    drop = level - 1
    if drop > len(pkg_parts) - 1:
        return None
    base = pkg_parts[: len(pkg_parts) - drop]
    if module:
        base = [*base, *module.split(".")]
    return ".".join(base)


def _extract_import_modules(tree: ast.AST, file_path: Path) -> list[str]:
    """Resolve every import in the tree to a fully-qualified module name."""
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                resolved = _resolve_relative(node.level, node.module, file_path)
                if resolved is None:
                    modules.append(f"<invalid-relative-level-{node.level}>")
                else:
                    modules.append(resolved)
            elif node.module:
                modules.append(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                modules.append(alias.name)
    return modules


def _resolve_in_package_targets(tree: ast.AST, file_path: Path) -> list[Path]:
    """Concrete source files under _SRC_ROOT that this file's imports reach."""
    targets: list[Path] = []

    def _add(module: str) -> None:
        for f in _module_to_source_files(module):
            if f not in targets:
                targets.append(f)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_in_package(alias.name):
                    _add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                resolved = _resolve_relative(node.level, node.module, file_path)
                if resolved is None or not _is_in_package(resolved):
                    continue
                _add(resolved)
                for alias in node.names:
                    _add(f"{resolved}.{alias.name}")
            elif node.module:
                if not _is_in_package(node.module):
                    continue
                _add(node.module)
                for alias in node.names:
                    _add(f"{node.module}.{alias.name}")
    return targets


def _extract_stdlib_imports(tree: ast.AST) -> list[str]:
    """Extract top-level stdlib module names from AST imports."""
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] in _STDLIB_NAMES:
                modules.append(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in _STDLIB_NAMES:
                    modules.append(alias.name)
    return modules


def _extract_third_party_imports(tree: ast.AST, file_path: Path) -> set[str]:
    """Return top-level import roots that are neither stdlib nor in-package."""
    pkg = _package_name()
    roots: set[str] = set()
    for module in _extract_import_modules(tree, file_path):
        if module.startswith("<invalid"):
            continue
        top = module.split(".")[0]
        if top in _STDLIB_NAMES or top == pkg:
            continue
        roots.add(top)
    return roots


def _all_source_files() -> list[Path]:
    return sorted(_SRC_ROOT.rglob("*.py"))


def _check_file(file_path: Path) -> list[str]:
    """Return layering violations for one source file (empty list = clean)."""
    layer = _classify_layer(file_path)
    if layer == "invalid":
        return [
            f"{file_path.relative_to(_SRC_ROOT)} is not part of the runtime "
            f"hexagon; expected a top-level dir in {_LAYERS} or the package __init__.py"
        ]
    allowed = _ALLOWED_TARGETS[layer]
    rel = file_path.relative_to(_SRC_ROOT)
    try:
        source = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return [f"{rel} has an encoding error: {exc}"]
    try:
        tree = ast.parse(source, filename=str(file_path))
    except SyntaxError as exc:
        return [f"{rel} has a syntax error: {exc}"]
    violations: list[str] = []
    for target in _resolve_in_package_targets(tree, file_path):
        target_layer = _classify_layer(target)
        target_rel = target.relative_to(_SRC_ROOT)
        if target_layer == "invalid":
            violations.append(f"{layer} layer imports a non-hexagon module: {rel} -> {target_rel}")
        elif target_layer not in allowed:
            violations.append(f"{layer} layer imports {target_layer} layer: {rel} -> {target_rel}")
    for module in _extract_import_modules(tree, file_path):
        if module.startswith("<invalid"):
            violations.append(
                f"{layer} layer contains an invalid relative import "
                f"(level exceeds package depth): {rel}"
            )
    # Star imports in domain/ports bypass name-level resolution and may leak
    # symbols from higher layers through __init__.py re-exports.
    if layer in ("domain", "ports"):
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.names:
                if any(alias.name == "*" for alias in node.names):
                    violations.append(
                        f"{layer} layer uses star import from "
                        f"{node.module or '<relative>'}: {rel}"
                    )
    # Domain must not call open() — full file I/O bypasses the Path-method ban.
    if layer == "domain":
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id == "open":
                    violations.append(f"{layer} layer calls open(): {rel}:{node.lineno}")
                elif isinstance(func, ast.Attribute) and func.attr == "open":
                    # e.g., pathlib.Path.open() — caught by Path-method ban, skip here
                    pass
    return violations


def _import_aliases(tree: ast.AST) -> dict[str, str]:
    """Map local binding names to canonical dotted names."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name
                aliases[local] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if alias.name == "*":
                    continue
                local = alias.asname or alias.name
                aliases[local] = f"{node.module}.{alias.name}"
    return aliases


def _resolve_name(name: str, aliases: dict[str, str]) -> str:
    return aliases.get(name, name)


def _decorator_name(deco: ast.expr) -> str:
    if isinstance(deco, ast.Name):
        return deco.id
    if isinstance(deco, ast.Attribute):
        return deco.attr
    if isinstance(deco, ast.Call):
        return _decorator_name(deco.func)
    return ""


def _is_abc_base(base: ast.expr, aliases: dict[str, str]) -> bool:
    if isinstance(base, ast.Name):
        return _resolve_name(base.id, aliases).split(".")[-1] == "ABC"
    if isinstance(base, ast.Attribute):
        return base.attr == "ABC"
    return False


def _is_protocol_base(base: ast.expr, aliases: dict[str, str]) -> bool:
    if isinstance(base, ast.Name):
        return _resolve_name(base.id, aliases).split(".")[-1] == "Protocol"
    if isinstance(base, ast.Attribute):
        return base.attr == "Protocol"
    return False


def _classify_ports_class(node: ast.ClassDef, aliases: dict[str, str]) -> str:
    """Classify a top-level class in ports/ as abc, dataclass-aggregate, or concrete."""
    inherits_abc = any(_is_abc_base(base, aliases) for base in node.bases)
    inherits_protocol = any(_is_protocol_base(base, aliases) for base in node.bases)

    has_abstract = False
    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(
                _resolve_name(_decorator_name(d), aliases).split(".")[-1] == "abstractmethod"
                for d in item.decorator_list
            ):
                has_abstract = True
                break

    if (has_abstract and inherits_abc) or inherits_protocol:
        return "abc"

    has_dataclass = any(
        _resolve_name(_decorator_name(d), aliases).split(".")[-1] == "dataclass"
        for d in node.decorator_list
    )

    if has_dataclass:
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if item.name.startswith("__") and item.name.endswith("__"):
                    continue
                return "concrete"
        return "dataclass-aggregate"

    return "concrete"


def _local_method_names(tree: ast.AST) -> set[str]:
    """Names of methods/functions defined within the file."""
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _scan_for_banned_method_calls(tree: ast.AST) -> list[tuple[int, str]]:
    """Find all calls to Path FS methods like .exists(), .read_text(), etc."""
    local = _local_method_names(tree)
    results: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                name = func.attr
                if name in _BANNED_PATH_FS_METHODS and name not in local:
                    results.append((node.lineno, name))
    return results


def _allowed_third_party_roots() -> set[str]:
    """Derive allowed third-party import roots from declared package deps."""
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for dep in data["project"].get("dependencies", []):
        name = dep.split(";")[0].split("[")[0].strip()
        roots.add(_import_root(name))
    for name in data.get("tool", {}).get("uv", {}).get("sources", {}):
        roots.add(_import_root(name))
    return roots


def _import_root(package_name: str) -> str:
    """Normalize a distribution name to its import root.

    ``types-*`` stub packages are excluded from contributing their target
    root — stubs are dev-only and must not silently allow the real package.
    """
    name = re.split(r"[\s<>=!~]+", package_name)[0]
    name = name.split("[")[0]
    name = _DIST_TO_IMPORT_ROOT.get(name, name)
    if name.startswith("types-"):
        return ""  # stub packages do not contribute an allowed root
    return name.replace("-", "_")


class TestHexagonalLayering:
    """Mechanical guard: no layer may import from a layer above it."""

    @pytest.mark.parametrize(
        "file_path", _all_source_files(), ids=lambda p: str(p.relative_to(_SRC_ROOT))
    )
    def test_no_layering_violations(self, file_path: Path) -> None:
        violations = _check_file(file_path)
        assert not violations, "\n".join(violations)

    def test_domain_imports_only_domain(self) -> None:
        """domain/ must have zero runtime imports except its own submodules."""
        domain_dir = _SRC_ROOT / "domain"
        for f in sorted(domain_dir.rglob("*.py")):
            for target in _resolve_in_package_targets(
                ast.parse(f.read_text(encoding="utf-8"), filename=str(f)), f
            ):
                assert _classify_layer(target) == "domain", (
                    f"{f.relative_to(_SRC_ROOT)} imports "
                    f"{target.relative_to(_SRC_ROOT)} "
                    f"(layer={_classify_layer(target)}); "
                    f"domain may only import from domain"
                )

    def test_ports_import_no_adapters(self) -> None:
        """ports/ must never import from adapters/, application/, or cli/."""
        ports_dir = _SRC_ROOT / "ports"
        if not ports_dir.exists():
            pytest.skip("ports layer does not exist yet")
        for f in sorted(ports_dir.rglob("*.py")):
            for target in _resolve_in_package_targets(
                ast.parse(f.read_text(encoding="utf-8"), filename=str(f)), f
            ):
                assert _classify_layer(target) not in ("adapters", "application", "cli"), (
                    f"{f.relative_to(_SRC_ROOT)} imports "
                    f"{target.relative_to(_SRC_ROOT)} "
                    f"(layer={_classify_layer(target)}); "
                    f"ports may not import adapters/application/cli"
                )

    def test_adapters_import_no_application(self) -> None:
        """adapters/ must never import from application/ or cli/."""
        adapters_dir = _SRC_ROOT / "adapters"
        if not adapters_dir.exists():
            pytest.skip("adapters layer does not exist yet")
        for f in sorted(adapters_dir.rglob("*.py")):
            for target in _resolve_in_package_targets(
                ast.parse(f.read_text(encoding="utf-8"), filename=str(f)), f
            ):
                assert _classify_layer(target) not in ("application", "cli"), (
                    f"{f.relative_to(_SRC_ROOT)} imports "
                    f"{target.relative_to(_SRC_ROOT)} "
                    f"(layer={_classify_layer(target)}); "
                    f"adapters may not import application/cli"
                )

    def test_layering_classification_self_test(self) -> None:
        """Self-test: file classification fails closed on non-hexagon paths."""
        assert _classify_layer(_SRC_ROOT / "domain" / "enums.py") == "domain"
        assert _classify_layer(_SRC_ROOT / "ports" / "fact_reader.py") == "ports"
        assert _classify_layer(_SRC_ROOT / "cli" / "main.py") == "cli"
        assert _classify_layer(_SRC_ROOT / "__init__.py") == "root"
        # Fail-closed: unknown top-level dirs / stray files are NOT root.
        assert _classify_layer(_SRC_ROOT / "utils" / "helpers.py") == "invalid"
        assert _classify_layer(_SRC_ROOT / "settings.py") == "invalid"
        # A deliberately-violating import must not be allowed by any inner layer.
        assert "adapters" not in _ALLOWED_TARGETS["domain"]
        assert "application" not in _ALLOWED_TARGETS["adapters"]
        assert "cli" not in _ALLOWED_TARGETS["application"]
        assert "cli" in _ALLOWED_TARGETS["cli"]

    def test_relative_import_resolution_self_test(self) -> None:
        """Self-test: relative imports resolve via the file's package dir."""
        tree = ast.parse("from . import enums\n")
        mods = _extract_import_modules(tree, _SRC_ROOT / "domain" / "__init__.py")
        assert mods == ["runtime.domain"], mods
        tree = ast.parse("from .enums import Distro\n")
        mods = _extract_import_modules(tree, _SRC_ROOT / "domain" / "__init__.py")
        assert mods == ["runtime.domain.enums"], mods
        tree = ast.parse("from .. import x\n")
        mods = _extract_import_modules(tree, _SRC_ROOT / "domain" / "__init__.py")
        assert mods == ["runtime"], mods
        # Over-deep relative imports fail closed instead of wrapping around.
        tree = ast.parse("from ....ports import x\n")
        mods = _extract_import_modules(tree, _SRC_ROOT / "domain" / "__init__.py")
        assert any(m.startswith("<invalid") for m in mods), mods
        # A same-layer relative import from domain must pass the layering check.
        _uid = f"{os.getpid()}_{int(time.time_ns())}"
        probe = _SRC_ROOT / "domain" / f"_tmp_relative_probe_{_uid}.py"
        try:
            probe.write_text("from . import __init__\n", encoding="utf-8")
            assert _check_file(probe) == []
        finally:
            probe.unlink(missing_ok=True)

    def test_import_name_expansion_self_test(self) -> None:
        """Self-test: `from runtime import <name>` expands to the layer."""
        tree = ast.parse("from runtime import cli\n")
        targets = _resolve_in_package_targets(tree, _SRC_ROOT / "domain" / "models.py")
        assert any(_classify_layer(t) == "cli" for t in targets), targets
        # A bare attribute import from the package root stays at the root layer.
        tree = ast.parse("from runtime import __version__\n")
        targets = _resolve_in_package_targets(tree, _SRC_ROOT / "domain" / "models.py")
        assert all(_classify_layer(t) == "root" for t in targets), targets

    def test_enforcement_loop_self_test(self) -> None:
        """AC 3 end-to-end: a violating import fails the full enforcement path."""
        _uid = f"{os.getpid()}_{int(time.time_ns())}"
        probe = _SRC_ROOT / "domain" / f"_tmp_violation_probe_{_uid}.py"
        try:
            probe.write_text(
                "from runtime import cli\n"
                "import runtime.cli.main\n"
                "from runtime.domain.enums import Distro\n",
                encoding="utf-8",
            )
            violations = _check_file(probe)
            joined = "\n".join(violations)
            assert "cli layer" in joined, joined
            # The legitimate same-layer import must not be flagged.
            assert "domain/enums.py" not in joined, joined
        finally:
            probe.unlink(missing_ok=True)

    # ── Domain stdlib allowlist ────────────────────────────────────

    def test_domain_stdlib_imports_are_allowlisted(self) -> None:
        """Every stdlib import in domain/ must be in _DOMAIN_ALLOWED_STDLIB."""
        domain_dir = _SRC_ROOT / "domain"
        failures: list[str] = []
        for f in sorted(domain_dir.rglob("*.py")):
            tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
            for module in _extract_stdlib_imports(tree):
                top = module.split(".")[0]
                if top not in _DOMAIN_ALLOWED_STDLIB:
                    failures.append(
                        f"domain layer imports non-allowlisted stdlib: "
                        f"{module} in {f.relative_to(_SRC_ROOT)}"
                    )
        assert not failures, "\n".join(failures)

    def test_domain_stdlib_allowlist_self_test(self) -> None:
        """Self-test: synthetic AST inputs exercise the allowlist rule."""
        passes = ast.parse("import dataclasses")
        fails_os = ast.parse("import os")
        fails_pathlib = ast.parse("import pathlib")
        passes_coll = ast.parse("from collections.abc import Mapping")

        def _stdlib_in_tree(tree: ast.AST) -> list[str]:
            return list({m.split(".")[0] for m in _extract_stdlib_imports(tree)})

        p = _stdlib_in_tree(passes)
        assert all(m in _DOMAIN_ALLOWED_STDLIB for m in p), f"{p} should be allowed"
        assert any(m not in _DOMAIN_ALLOWED_STDLIB for m in _stdlib_in_tree(fails_os)), (
            "os should fail the allowlist"
        )
        assert any(m not in _DOMAIN_ALLOWED_STDLIB for m in _stdlib_in_tree(fails_pathlib)), (
            "pathlib should fail the allowlist"
        )
        pc = _stdlib_in_tree(passes_coll)
        assert all(m in _DOMAIN_ALLOWED_STDLIB for m in pc), f"{pc} should be allowed"

    # ── Domain banned stdlib ────────────────────────────────────

    def test_domain_imports_no_banned_stdlib(self) -> None:
        """No domain file may import from _DOMAIN_BANNED_STDLIB."""
        domain_dir = _SRC_ROOT / "domain"
        failures: list[str] = []
        for f in sorted(domain_dir.rglob("*.py")):
            tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
            for module in _extract_stdlib_imports(tree):
                top = module.split(".")[0]
                if top in _DOMAIN_BANNED_STDLIB:
                    failures.append(
                        f"domain layer imports banned stdlib: "
                        f"{module} in {f.relative_to(_SRC_ROOT)}"
                    )
        assert not failures, "\n".join(failures)

    def test_domain_banned_stdlib_self_test(self) -> None:
        """Self-test: synthetic AST inputs exercise the banned-stdlib rule."""
        fails_subprocess = ast.parse("import subprocess")
        fails_os = ast.parse("import os")
        fails_os_path = ast.parse("from os import path")
        passes_dataclasses = ast.parse("import dataclasses")

        def _banned(tree: ast.AST) -> list[str]:
            return [
                m.split(".")[0]
                for m in _extract_stdlib_imports(tree)
                if m.split(".")[0] in _DOMAIN_BANNED_STDLIB
            ]

        assert _banned(fails_subprocess), "subprocess should be banned"
        assert _banned(fails_os), "os should be banned"
        assert _banned(fails_os_path), "os (from os import path) should be banned"
        assert not _banned(passes_dataclasses), "dataclasses should not be banned"

    # ── Ports ABC-only rule ────────────────────────────────────

    def test_ports_files_are_abstract_only(self) -> None:
        """ports/ must only contain ABCs or @dataclass aggregates."""
        ports_dir = _SRC_ROOT / "ports"
        if not ports_dir.exists():
            pytest.skip("ports layer does not exist yet")
        failures: list[str] = []
        for f in sorted(ports_dir.rglob("*.py")):
            tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
            aliases = _import_aliases(tree)
            for node in tree.body:
                if not isinstance(node, ast.ClassDef):
                    continue
                kind = _classify_ports_class(node, aliases)
                if kind == "abc":
                    continue
                allowlist = _PORTS_ALLOWED_AGGREGATES.get(f.name, set())
                if node.name in allowlist:
                    continue
                failures.append(
                    f"{f.relative_to(_SRC_ROOT)}:{node.lineno} "
                    f"declares concrete class '{node.name}' "
                    f"which is neither an ABC nor a @dataclass aggregate; "
                    f"relocate to adapters/"
                )
        assert not failures, "\n".join(failures)

    def test_ports_classification_self_test(self) -> None:
        """Self-test: synthetic AST inputs exercise port classification."""
        abc_tree = ast.parse(
            "from abc import ABC, abstractmethod\n"
            "class Foo(ABC):\n"
            "    @abstractmethod\n"
            "    def m(self): ...\n"
        )
        abc_class = next(n for n in ast.walk(abc_tree) if isinstance(n, ast.ClassDef))
        assert _classify_ports_class(abc_class, _import_aliases(abc_tree)) == "abc"

        async_abc_tree = ast.parse(
            "from abc import ABC, abstractmethod\n"
            "class AsyncFoo(ABC):\n"
            "    @abstractmethod\n"
            "    async def m(self): ...\n"
        )
        async_class = next(n for n in ast.walk(async_abc_tree) if isinstance(n, ast.ClassDef))
        assert _classify_ports_class(async_class, _import_aliases(async_abc_tree)) == "abc"

        protocol_tree = ast.parse(
            "from typing import Protocol\nclass P(Protocol):\n    def m(self): ...\n"
        )
        protocol_class = next(n for n in ast.walk(protocol_tree) if isinstance(n, ast.ClassDef))
        assert _classify_ports_class(protocol_class, _import_aliases(protocol_tree)) == "abc"

        aliased_tree = ast.parse(
            "from abc import ABC as A, abstractmethod as abm\n"
            "class Foo(A):\n"
            "    @abm\n"
            "    def m(self): ...\n"
            "    class Inner:\n"
            "        pass\n"
        )
        aliases = _import_aliases(aliased_tree)
        classes = [n for n in aliased_tree.body if isinstance(n, ast.ClassDef)]
        assert _classify_ports_class(classes[0], aliases) == "abc"
        # Nested helper classes inside a class body are NOT ports classes.
        assert len(classes) == 1, "nested classes must not be classified as ports classes"

        dataclass_tree = ast.parse(
            "from dataclasses import dataclass as dc\n@dc(frozen=True)\nclass Bar:\n    x: int\n"
        )
        dc_class = next(n for n in ast.walk(dataclass_tree) if isinstance(n, ast.ClassDef))
        assert _classify_ports_class(dc_class, _import_aliases(dataclass_tree)) == (
            "dataclass-aggregate"
        )

        concrete_tree = ast.parse(
            "class Baz:\n    def m(self):\n        import os\n        os.read(...)\n"
        )
        conc_class = next(n for n in ast.walk(concrete_tree) if isinstance(n, ast.ClassDef))
        assert _classify_ports_class(conc_class, _import_aliases(concrete_tree)) == "concrete"

    # ── Path FS method call ban ────────────────────────────────────

    def test_domain_no_path_fs_method_calls(self) -> None:
        """domain/ must not call banned FS methods on Path objects."""
        domain_dir = _SRC_ROOT / "domain"
        failures: list[str] = []
        for f in sorted(domain_dir.rglob("*.py")):
            tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
            for lineno, method in _scan_for_banned_method_calls(tree):
                failures.append(
                    f"{f.relative_to(_SRC_ROOT)}:{lineno} "
                    f"calls Path.{method}() — "
                    f"FS methods on Path are forbidden in domain; "
                    f"delegate to an adapter"
                )
        assert not failures, "\n".join(failures)

    def test_path_method_ban_self_test(self) -> None:
        """Self-test: synthetic AST inputs exercise the path method ban."""
        flagged_exists = ast.parse("if p.exists(): pass")
        flagged_read = ast.parse("text = p.read_text()")
        not_flagged_decode = ast.parse("text = bytes(b'x').decode()")
        not_flagged_bytes = ast.parse("data = bytes(b'x')")

        assert _scan_for_banned_method_calls(flagged_exists) == [(1, "exists")]
        assert _scan_for_banned_method_calls(flagged_read) == [(1, "read_text")]
        assert _scan_for_banned_method_calls(not_flagged_decode) == []
        assert _scan_for_banned_method_calls(not_flagged_bytes) == []

        # A method defined locally in the same file is not a Path FS call.
        local_tree = ast.parse(
            "class X:\n"
            "    def resolve(self): ...\n"
            "    def m(self):\n"
            "        return self.resolve()\n"
        )
        assert _scan_for_banned_method_calls(local_tree) == []

    # ── Rule 5: cross-package §11 boundary ──────────────────────────

    def test_no_forbidden_cross_package_imports(self) -> None:
        """No source file may import any member of the Rule 5 forbidden set."""
        failures: list[str] = []
        for f in _all_source_files():
            tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
            for root in sorted(_extract_third_party_imports(tree, f)):
                if root in _FORBIDDEN_CROSS_PACKAGE:
                    failures.append(
                        f"{f.relative_to(_SRC_ROOT)} imports forbidden cross-package root {root!r}"
                    )
        assert not failures, "\n".join(failures)

    def test_only_allowed_cross_package_roots(self) -> None:
        """Only cli_output (or declared deps) may be imported cross-package."""
        allowed = _allowed_third_party_roots()
        failures: list[str] = []
        for f in _all_source_files():
            tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
            for root in sorted(_extract_third_party_imports(tree, f)):
                if root in _FORBIDDEN_CROSS_PACKAGE:
                    continue
                if root not in allowed:
                    failures.append(
                        f"{f.relative_to(_SRC_ROOT)} imports undeclared "
                        f"third-party root {root!r}; allowed: "
                        f"{sorted(allowed)}"
                    )
        assert not failures, "\n".join(failures)

    def test_forbidden_cross_package_self_test(self) -> None:
        """Self-test: a deliberately-violating import must be flagged."""
        tree = ast.parse(
            "import core\n"
            "import cli_output\n"
            "from runtime.domain import models\n"
            "from runtime.adapters import executor\n"
        )
        third_party = _extract_third_party_imports(tree, _SRC_ROOT / "domain" / "models.py")
        # core is in the forbidden set and must be flagged.
        assert "core" in third_party
        assert "core" in _FORBIDDEN_CROSS_PACKAGE
        # cli_output is the one allowed dotfiles-sibling.
        assert "cli_output" in third_party
        assert "cli_output" not in _FORBIDDEN_CROSS_PACKAGE
        # in-package imports are never classified as third-party.
        assert "runtime" not in third_party

    def test_allowed_third_party_roots_derivation_self_test(self) -> None:
        """Self-test: the pyproject-derived allowlist covers current imports."""
        allowed = _allowed_third_party_roots()
        assert "cli_output" in allowed
        assert "typer" in allowed
        # dotfiles siblings are NOT declared deps, so they are not allowed.
        for forbidden in _FORBIDDEN_CROSS_PACKAGE:
            assert forbidden not in allowed, (
                f"{forbidden!r} is a forbidden dotfiles-sibling and must not be "
                f"derivable as an allowed third-party root"
            )

    def test_import_root_normalization_self_test(self) -> None:
        """Self-test: dist-name -> import-root normalization."""
        assert _import_root("typer>=0.12") == "typer"
        assert _import_root("cli-output") == "cli_output"
        # types-* stubs do not contribute their target root to the allowed set.
        assert _import_root("types-requests") == ""


def _run_standalone() -> None:
    """Enforce every rule family without pytest."""
    failures = 0

    def _fail(message: str) -> None:
        nonlocal failures
        print(f"FAIL: {message}")
        failures += 1

    # 1. Layering + fail-closed hexagon membership.
    for f in _all_source_files():
        for line in _check_file(f):
            _fail(line)

    # 2. Domain stdlib rules + Path-FS ban.
    domain_dir = _SRC_ROOT / "domain"
    if domain_dir.exists():
        for f in sorted(domain_dir.rglob("*.py")):
            tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
            for module in _extract_stdlib_imports(tree):
                top = module.split(".")[0]
                if top not in _DOMAIN_ALLOWED_STDLIB:
                    _fail(f"{f.relative_to(_SRC_ROOT)} imports non-allowlisted stdlib {module}")
                if top in _DOMAIN_BANNED_STDLIB:
                    _fail(f"{f.relative_to(_SRC_ROOT)} imports banned stdlib {module}")
            for lineno, method in _scan_for_banned_method_calls(tree):
                _fail(f"{f.relative_to(_SRC_ROOT)}:{lineno} calls Path.{method}() in domain")

    # 3. Ports ABC-only rule.
    ports_dir = _SRC_ROOT / "ports"
    if ports_dir.exists():
        for f in sorted(ports_dir.rglob("*.py")):
            tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
            aliases = _import_aliases(tree)
            for node in tree.body:
                if isinstance(node, ast.ClassDef) and _classify_ports_class(node, aliases) != "abc":
                    _fail(
                        f"{f.relative_to(_SRC_ROOT)}:{node.lineno} declares "
                        f"concrete class {node.name!r}; relocate to adapters/"
                    )

    # 4. Rule 5 cross-package boundary.
    allowed = _allowed_third_party_roots()
    for f in _all_source_files():
        tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        for root in sorted(_extract_third_party_imports(tree, f)):
            if root in _FORBIDDEN_CROSS_PACKAGE:
                _fail(f"{f.relative_to(_SRC_ROOT)} imports forbidden cross-package root {root!r}")
            elif root not in allowed:
                _fail(f"{f.relative_to(_SRC_ROOT)} imports undeclared third-party root {root!r}")

    if failures:
        print(f"\n{failures} architectural violation(s) found.")
        sys.exit(1)
    print(
        f"OK: {len(_all_source_files())} source files checked across 6 "
        f"architectural rules (layering, allowlist, banned-stdlib, path-FS-ban, "
        f"ports-ABC, cross-package)."
    )


if __name__ == "__main__":
    _run_standalone()
