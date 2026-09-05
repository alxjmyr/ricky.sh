"""Keep the documented interface dependency direction executable."""

from __future__ import annotations

import ast
from importlib.util import resolve_name
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1] / "src" / "ricky"


def _imports(path: Path) -> list[tuple[int, str]]:
    package = ".".join(("ricky", *path.relative_to(_ROOT).parts[:-1]))
    imports: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imports.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                module = resolve_name("." * node.level + module, package)
            imports.append((node.lineno, module))
            imports.extend((node.lineno, f"{module}.{alias.name}") for alias in node.names)
    return imports


@pytest.mark.parametrize(
    ("area", "excluded", "forbidden"),
    [
        (".", "interfaces", "ricky.interfaces"),
        ("interfaces/cli", "interfaces/cli/app.py", "ricky.interfaces.cli.app"),
    ],
    ids=["core-does-not-import-interfaces", "commands-do-not-import-composition-root"],
)
def test_interface_dependencies_point_outward(area: str, excluded: str, forbidden: str) -> None:
    violations: list[str] = []
    excluded_path = _ROOT / excluded
    for path in sorted((_ROOT / area).rglob("*.py")):
        if path == excluded_path or path.is_relative_to(excluded_path):
            continue
        for line, imported in _imports(path):
            if imported == forbidden or imported.startswith(f"{forbidden}."):
                violations.append(f"{path.relative_to(_ROOT)}:{line}: {imported}")
    assert not violations, "Interface dependency violations:\n" + "\n".join(violations)
