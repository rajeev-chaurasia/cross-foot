"""No module that scores a document may read the generator's answer key.

`tests/contract/test_import_boundary.py` guards three packages by name:
`extraction`, `confidence`, `reconcile`. That net had a hole exactly the shape of
`crossfoot/scoring.py`, which sits at package top level, imported `ManifestRecord`,
and built every confidence feature in the review database the API and the UI read
out of the dataset manifest. An adversarial audit found it by measuring what the
leak was worth rather than by reading an import list, so this file inverts the
rule: the whole package is guarded, and the handful of modules whose job is the
dataset are named one at a time with the reason each is allowed.

The second half pins the feature surface itself. A guard on imports would not
have caught the leak coming back through a field added to `FieldSignals` or to
`SignalContext`, so both are spelled out here and any addition to either has to
be a deliberate edit to this file.
"""

import ast
from pathlib import Path

from crossfoot.confidence.signals import SignalContext
from crossfoot.models.extraction import FieldSignals

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"
PACKAGE_ROOT = SRC_ROOT / "crossfoot"

FORBIDDEN_MODULES = ("crossfoot.generator", "crossfoot.models.manifest")
FORBIDDEN_LITERAL = "manifest.json"

# Directories whose whole purpose is the dataset: one writes the answer key, the
# other scores against it. Neither is on the path a document takes to a score.
EXEMPT_DIRECTORIES = ("crossfoot/generator/", "crossfoot/evals/")

# Individual modules allowed to read the answer key, each for a reason that is
# about building or measuring a dataset rather than about judging a document.
# Adding a line here is the visible act this test exists to force.
EXEMPT_MODULES = {
    # It is the answer key.
    "crossfoot/models/manifest.py",
    # The eval-side builder for the demo database. It reads truth to produce
    # `scoring.FieldLabel` rows and the documents table's split and tier columns,
    # and hands `apply_confidence` labels only; see `ingest_db._labels`.
    "crossfoot/ingest_db.py",
    # The operator entry point. `crossfoot generate` and `crossfoot eval` are
    # dataset commands, so this module drives both sides by construction.
    "crossfoot/cli.py",
}

# A floor, so nobody can empty the net by moving code somewhere it is not walked.
MIN_GUARDED_MODULES = 40

# The complete feature surface. Every name is computable from the artifact and
# the extraction; nothing here may be a fact only the generator holds.
EXPECTED_SIGNAL_FIELDS = {
    "self_consistency",
    "det_llm_agreement",
    "validator_pass",
    "grammar_match",
    "crossfoot_ok",
    "crossfoot_residual_suspect",
    "char_ambiguity",
    "route",
}

# Everything `attach_signals` may be told that it cannot see for itself: two
# upstream measurements the extractor made while reading the page.
EXPECTED_CONTEXT_FIELDS = {"self_consistency", "det_llm_agreement"}


def _relative(path: Path) -> str:
    return path.relative_to(SRC_ROOT).as_posix()


def _is_exempt(relative: str) -> bool:
    return relative in EXEMPT_MODULES or relative.startswith(EXEMPT_DIRECTORIES)


def guarded_modules() -> list[Path]:
    """Every module under src/crossfoot except the named dataset-side exemptions."""
    return [path for path in sorted(PACKAGE_ROOT.rglob("*.py")) if not _is_exempt(_relative(path))]


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


def test_scoring_is_inside_the_net() -> None:
    """The module the audit caught. It is the reason this file exists."""
    assert "crossfoot/scoring.py" in {_relative(path) for path in guarded_modules()}


def test_the_net_covers_the_package() -> None:
    guarded = {_relative(path) for path in guarded_modules()}
    assert len(guarded) >= MIN_GUARDED_MODULES
    # A spot check across the layers a score travels through, so a future
    # reorganization that moves one of them out of the net fails loudly.
    for expected in (
        "crossfoot/confidence/signals.py",
        "crossfoot/confidence/scorer.py",
        "crossfoot/extraction/router.py",
        "crossfoot/api/dto.py",
        "crossfoot/db/review.py",
        "crossfoot/reconcile/engine.py",
    ):
        assert expected in guarded


def test_every_exemption_names_a_module_that_exists() -> None:
    for relative in EXEMPT_MODULES:
        assert (SRC_ROOT / relative).is_file(), relative


def test_guarded_modules_do_not_import_the_generator_or_the_manifest() -> None:
    for path in guarded_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        package_parts = _package_parts(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not _is_forbidden(alias.name), f"{_relative(path)}: import {alias.name}"
            elif isinstance(node, ast.ImportFrom):
                resolved = _resolve_import_from(node, package_parts)
                assert not _is_forbidden(resolved), f"{_relative(path)}: from {resolved} import ..."
                for alias in node.names:
                    imported = f"{resolved}.{alias.name}" if resolved else alias.name
                    assert not _is_forbidden(imported), f"{_relative(path)}: {imported}"


def test_guarded_modules_never_mention_manifest_json() -> None:
    for path in guarded_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert FORBIDDEN_LITERAL not in node.value, f"{_relative(path)}: {node.value!r}"


def test_field_signals_carry_only_artifact_derived_evidence() -> None:
    """`quality_tier` lived here and one-hot encoded a generator label as a feature."""
    assert set(FieldSignals.model_fields) == EXPECTED_SIGNAL_FIELDS


def test_signal_context_carries_only_upstream_measurements() -> None:
    """It used to carry the true marque, period, tier, and per-line types."""
    assert set(SignalContext.__dataclass_fields__) == EXPECTED_CONTEXT_FIELDS
