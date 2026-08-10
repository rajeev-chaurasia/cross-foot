"""The baseline runner must treat every manifest path as untrusted input.

It must also name the models that served the run it scores, which is a fact only
the cost ledger holds. A scorecard publishing an empty `models_used` says it has
no record; it does not say no model ran, and nothing here may let the two blur.
"""

from datetime import date
from pathlib import Path

from crossfoot.constants import DocType, LineType, Oem, Provider, QualityTier, SplitName
from crossfoot.costs.ledger import CostLedger, Purpose
from crossfoot.evals.runner import run_eval
from crossfoot.generator.ledger_gen import generate_ledger
from crossfoot.ingest_db import extraction_run_id
from crossfoot.models.manifest import DatasetManifest, ManifestRecord
from crossfoot.models.statement import StatementDoc, StatementLine

SECRET_MARKER = "9999.99"
GOOD_CSV = "Claim Number,Date,Description,Amount\nNS12345678,07/15/2026,Alpha brake kit,123.45\n"
SECRET_CSV = f"Claim Number,Date,Description,Amount\nNS87654321,07/15/2026,Secret,{SECRET_MARKER}\n"


def _truth(doc_id: str) -> StatementDoc:
    line = StatementLine(
        line_no=1,
        line_type=LineType.CHARGE,
        claim_number="NS12345678",
        line_date=date(2026, 7, 15),
        description="Alpha brake kit",
        amount_cents=12_345,
    )
    return StatementDoc(
        doc_id=doc_id,
        dealer_id="dlr-northstar",
        doc_type=DocType.WARRANTY_CREDIT_MEMO,
        oem=Oem.NORTHSTAR,
        statement_number="STMT-202607-01",
        statement_date=date(2026, 7, 31),
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        subtotal_cents=12_345,
        total_cents=12_345,
        lines=(line,),
    )


# The amended scoring rule expects only what the artifact printed, so a record
# with no rendered values would score no cells at all.
RENDERED = {
    "1:claim_number": "NS12345678",
    "1:line_date": "07/15/2026",
    "1:description": "Alpha brake kit",
    "1:line_amount": "123.45",
}


def _record(doc_id: str, file_path: str) -> ManifestRecord:
    return ManifestRecord(
        doc_id=doc_id,
        file_path=file_path,
        quality_tier=QualityTier.CSV,
        template_id="northstar-warranty_credit_memo-csv-v1",
        render_seed=1,
        truth=_truth(doc_id),
        rendered_values=RENDERED,
        split=SplitName.TRAIN,
    )


def _build_dataset(tmp_path: Path, hostile_paths: tuple[str, ...]) -> Path:
    """Dataset dir with one legitimate CSV plus records pointing outside it."""
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "secret.csv").write_text(SECRET_CSV, encoding="utf-8")
    dataset_dir = tmp_path / "dataset"
    (dataset_dir / "files").mkdir(parents=True)
    (dataset_dir / "files" / "good.csv").write_text(GOOD_CSV, encoding="utf-8")
    records = [_record("doc-good-01", "files/good.csv")]
    records.extend(
        _record(f"doc-hostile-{index:02d}", path)
        for index, path in enumerate(hostile_paths, start=1)
    )
    manifest = DatasetManifest(
        master_seed=1,
        generator_version="0.1.0",
        config_hash="0" * 64,
        records=tuple(records),
    )
    (dataset_dir / "manifest.json").write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    (dataset_dir / "ledger.json").write_text(
        generate_ledger(1).model_dump_json(indent=2), encoding="utf-8"
    )
    return dataset_dir


def test_run_eval_refuses_paths_outside_the_dataset(tmp_path: Path) -> None:
    hostile = (
        "../outside/secret.csv",
        "files/../../outside/secret.csv",
        str(tmp_path / "outside" / "secret.csv"),
        f"//./{(tmp_path / 'outside' / 'secret.csv').as_posix()}",
    )
    dataset_dir = _build_dataset(tmp_path, hostile)
    scorecard = run_eval(dataset_dir, SplitName.TRAIN, cost_db=tmp_path / "absent.db")
    assert scorecard.documents_processed == 1
    assert scorecard.documents_unprocessable == len(hostile)


CONFIG_HASH = "0" * 64


def _ledger_call(ledger: CostLedger, *, run_id: str, model: str) -> None:
    """One recorded attempt, with the fields the model name is the point of."""
    ledger.record(
        run_id=run_id,
        doc_id="doc-good-01",
        purpose=Purpose.EXTRACT,
        provider=Provider.GEMINI,
        model=model,
        prompt_tokens=100,
        completion_tokens=10,
        total_tokens=110,
        cached=False,
        latency_ms=500,
        http_status=200,
        attempt=1,
    )


def test_the_scorecard_names_every_model_that_served_the_run(tmp_path: Path) -> None:
    dataset_dir = _build_dataset(tmp_path, ())
    cost_db = tmp_path / "costs.db"
    ledger = CostLedger(cost_db)
    scored_run = extraction_run_id(SplitName.TRAIN, CONFIG_HASH)
    _ledger_call(ledger, run_id=scored_run, model="qwen2.5vl:7b")
    # Two attempts on one model must not name it twice.
    _ledger_call(ledger, run_id=scored_run, model="qwen2.5vl:7b")
    _ledger_call(ledger, run_id=scored_run, model="gemini-3.5-flash")
    ledger.close()

    scorecard = run_eval(dataset_dir, SplitName.TRAIN, cost_db=cost_db)
    assert scorecard.models_used == ("gemini-3.5-flash", "qwen2.5vl:7b")


def test_the_scorecard_names_no_model_another_split_called(tmp_path: Path) -> None:
    dataset_dir = _build_dataset(tmp_path, ())
    cost_db = tmp_path / "costs.db"
    ledger = CostLedger(cost_db)
    _ledger_call(
        ledger, run_id=extraction_run_id(SplitName.TRAIN, CONFIG_HASH), model="qwen2.5vl:7b"
    )
    _ledger_call(
        ledger, run_id=extraction_run_id(SplitName.TEST, CONFIG_HASH), model="gemini-3.5-flash"
    )
    ledger.close()

    scorecard = run_eval(dataset_dir, SplitName.TRAIN, cost_db=cost_db)
    assert scorecard.models_used == ("qwen2.5vl:7b",)


def test_a_run_with_no_ledger_publishes_no_models_rather_than_guessing(tmp_path: Path) -> None:
    dataset_dir = _build_dataset(tmp_path, ())
    scorecard = run_eval(dataset_dir, SplitName.TRAIN, cost_db=tmp_path / "never-written.db")
    # Empty is "no record", which is the only thing an absent ledger supports.
    assert scorecard.models_used == ()


def test_run_eval_still_scores_the_legitimate_document(tmp_path: Path) -> None:
    dataset_dir = _build_dataset(tmp_path, ("../outside/secret.csv",))
    scorecard = run_eval(dataset_dir, SplitName.TRAIN, cost_db=tmp_path / "absent.db")
    assert scorecard.documents_total == 2
    assert any(cell.correct_canonical > 0 for cell in scorecard.field_accuracy)
