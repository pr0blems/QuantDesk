from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "quantdesk_v2"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    return imported


def _python_files(package: str) -> list[Path]:
    return sorted((PACKAGE_ROOT / package).rglob("*.py"))


def test_domain_layer_has_no_framework_or_outer_layer_dependencies() -> None:
    forbidden = (
        "fastapi",
        "sqlalchemy",
        "quantdesk_v2.application",
        "quantdesk_v2.infrastructure",
        "quantdesk_v2.interfaces",
    )

    violations = {
        str(path.relative_to(PACKAGE_ROOT)): sorted(
            name for name in _imports(path) if name.startswith(forbidden)
        )
        for path in _python_files("domain")
    }

    assert not {path: names for path, names in violations.items() if names}


def test_application_layer_does_not_depend_on_frameworks_or_adapters() -> None:
    forbidden = (
        "fastapi",
        "sqlalchemy",
        "quantdesk_v2.infrastructure",
        "quantdesk_v2.interfaces",
    )

    violations = {
        str(path.relative_to(PACKAGE_ROOT)): sorted(
            name for name in _imports(path) if name.startswith(forbidden)
        )
        for path in _python_files("application")
    }

    assert not {path: names for path, names in violations.items() if names}


def test_paper_runtime_does_not_reach_into_backtest_private_helpers() -> None:
    path = PACKAGE_ROOT / "paper_engine.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    private_imports: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        if module in {"backtest", "quantdesk_v2.backtest"} or module.endswith(".backtest"):
            private_imports.extend(alias.name for alias in node.names if alias.name.startswith("_"))

    assert private_imports == []


def test_shadow_worker_does_not_import_the_paper_runtime() -> None:
    imports = _imports(PACKAGE_ROOT / "shadow_worker.py")

    assert "paper_engine" not in imports
    assert "quantdesk_v2.paper_engine" not in imports

