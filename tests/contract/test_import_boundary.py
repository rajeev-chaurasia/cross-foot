"""Pipeline packages must never see the generator or the eval manifest.

Walks the AST of every module under extraction, confidence, and reconcile.
Packages that do not exist yet contribute nothing, so this passes today.
"""

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"
PIPELINE_PACKAGES = ("extraction", "confidence", "reconcile")
FORBIDDEN_MODULES = ("crossfoot.generator", "crossfoot.models.manifest")


def _pipeline_modules() -> list[Path]:
    modules: list[Path] = []
    for package in PIPELINE_PACKAGES:
        package_dir = SRC_ROOT / "crossfoot" / package
        if package_dir.is_dir():
            modules.extend(sorted(package_dir.rglob("*.py")))
    return modules


def _package_parts(path: Path) -> tuple[str, ...]:
    # Both pkg/mod.py and pkg/__init__.py resolve relative imports against pkg.
    return path.relative_to(SRC_ROOT).with_suffix("").parts[:-1]


def _resolve_import_from(node: ast.ImportFrom, package_parts: tuple[str, ...]) -> str:
    module_parts = tuple(node.module.split(".")) if node.module else ()
    if node.level == 0:
        return ".".join(module_parts)
    keep = len(package_parts) - (node.level - 1)
    base = package_parts[:keep] if keep > 0 else ()
    return ".".join((*base, *module_parts))


def _is_forbidden(module: str) -> bool:
    return any(
        module == forbidden or module.startswith(forbidden + ".") for forbidden in FORBIDDEN_MODULES
    )


def test_pipeline_modules_do_not_import_generator_or_manifest() -> None:
    for path in _pipeline_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        package_parts = _package_parts(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not _is_forbidden(alias.name), f"{path}: import {alias.name}"
            elif isinstance(node, ast.ImportFrom):
                resolved = _resolve_import_from(node, package_parts)
                assert not _is_forbidden(resolved), f"{path}: from {resolved} import ..."
                for alias in node.names:
                    imported = f"{resolved}.{alias.name}" if resolved else alias.name
                    assert not _is_forbidden(imported), f"{path}: from-import {imported}"


def test_pipeline_modules_never_mention_manifest_json() -> None:
    for path in _pipeline_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert "manifest.json" not in node.value, f"{path}: literal {node.value!r}"
