"""The platform core must stay free of MCP and web-framework imports."""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "mcp_tools"
FORBIDDEN_IN_CORE = {"mcp", "mcp_types", "starlette", "uvicorn"}
CORE_PACKAGES = ("core", "db", "blobs", "providers")


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found


def test_core_does_not_import_mcp_or_starlette() -> None:
    offenders = []
    for package in CORE_PACKAGES:
        for path in (SRC / package).rglob("*.py"):
            bad = _imports(path) & FORBIDDEN_IN_CORE
            if bad:
                offenders.append(f"{path.relative_to(SRC)}: {sorted(bad)}")
    assert not offenders, offenders


def test_core_does_not_import_the_server_layer() -> None:
    offenders = []
    for package in CORE_PACKAGES:
        for path in (SRC / package).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.ImportFrom)
                    and node.module
                    and node.module.startswith("mcp_tools.server")
                ):
                    offenders.append(str(path.relative_to(SRC)))
    assert not offenders, offenders
