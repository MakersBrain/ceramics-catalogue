from __future__ import annotations

import ast
from pathlib import Path

CONNECTORS = Path(__file__).parents[1] / "src" / "mb_ceramics_catalogue" / "connectors"
TRANSPORTS = Path(__file__).parents[1] / "src" / "mb_ceramics_catalogue" / "transports"
FORBIDDEN_PREFIXES = (
    "mb_ceramics_catalogue.scrapers",
    "mb_ceramics_catalogue.datasets",
    "mb_ceramics_catalogue.ops",
    "mb_ceramics_catalogue.pipeline",
    "playwright",
    "camoufox",
)
ALLOWED_TRANSPORT_MODULES = frozenset({"mb_ceramics_catalogue.transports.browser"})
FORBIDDEN_TRANSPORT_PREFIXES = (
    "mb_ceramics_catalogue.cli",
    "mb_ceramics_catalogue.connectors",
    "mb_ceramics_catalogue.crawl",
    "mb_ceramics_catalogue.datasets",
    "mb_ceramics_catalogue.ops",
    "mb_ceramics_catalogue.scrapers",
)


def test_neutral_connectors_do_not_import_legacy_or_concrete_runtime_layers() -> None:
    violations: list[str] = []
    for path in sorted(CONNECTORS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            modules: tuple[str, ...] = ()
            if isinstance(node, ast.Import):
                modules = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = (node.module,)
            for module in modules:
                concrete_transport = module.startswith("mb_ceramics_catalogue.transports.") and all(
                    module != allowed and not module.startswith(allowed + ".")
                    for allowed in ALLOWED_TRANSPORT_MODULES
                )
                if concrete_transport or any(
                    module == forbidden or module.startswith(forbidden + ".")
                    for forbidden in FORBIDDEN_PREFIXES
                ):
                    violations.append(f"{path.name}:{getattr(node, 'lineno', 0)}: {module}")
    assert violations == []


def test_transports_do_not_import_concrete_higher_layers() -> None:
    violations: list[str] = []
    for path in sorted(TRANSPORTS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            modules: tuple[str, ...] = ()
            if isinstance(node, ast.Import):
                modules = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.level == 0:
                    modules = (node.module,)
                else:
                    package = ["mb_ceramics_catalogue", "transports"]
                    keep = max(0, len(package) - (node.level - 1))
                    modules = (".".join((*package[:keep], *node.module.split("."))),)
            for module in modules:
                if any(
                    module == forbidden or module.startswith(forbidden + ".")
                    for forbidden in FORBIDDEN_TRANSPORT_PREFIXES
                ):
                    violations.append(
                        f"{path.name}:{getattr(node, 'lineno', 0)}: {module}"
                    )
    assert violations == []
