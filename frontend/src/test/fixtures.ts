/**
 * Payloads shaped exactly like the ones tests/contract/ pins.
 *
 * The field names, the enum spellings and the money units are the contract's.
 * The values are chosen to make the product claim legible: 490 of 2500 fields
 * in the queue is the 19.6 percent the demo is about.
 */

import type {
  ExceptionListResponse,
  MetricsResponse,
  ReviewItem,
  ReviewItemDetail,
  ReviewQueueResponse,
  StatsSummary,
} from '../api/types'

export const SUMMARY: StatsSummary = {
  documents_processed: 105,
  fields_extracted: 2500,
  auto_accept_rate: 0.804,
  review_queue_depth: 490,
  open_exception_count: 4,
  gross_dollars_at_risk_cents: 367_000,
  cost_per_document_microusd: 45_000,
}

export const VIN_ITEM: ReviewItem = {
  field_id: 'fld-a-0001',
  doc_id: 'doc-a',
  line_no: 1,
  name: 'vin',
  family: 'reference',
  raw_text: '1G1ZT53826F1O9149',
  value: '1G1ZT53826F1O9149',
  confidence: 0.2,
  status: 'needs_review',
  crop_url: '/api/crops/doc-a/fld-a-0001.png',
}

export const AMOUNT_ITEM: ReviewItem = {
  field_id: 'fld-a-0002',
  doc_id: 'doc-a',
  line_no: 1,
  name: 'line_amount',
  family: 'amount',
  raw_text: '$1,234.56',
  value: '1234.56',
  confidence: 0.2,
  status: 'needs_review',
  crop_url: '/api/crops/doc-a/fld-a-0002.png',
}

export const CLAIM_ITEM: ReviewItem = {
  field_id: 'fld-b-0001',
  doc_id: 'doc-b',
  line_no: 1,
  name: 'claim_number',
  family: 'reference',
  raw_text: 'NS12345678',
  value: 'NS12345678',
  confidence: 0.31,
  status: 'needs_review',
  crop_url: '/api/crops/doc-b/fld-b-0001.png',
}

export const QUEUE: ReviewQueueResponse = {
  items: [VIN_ITEM, AMOUNT_ITEM, CLAIM_ITEM],
  total: 3,
}

export const VIN_DETAIL: ReviewItemDetail = {
  ...VIN_ITEM,
  signals: {
    self_consistency: 0.5,
    det_llm_agreement: null,
    validator_pass: 0,
    grammar_match: 0,
    crossfoot_ok: null,
    crossfoot_residual_suspect: false,
    char_ambiguity: 0.35,
    quality_tier: 'scan_heavy',
  },
  document: {
    doc_id: 'doc-a',
    doc_type: 'parts_statement',
    quality_tier: 'scan_heavy',
    route: 'scanned_pdf',
    split: 'test',
  },
  neighbors: [AMOUNT_ITEM],
}

export const AMOUNT_DETAIL: ReviewItemDetail = {
  ...AMOUNT_ITEM,
  signals: {
    self_consistency: 0.5,
    det_llm_agreement: null,
    validator_pass: 1,
    grammar_match: null,
    crossfoot_ok: 0,
    crossfoot_residual_suspect: true,
    char_ambiguity: 0.25,
    quality_tier: 'clean_digital',
  },
  document: {
    doc_id: 'doc-a',
    doc_type: 'parts_statement',
    quality_tier: 'clean_digital',
    route: 'digital_pdf',
    split: 'test',
  },
  neighbors: [VIN_ITEM],
}

export const CLAIM_DETAIL: ReviewItemDetail = {
  ...CLAIM_ITEM,
  signals: {
    self_consistency: 0,
    det_llm_agreement: null,
    validator_pass: 1,
    grammar_match: 1,
    crossfoot_ok: null,
    crossfoot_residual_suspect: false,
    char_ambiguity: 0.4,
    quality_tier: 'scan_heavy',
  },
  document: {
    doc_id: 'doc-b',
    doc_type: 'warranty_credit_memo',
    quality_tier: 'scan_heavy',
    route: 'scanned_pdf',
    split: 'train',
  },
  neighbors: [],
}

export const DETAILS: Record<string, ReviewItemDetail> = {
  'fld-a-0001': VIN_DETAIL,
  'fld-a-0002': AMOUNT_DETAIL,
  'fld-b-0001': CLAIM_DETAIL,
}

// The exceptions contract test's seeded rows, already in the API's ranking:
// absolute dollar impact descending.
export const EXCEPTIONS: ExceptionListResponse = {
  items: [
    {
      exception_id: 'exc-3',
      run_id: 'run-contract-0001',
      exception_type: 'short_pay',
      doc_id: 'doc-a',
      statement_line_no: 3,
      ledger_entry_id: null,
      match_key: 'ro:RO123456',
      statement_amount_cents: 50_000,
      ledger_amount_cents: 300_000,
      dollar_impact_cents: 250_000,
      memo_amount_cents: 0,
      explanation: 'factory withheld 2500.00',
      status: 'open',
      detected_at: '2026-08-01T12:00:00Z',
    },
    {
      exception_id: 'exc-5',
      run_id: 'run-contract-0001',
      exception_type: 'duplicate',
      doc_id: 'doc-a',
      statement_line_no: 5,
      ledger_entry_id: null,
      match_key: null,
      statement_amount_cents: 60_000,
      ledger_amount_cents: null,
      dollar_impact_cents: -60_000,
      memo_amount_cents: 0,
      explanation: 'line 5 repeats line 4',
      status: 'resolved',
      detected_at: '2026-08-01T12:00:00Z',
    },
    {
      exception_id: 'exc-1',
      run_id: 'run-contract-0001',
      exception_type: 'amount_mismatch',
      doc_id: 'doc-a',
      statement_line_no: 1,
      ledger_entry_id: 'led-parts_payable-00007',
      match_key: 'invoice:M1234567',
      statement_amount_cents: 105_000,
      ledger_amount_cents: 150_000,
      dollar_impact_cents: -45_000,
      memo_amount_cents: 0,
      explanation: 'statement is 450.00 under the ledger',
      status: 'open',
      detected_at: '2026-08-01T12:00:00Z',
    },
    {
      exception_id: 'exc-2',
      run_id: 'run-contract-0001',
      exception_type: 'missing_from_ledger',
      doc_id: 'doc-a',
      statement_line_no: 2,
      ledger_entry_id: null,
      match_key: null,
      statement_amount_cents: 12_000,
      ledger_amount_cents: null,
      dollar_impact_cents: 12_000,
      memo_amount_cents: 0,
      explanation: 'statement line never reached the ledger',
      status: 'open',
      detected_at: '2026-08-01T12:00:00Z',
    },
    {
      exception_id: 'exc-4',
      run_id: 'run-contract-0001',
      exception_type: 'timing_difference',
      doc_id: 'doc-a',
      statement_line_no: 4,
      ledger_entry_id: 'led-parts_payable-00011',
      match_key: null,
      statement_amount_cents: 88_000,
      ledger_amount_cents: 88_000,
      dollar_impact_cents: 0,
      memo_amount_cents: 88_000,
      explanation: 'posted outside the statement period',
      status: 'open',
      detected_at: '2026-08-01T12:00:00Z',
    },
  ],
  total: 5,
}

export const METRICS: MetricsResponse = {
  scorecard: {
    run_id: '20260807T090000-bbbbbbb',
    created_at: '2026-08-07T09:00:00Z',
    git_sha: 'bbbbbbb',
    dataset_config_hash: 'b'.repeat(64),
    master_seed: 42,
    split: 'test',
    models_used: ['gemini-3.5-flash'],
    documents_total: 105,
    documents_processed: 104,
    documents_unprocessable: 1,
    field_accuracy: [
      {
        field_family: 'amount',
        quality_tier: 'csv',
        fields_in_truth: 12,
        fields_expected: 10,
        fields_extracted: 9,
        fields_spurious: 0,
        correct_canonical: 9,
        correct_raw: 8,
      },
      {
        field_family: 'amount',
        quality_tier: 'scan_heavy',
        fields_in_truth: 154,
        fields_expected: 150,
        fields_extracted: 140,
        fields_spurious: 2,
        correct_canonical: 121,
        correct_raw: 118,
      },
      {
        field_family: 'reference',
        quality_tier: 'scan_heavy',
        fields_in_truth: 230,
        fields_expected: 220,
        fields_extracted: 200,
        fields_spurious: 3,
        correct_canonical: 154,
        correct_raw: 150,
      },
    ],
    calibration: [
      { field_family: 'amount', mean_confidence: 0.91, empirical_accuracy: 0.88, count: 120 },
      { field_family: 'reference', mean_confidence: 0.55, empirical_accuracy: 0.51, count: 80 },
    ],
    threshold_sweep: [
      {
        field_family: 'amount',
        threshold: 0.5,
        auto_accept_precision: 0.951,
        review_rate: 0.42,
      },
      {
        field_family: 'amount',
        threshold: 0.9,
        auto_accept_precision: 0.9964,
        review_rate: 0.181,
      },
      {
        field_family: 'reference',
        threshold: 0.9,
        auto_accept_precision: 1,
        review_rate: 0.196,
      },
    ],
    reconciliation: [],
    costs: [],
    notes: 'first calibrated run',
  },
  calibration: [
    { field_family: 'amount', mean_confidence: 0.91, empirical_accuracy: 0.88, count: 120 },
    { field_family: 'reference', mean_confidence: 0.55, empirical_accuracy: 0.51, count: 80 },
  ],
  threshold_sweep: [
    { field_family: 'amount', threshold: 0.5, auto_accept_precision: 0.951, review_rate: 0.42 },
    { field_family: 'amount', threshold: 0.9, auto_accept_precision: 0.9964, review_rate: 0.181 },
    { field_family: 'reference', threshold: 0.9, auto_accept_precision: 1, review_rate: 0.196 },
  ],
}
