"""Dataset orchestration: ledger, compose, inject, render, degrade, corrupt, split, manifest."""

import hashlib
import importlib
import json
import math
import random
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from crossfoot import __version__
from crossfoot.constants import CorruptionKind, DocType, QualityTier, SplitName
from crossfoot.generator.compose import compose_statements
from crossfoot.generator.discrepancy import inject
from crossfoot.generator.ledger_gen import generate_ledger, record_seed
from crossfoot.models.ledger import LedgerBook
from crossfoot.models.manifest import DatasetManifest, InjectedDiscrepancy, ManifestRecord
from crossfoot.models.statement import StatementDoc


class DatasetProfile(StrEnum):
    FULL = "full"
    SMALL = "small"


DATASET_MIX: dict[DatasetProfile, dict[tuple[DocType, QualityTier], int]] = {
    DatasetProfile.FULL: {
        (DocType.PARTS_STATEMENT, QualityTier.CLEAN_DIGITAL): 24,
        (DocType.PARTS_STATEMENT, QualityTier.SCAN_LIGHT): 14,
        (DocType.PARTS_STATEMENT, QualityTier.SCAN_HEAVY): 8,
        (DocType.PARTS_STATEMENT, QualityTier.CSV): 8,
        (DocType.PARTS_STATEMENT, QualityTier.XLSX): 6,
        (DocType.WARRANTY_CREDIT_MEMO, QualityTier.CLEAN_DIGITAL): 24,
        (DocType.WARRANTY_CREDIT_MEMO, QualityTier.SCAN_LIGHT): 16,
        (DocType.WARRANTY_CREDIT_MEMO, QualityTier.SCAN_HEAVY): 10,
        (DocType.WARRANTY_CREDIT_MEMO, QualityTier.CSV): 6,
        (DocType.WARRANTY_CREDIT_MEMO, QualityTier.XLSX): 4,
        (DocType.INCENTIVE_STATEMENT, QualityTier.CLEAN_DIGITAL): 20,
        (DocType.INCENTIVE_STATEMENT, QualityTier.SCAN_LIGHT): 12,
        (DocType.INCENTIVE_STATEMENT, QualityTier.SCAN_HEAVY): 8,
        (DocType.INCENTIVE_STATEMENT, QualityTier.CSV): 6,
        (DocType.INCENTIVE_STATEMENT, QualityTier.XLSX): 4,
        (DocType.FLOORPLAN_STATEMENT, QualityTier.CLEAN_DIGITAL): 16,
        (DocType.FLOORPLAN_STATEMENT, QualityTier.SCAN_LIGHT): 10,
        (DocType.FLOORPLAN_STATEMENT, QualityTier.SCAN_HEAVY): 6,
        (DocType.FLOORPLAN_STATEMENT, QualityTier.CSV): 5,
        (DocType.FLOORPLAN_STATEMENT, QualityTier.XLSX): 3,
    },
    DatasetProfile.SMALL: {
        (DocType.PARTS_STATEMENT, QualityTier.CLEAN_DIGITAL): 2,
        (DocType.PARTS_STATEMENT, QualityTier.SCAN_LIGHT): 1,
        (DocType.PARTS_STATEMENT, QualityTier.CSV): 1,
        (DocType.PARTS_STATEMENT, QualityTier.XLSX): 1,
        (DocType.WARRANTY_CREDIT_MEMO, QualityTier.CLEAN_DIGITAL): 1,
        (DocType.WARRANTY_CREDIT_MEMO, QualityTier.CSV): 1,
        (DocType.INCENTIVE_STATEMENT, QualityTier.CLEAN_DIGITAL): 1,
        (DocType.INCENTIVE_STATEMENT, QualityTier.XLSX): 1,
        (DocType.FLOORPLAN_STATEMENT, QualityTier.CLEAN_DIGITAL): 1,
    },
}

CORRUPTED_MIX: dict[DatasetProfile, dict[CorruptionKind, int]] = {
    DatasetProfile.FULL: {kind: 2 for kind in CorruptionKind},
    DatasetProfile.SMALL: {CorruptionKind.TRUNCATED_PDF: 1, CorruptionKind.EMPTY_FILE: 1},
}

INJECTION_RATE = 0.65
TRAIN_FRACTION = 0.5  # calibration and test split the remainder evenly

_FILES_DIR = "files"
_PDF_TIERS = frozenset({QualityTier.CLEAN_DIGITAL, QualityTier.SCAN_LIGHT, QualityTier.SCAN_HEAVY})

_TIER_EXTENSIONS: dict[QualityTier, str] = {
    QualityTier.CLEAN_DIGITAL: "pdf",
    QualityTier.SCAN_LIGHT: "pdf",
    QualityTier.SCAN_HEAVY: "pdf",
    QualityTier.CSV: "csv",
    QualityTier.XLSX: "xlsx",
}

_CORRUPTED_EXTENSIONS: dict[CorruptionKind, str] = {
    CorruptionKind.TRUNCATED_PDF: "pdf",
    CorruptionKind.WRONG_EXTENSION: "csv",  # non-CSV bytes behind a spreadsheet extension
    CorruptionKind.EMPTY_FILE: "pdf",
    CorruptionKind.ENCRYPTED_PDF: "pdf",
    CorruptionKind.BINARY_JUNK: "pdf",
}


class PdfRenderer(Protocol):
    """Structural stand-in for the frozen renderers.base.Renderer contract."""

    def render(
        self, doc: StatementDoc, template_id: str, seed: int, out_path: Path
    ) -> dict[str, str]: ...


TabularRenderFn = Callable[[StatementDoc, str, int, Path], dict[str, str]]
DegradeFn = Callable[[Path, str, int], None]
CorruptFn = Callable[[CorruptionKind, int, Path], None]


@dataclass(frozen=True)
class RenderHooks:
    """Seam for the rendering backends so planning stays unit-testable."""

    pdf_renderer_factory: Callable[[], AbstractContextManager[PdfRenderer]]
    render_csv: TabularRenderFn
    render_xlsx: TabularRenderFn
    degrade_to_scan: DegradeFn
    write_corrupted: CorruptFn


@dataclass(frozen=True)
class _PlannedDoc:
    doc: StatementDoc
    tier: QualityTier


@dataclass(frozen=True)
class _PreparedDoc:
    doc: StatementDoc
    tier: QualityTier
    injected: tuple[InjectedDiscrepancy, ...]


def generate_dataset(
    master_seed: int, out_dir: Path, profile: DatasetProfile = DatasetProfile.FULL
) -> DatasetManifest:
    """Generate the full synthetic dataset and write manifest.json plus ledger.json."""
    return _generate_dataset(master_seed, out_dir, profile, _default_hooks())


def _default_hooks() -> RenderHooks:
    # importlib keeps mypy off the renderer modules, which land from a parallel
    # workstream against the same frozen contract signatures.
    chromium = importlib.import_module("crossfoot.generator.renderers.chromium")
    tabular = importlib.import_module("crossfoot.generator.renderers.tabular")
    degrade = importlib.import_module("crossfoot.generator.degrade")
    corrupt = importlib.import_module("crossfoot.generator.corrupt")
    return RenderHooks(
        pdf_renderer_factory=chromium.ChromiumPdfRenderer,
        render_csv=tabular.render_csv,
        render_xlsx=tabular.render_xlsx,
        degrade_to_scan=degrade.degrade_to_scan,
        write_corrupted=corrupt.write_corrupted,
    )


def _generate_dataset(
    master_seed: int, out_dir: Path, profile: DatasetProfile, hooks: RenderHooks
) -> DatasetManifest:
    book = generate_ledger(master_seed)
    statements = compose_statements(book, master_seed)
    prepared = [
        _prepare_document(planned, book, master_seed)
        for planned in _plan_documents(statements, master_seed, profile)
    ]
    splits = _assign_splits(prepared, master_seed)

    files_dir = out_dir / _FILES_DIR
    files_dir.mkdir(parents=True, exist_ok=True)
    records: list[ManifestRecord] = []
    # One browser serves every PDF in the run.
    with hooks.pdf_renderer_factory() as pdf_renderer:
        for item in prepared:
            records.append(
                _render_document(
                    item, pdf_renderer, hooks, files_dir, splits[item.doc.doc_id], master_seed
                )
            )
    records.extend(_write_corrupted_files(profile, master_seed, files_dir, hooks))

    manifest = DatasetManifest(
        master_seed=master_seed,
        generator_version=__version__,
        config_hash=_config_hash(master_seed, profile),
        records=tuple(records),
    )
    _write_json(out_dir / "ledger.json", book)
    _write_json(out_dir / "manifest.json", manifest)
    return manifest


def _plan_documents(
    statements: Sequence[StatementDoc], master_seed: int, profile: DatasetProfile
) -> list[_PlannedDoc]:
    """Deterministically map composed statements onto the (doc_type, tier) slots."""
    mix = DATASET_MIX[profile]
    by_type: dict[DocType, list[StatementDoc]] = {doc_type: [] for doc_type in DocType}
    for doc in statements:
        by_type[doc.doc_type].append(doc)

    planned: list[_PlannedDoc] = []
    for doc_type in DocType:
        pool = list(by_type[doc_type])
        if not pool:
            continue
        rng = random.Random(record_seed(master_seed, f"assign:{doc_type}"))
        rng.shuffle(pool)
        tiers = [
            tier
            for tier in QualityTier
            if (doc_type, tier) in mix
            for _ in range(mix[(doc_type, tier)])
        ]
        uses: dict[str, int] = {}
        for index, tier in enumerate(tiers):
            base = pool[index % len(pool)]
            occurrence = uses.get(base.doc_id, 0) + 1
            uses[base.doc_id] = occurrence
            doc = base if occurrence == 1 else _with_sequence(base, occurrence)
            planned.append(_PlannedDoc(doc=doc, tier=tier))
    return planned


def _with_sequence(doc: StatementDoc, seq: int) -> StatementDoc:
    # A statement reused for another slot becomes a sibling document: same truth
    # content, next doc_id sequence, so file names and per-doc seeds stay unique.
    return doc.model_copy(update={"doc_id": f"{doc.doc_id[:-2]}{seq:02d}"})


def _prepare_document(planned: _PlannedDoc, book: LedgerBook, master_seed: int) -> _PreparedDoc:
    decision_rng = random.Random(record_seed(master_seed, f"inject:{planned.doc.doc_id}"))
    if decision_rng.random() >= INJECTION_RATE:
        return _PreparedDoc(doc=planned.doc, tier=planned.tier, injected=())
    doc, injected = inject(
        planned.doc, book, record_seed(master_seed, f"discrepancy:{planned.doc.doc_id}")
    )
    return _PreparedDoc(doc=doc, tier=planned.tier, injected=injected)


def _assign_splits(prepared: Sequence[_PreparedDoc], master_seed: int) -> dict[str, SplitName]:
    """50/25/25 train/calibration/test, stratified per (doc_type, tier) cell."""
    cells: dict[tuple[DocType, QualityTier], list[str]] = {}
    for item in prepared:
        cells.setdefault((item.doc.doc_type, item.tier), []).append(item.doc.doc_id)

    splits: dict[str, SplitName] = {}
    for doc_type in DocType:
        for tier in QualityTier:
            doc_ids = cells.get((doc_type, tier))
            if not doc_ids:
                continue
            rng = random.Random(record_seed(master_seed, f"split:{doc_type}:{tier}"))
            shuffled = list(doc_ids)
            rng.shuffle(shuffled)
            n_train = math.ceil(len(shuffled) * TRAIN_FRACTION)
            n_calibration = math.ceil((len(shuffled) - n_train) / 2)
            for index, doc_id in enumerate(shuffled):
                if index < n_train:
                    splits[doc_id] = SplitName.TRAIN
                elif index < n_train + n_calibration:
                    splits[doc_id] = SplitName.CALIBRATION
                else:
                    splits[doc_id] = SplitName.TEST
    return splits


def _render_document(
    item: _PreparedDoc,
    pdf_renderer: PdfRenderer,
    hooks: RenderHooks,
    files_dir: Path,
    split: SplitName,
    master_seed: int,
) -> ManifestRecord:
    doc = item.doc
    seed = record_seed(master_seed, doc.doc_id)
    template_id = f"{doc.oem}-{doc.doc_type}-v1"
    extension = _TIER_EXTENSIONS[item.tier]
    out_path = files_dir / f"{doc.doc_id}.{extension}"
    augraphy_profile: str | None = None
    if item.tier in _PDF_TIERS:
        rendered = pdf_renderer.render(doc, template_id, seed, out_path)
        if item.tier is not QualityTier.CLEAN_DIGITAL:
            hooks.degrade_to_scan(out_path, item.tier.value, seed)
            augraphy_profile = item.tier.value
    elif item.tier is QualityTier.CSV:
        rendered = hooks.render_csv(doc, template_id, seed, out_path)
    else:
        rendered = hooks.render_xlsx(doc, template_id, seed, out_path)
    return ManifestRecord(
        doc_id=doc.doc_id,
        file_path=f"{_FILES_DIR}/{doc.doc_id}.{extension}",
        quality_tier=item.tier,
        template_id=template_id,
        render_seed=seed,
        augraphy_profile=augraphy_profile,
        corruption=None,
        truth=doc,
        rendered_values=rendered,
        injected=item.injected,
        split=split,
    )


def _write_corrupted_files(
    profile: DatasetProfile, master_seed: int, files_dir: Path, hooks: RenderHooks
) -> list[ManifestRecord]:
    counts = CORRUPTED_MIX[profile]
    records: list[ManifestRecord] = []
    for kind in CorruptionKind:
        for ordinal in range(1, counts.get(kind, 0) + 1):
            doc_id = f"doc-corrupted-{kind}-{ordinal:02d}"
            seed = record_seed(master_seed, doc_id)
            extension = _CORRUPTED_EXTENSIONS[kind]
            hooks.write_corrupted(kind, seed, files_dir / f"{doc_id}.{extension}")
            records.append(
                ManifestRecord(
                    doc_id=doc_id,
                    file_path=f"{_FILES_DIR}/{doc_id}.{extension}",
                    quality_tier=QualityTier.CORRUPTED,
                    template_id=f"corrupted-{kind}-v1",
                    render_seed=seed,
                    augraphy_profile=None,
                    corruption=kind,
                    truth=None,
                    rendered_values={},
                    injected=(),
                    split=None,
                )
            )
    return records


def _config_hash(master_seed: int, profile: DatasetProfile) -> str:
    mix = sorted(
        [doc_type.value, tier.value, str(count)]
        for (doc_type, tier), count in DATASET_MIX[profile].items()
    )
    parameters = {
        "generator_version": __version__,
        "master_seed": master_seed,
        "mix": mix,
        "profile": profile.value,
    }
    return hashlib.sha256(json.dumps(parameters, sort_keys=True).encode("utf-8")).hexdigest()


def _write_json(path: Path, model: BaseModel) -> None:
    # write_bytes keeps line endings byte-identical across operating systems.
    path.write_bytes(model.model_dump_json(indent=2).encode("utf-8"))
