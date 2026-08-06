# Phase 1 interface freeze

These interfaces are frozen. Contract tests are written against them before any
implementation exists, and implementations must pass those tests unedited. A change
to anything here routes through the maintainer and re-freezes.

## Models

All contracts are the pydantic models in `crossfoot.models` plus the enums, grammars,
and VIN tables in `crossfoot.constants`. Money is int cents everywhere. Statement truth
uses `StatementDoc`; the eval manifest wraps it in `ManifestRecord`.

## Determinism rules

- Generated IDs are deterministic slugs, never random: dealers `dlr-{oem}`,
  ledger entries `led-{schedule}-{seq:05d}`, documents
  `doc-{doc_type}-{dealer_id}-{yyyymm}-{seq:02d}`, discrepancies `dis-{doc_id}-{seq}`.
  Runtime records (runs, exceptions) may use ULIDs; dataset artifacts may not.
- Per-record seed = first 8 bytes of sha256(f"{master_seed}:{record_id}") as big-endian
  int. All randomness flows from `random.Random(seed)`; never the global `random`.
- The manifest contains no timestamps. `config_hash` = sha256 of the canonical JSON
  (sorted keys) of the generation parameters.
- Same master seed in, byte-identical `manifest.json` out, on every OS.

## Dataset layout

`crossfoot gen --seed INT --out DIR` writes:

- `DIR/manifest.json`: one `DatasetManifest` JSON document
- `DIR/files/{doc_id}.{ext}`: the rendered artifacts (`file_path` in each record is
  relative to DIR)

Mix per the plan: 220 files (60 parts, 60 warranty, 50 incentive, 40 floorplan across
clean_digital / scan_light / scan_heavy / csv / xlsx, plus 10 corrupted). Roughly 65%
of non-corrupted docs carry 1-3 injected discrepancies. Splits are document-level,
stratified by (doc_type, quality_tier), 50/25/25 train/calibration/test, assigned with
the seeded RNG; corrupted files get split=None.

## Generator API (`crossfoot.generator`)

- `ledger_gen.generate_ledger(master_seed: int) -> LedgerBook`
  3 dealers on different OEMs, 4 months of entries per schedule, valid ISO 3779 VINs,
  reference numbers matching `REF_GRAMMARS`.
- `compose.compose_statements(book: LedgerBook, master_seed: int) -> tuple[StatementDoc, ...]`
  Every line carries `source_entry_id`. Composer invariants: `subtotal_cents` equals the
  sum of line amounts and `crossfoot_delta_cents() == 0`.
- `discrepancy.inject(doc: StatementDoc, book: LedgerBook, seed: int) -> tuple[StatementDoc, tuple[InjectedDiscrepancy, ...]]`
  One injector per ExceptionType. After amount mutations the printed totals are
  re-crossfooted so the document stays internally consistent.
- `dataset.DatasetProfile(StrEnum)`: `FULL` (the 220-file mix) and `SMALL` (12 files for
  contract tests and CI fixtures: parts in clean_digital + scan_light + csv + xlsx,
  warranty in clean_digital + csv, incentive in clean_digital + xlsx, floorplan in
  clean_digital, plus truncated_pdf and empty_file corrupted).
- `dataset.DATASET_MIX: dict[DatasetProfile, dict[tuple[DocType, QualityTier], int]]`
  The planned counts as data, so distribution tests never render anything.
- `dataset.generate_dataset(master_seed: int, out_dir: Path, profile: DatasetProfile = DatasetProfile.FULL) -> DatasetManifest`
  Orchestrates ledger, compose, inject, render, degrade, corrupt, split, manifest.
  Also writes the ledger itself to `DIR/ledger.json` (a `LedgerBook`); the ledger is a
  legitimate pipeline input, the manifest is not.

## Template context (frozen)

Templates are Jinja2 HTML at `templates/{oem}/{doc_type}.html.j2`, self-contained
(inline CSS, system font stacks, no external assets or network fetches), print-clean
for US Letter through Chromium, paginating up to 3 pages of line items. All values
arrive pre-formatted as strings; templates place them and never compute. Variables:

- `marque_name`, `marque_tagline`, `marque_address`
- `dealer_name`, `dealer_code`, `dealer_address`
- `doc_title`, `statement_number`, `statement_date`, `period_start`, `period_end`
- `previous_balance`, `subtotal`, `adjustments`, `total` (currency strings; empty
  string when not applicable to the doc type)
- `lines`: list of objects with `line_no`, `line_date`, `claim_number`, `ro_number`,
  `vin`, `invoice_number`, `program_code`, `description`, `amount` (all strings,
  empty when absent); each doc type's template shows its relevant columns

Multiple template variants per (oem, doc_type) are welcome; `template_id` in the
manifest records which variant rendered each document. The renderer records every
printed string into `rendered_values`.

## Renderer API (`crossfoot.generator.renderers`)

- `base.Renderer` protocol: `render(doc: StatementDoc, template_id: str, seed: int, out_path: Path) -> dict[str, str]`
  returning the rendered_values map ("header:{field}" / "{line_no}:{field}" -> printed text).
- `chromium.ChromiumPdfRenderer`: Jinja2 HTML template per (oem, doc_type) family,
  printed via Playwright Chromium (sync API).
- `tabular.render_csv` / `tabular.render_xlsx`: messy header synonyms, mixed
  delimiters/encodings (cp1252 and utf-8), currency strings with $ and
  parentheses-negatives; xlsx with merged title cells and two-row headers.
- `degrade.degrade_to_scan(pdf_path: Path, profile: str, seed: int) -> None`
  pypdfium2 rasterize at 200 DPI, Augraphy profile `scan_light` or `scan_heavy`,
  reassemble IMAGE-ONLY via img2pdf (a contract test asserts zero extractable text).
- `corrupt.write_corrupted(kind: CorruptionKind, seed: int, out_path: Path) -> None`

## Eval API (`crossfoot.evals`)

- `metrics.field_is_correct(field: ExtractedField, truth: StatementDoc) -> bool | None`
  None when the truth doc has no value for that (line_no, name). Typed comparison per
  family: AMOUNT compares value_cents to the truth cents; DATE compares value_date;
  REFERENCE compares uppercased with separators ("-", " ", leading zeros) stripped;
  TEXT compares casefolded whitespace-collapsed strings.
- `metrics.raw_is_correct(field: ExtractedField, rendered: str | None) -> bool | None`
  Verbatim string equality against the manifest rendered_values entry; None when absent.
- `metrics.score_fields(docs: Sequence[ExtractedDocument], manifest: DatasetManifest, split: SplitName) -> tuple[FieldAccuracyCell, ...]`
- `metrics.score_recon(exceptions: Sequence[ExceptionRecord], manifest: DatasetManifest, split: SplitName, mode: ReconMode) -> tuple[ReconCell, ...]`
  A detection is a true positive when exception_type AND doc_id AND (line or ledger id)
  match an injected discrepancy.
- `runner.run_eval(dataset_dir: Path, split: SplitName) -> Scorecard`
  Phase 1 baseline: routes only clean CSV docs through the deterministic extractor,
  everything else counts as not-extracted. The number will be bad; it will be real.

## Baseline extractor (`crossfoot.extraction`)

- `tabular.extract_csv(path: Path, doc_id: str) -> ExtractedDocument`
  Encoding via charset-normalizer, delimiter via csv.Sniffer with fallbacks, header row
  detected by synonym match, amounts parsed from $, commas, parentheses-negatives.
  Deterministic fields get `validator_pass`/`grammar_match` signals and
  confidence 1.0 with status AUTO_ACCEPTED when validators pass; anything unparsed
  gets confidence 0.0 and NEEDS_REVIEW.

## Boundaries

- `crossfoot.extraction`, `crossfoot.confidence`, `crossfoot.reconcile` must not import
  `crossfoot.generator` or `crossfoot.models.manifest`, and must not open manifest.json.
  Enforced by a contract test that walks the AST of every module in those packages.
- Everything passes mypy strict and ruff with the repo config. No em-dashes anywhere.
