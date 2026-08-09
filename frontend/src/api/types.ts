/**
 * Wire types for the crossfoot review surface.
 *
 * docs/contracts-phase3.md says these are "generated from the OpenAPI snapshot,
 * never hand written", and the same document says the snapshot "cannot be
 * generated until crossfoot.api imports". That module does not exist in the
 * repository yet, so there is no schema to generate from. Everything below is
 * transcribed by hand from the frozen route list in the contract, the pinned
 * response shapes in tests/contract/test_api_review.py,
 * tests/contract/test_api_exceptions.py and
 * tests/contract/test_api_stats_and_metrics.py, and the frozen pydantic models
 * in src/crossfoot/models/. No field appears here that those sources do not
 * name. Regenerate this file from the snapshot the first time the API lands and
 * the snapshot is committed; a diff at that point is a real contract break.
 *
 * Money crosses the wire as integer cents and LLM cost as integer microusd.
 * Nothing in this file is a float dollar amount.
 */

// Enumerations, mirroring src/crossfoot/constants.py. Written as string unions
// rather than TypeScript enums because the build runs with erasableSyntaxOnly.

export type FieldFamily = 'amount' | 'date' | 'reference' | 'text'

export type FieldName =
  | 'statement_number'
  | 'statement_date'
  | 'total'
  | 'subtotal'
  | 'previous_balance'
  | 'claim_number'
  | 'ro_number'
  | 'vin'
  | 'invoice_number'
  | 'program_code'
  | 'line_date'
  | 'line_amount'
  | 'description'

export type QualityTier =
  | 'clean_digital'
  | 'scan_light'
  | 'scan_heavy'
  | 'csv'
  | 'xlsx'
  | 'corrupted'

export type ReviewStatus =
  | 'auto_accepted'
  | 'needs_review'
  | 'human_accepted'
  | 'human_corrected'

export type ExtractionRoute =
  | 'digital_pdf'
  | 'scanned_pdf'
  | 'csv'
  | 'xlsx'
  | 'unprocessable'

export type DocType =
  | 'parts_statement'
  | 'warranty_credit_memo'
  | 'floorplan_statement'
  | 'incentive_statement'

export type SplitName = 'train' | 'calibration' | 'test'

export type ExceptionType =
  | 'missing_from_ledger'
  | 'missing_from_statement'
  | 'amount_mismatch'
  | 'duplicate'
  | 'short_pay'
  | 'timing_difference'

export type ExceptionStatus = 'open' | 'resolved'

export type Provider = 'custom' | 'gemini' | 'groq' | 'openrouter' | 'mistral'

export type ReconMode = 'end_to_end' | 'oracle'

export const FIELD_FAMILIES: readonly FieldFamily[] = [
  'amount',
  'date',
  'reference',
  'text',
]

export const REVIEW_STATUSES: readonly ReviewStatus[] = [
  'needs_review',
  'auto_accepted',
  'human_accepted',
  'human_corrected',
]

export const QUALITY_TIERS: readonly QualityTier[] = [
  'clean_digital',
  'scan_light',
  'scan_heavy',
  'csv',
  'xlsx',
  'corrupted',
]

export const EXCEPTION_TYPES: readonly ExceptionType[] = [
  'missing_from_ledger',
  'missing_from_statement',
  'amount_mismatch',
  'duplicate',
  'short_pay',
  'timing_difference',
]

export const EXCEPTION_STATUSES: readonly ExceptionStatus[] = ['open', 'resolved']

// GET /api/stats/summary

export interface StatsSummary {
  documents_processed: number
  fields_extracted: number
  auto_accept_rate: number
  review_queue_depth: number
  open_exception_count: number
  gross_dollars_at_risk_cents: number
  cost_per_document_microusd: number
}

// GET /api/review/queue and GET /api/review/items/{field_id}

/** The raw evidence feeding the confidence model, as FieldSignals stores it. */
export interface FieldSignals {
  self_consistency: number | null
  det_llm_agreement: number | null
  validator_pass: number | null
  grammar_match: number | null
  crossfoot_ok: number | null
  crossfoot_residual_suspect: boolean
  char_ambiguity: number
  /** The extractor the router picked from the file's bytes. Null when unrouted. */
  route: ExtractionRoute | null
}

export interface ReviewItem {
  field_id: string
  doc_id: string
  line_no: number | null
  name: FieldName
  family: FieldFamily
  raw_text: string | null
  value: string | null
  confidence: number
  status: ReviewStatus
  crop_url: string
}

/** The document context the detail route returns alongside an item. */
export interface DocumentContext {
  doc_id: string
  doc_type: DocType | null
  quality_tier: QualityTier
  route: ExtractionRoute
  split: SplitName | null
}

export interface ReviewItemDetail extends ReviewItem {
  signals: FieldSignals
  document: DocumentContext
  neighbors: ReviewItem[]
}

export interface ReviewQueueResponse {
  items: ReviewItem[]
  total: number
}

export interface ReviewQueueParams {
  status?: ReviewStatus
  family?: FieldFamily
  tier?: QualityTier
  limit?: number
  offset?: number
}

export interface CorrectionRequest {
  value: string
  reviewer: string
}

// GET /api/exceptions and POST /api/exceptions/{exception_id}/resolve

export interface ExceptionRecord {
  exception_id: string
  run_id: string
  exception_type: ExceptionType
  doc_id: string | null
  statement_line_no: number | null
  ledger_entry_id: string | null
  match_key: string | null
  statement_amount_cents: number | null
  ledger_amount_cents: number | null
  dollar_impact_cents: number
  memo_amount_cents: number
  explanation: string
  status: ExceptionStatus
  detected_at: string
}

export interface ExceptionListResponse {
  items: ExceptionRecord[]
  total: number
}

export interface ExceptionListParams {
  type?: ExceptionType
  status?: ExceptionStatus
  min_impact_cents?: number
}

export interface ResolutionRequest {
  resolution: string
}

// GET /api/metrics

export interface FieldAccuracyCell {
  field_family: FieldFamily
  quality_tier: QualityTier
  // Optional because scorecards committed before these two counters existed do
  // not carry them; the model defaults both to zero.
  fields_in_truth?: number
  fields_spurious?: number
  fields_expected: number
  fields_extracted: number
  correct_canonical: number
  correct_raw: number
}

export interface CalibrationBin {
  field_family: FieldFamily
  mean_confidence: number
  empirical_accuracy: number
  count: number
}

export interface ThresholdPoint {
  field_family: FieldFamily
  threshold: number
  auto_accept_precision: number
  review_rate: number
}

export interface ReconCell {
  mode: ReconMode
  exception_type: ExceptionType
  injected: number
  detected_true: number
  detected_false: number
  injected_dollar_cents: number
  caught_dollar_cents: number
}

export interface CostCell {
  provider: Provider
  quality_tier: QualityTier | null
  calls: number
  prompt_tokens: number
  completion_tokens: number
  list_price_microusd: number
}

export interface Scorecard {
  run_id: string
  created_at: string
  git_sha: string
  dataset_config_hash: string
  master_seed: number
  split: SplitName
  models_used: string[]
  documents_total: number
  documents_processed: number
  documents_unprocessable: number
  field_accuracy: FieldAccuracyCell[]
  calibration: CalibrationBin[]
  threshold_sweep: ThresholdPoint[]
  reconciliation: ReconCell[]
  costs: CostCell[]
  notes: string
}

export interface MetricsResponse {
  scorecard: Scorecard
  calibration: CalibrationBin[]
  threshold_sweep: ThresholdPoint[]
}
