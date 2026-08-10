-- The review database `crossfoot serve` materializes, column for column as
-- docs/contracts-phase3.md names it, plus the three additions the same document
-- binds: fields.signals holding the FieldSignals JSON, exceptions.resolution and
-- exceptions.resolved_at, and phase 2's llm_calls living in the same file.

-- dealer_id, oem, period_start and period_end are the blocking identity: the
-- four facts the reconciler matches on that no extractor reads off a page. In
-- production they are known at ingest because you know whose statement you are
-- processing, so they are stored here and read back whenever the document has to
-- be reconciled again. Null for a file nothing could be extracted from.
CREATE TABLE IF NOT EXISTS documents (
    doc_id TEXT PRIMARY KEY,
    file_path TEXT NOT NULL,
    doc_type TEXT,
    quality_tier TEXT NOT NULL,
    route TEXT NOT NULL,
    split TEXT,
    error_kind TEXT,
    dealer_id TEXT,
    oem TEXT,
    period_start TEXT,
    period_end TEXT
);

CREATE TABLE IF NOT EXISTS fields (
    field_id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL REFERENCES documents(doc_id),
    line_no INTEGER,
    name TEXT NOT NULL,
    family TEXT NOT NULL,
    raw_text TEXT,
    value TEXT,
    value_cents INTEGER,
    value_date TEXT,
    source TEXT NOT NULL,
    crop_kind TEXT NOT NULL,
    page INTEGER,
    x0 REAL,
    y0 REAL,
    x1 REAL,
    y1 REAL,
    confidence REAL NOT NULL,
    status TEXT NOT NULL,
    signals TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exceptions (
    exception_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    exception_type TEXT NOT NULL,
    doc_id TEXT,
    statement_line_no INTEGER,
    ledger_entry_id TEXT,
    match_key TEXT,
    statement_amount_cents INTEGER,
    ledger_amount_cents INTEGER,
    dollar_impact_cents INTEGER NOT NULL,
    memo_amount_cents INTEGER NOT NULL DEFAULT 0,
    explanation TEXT NOT NULL,
    status TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    resolution TEXT,
    resolved_at TEXT
);

-- Append only: a correction adds a row and never rewrites one, so the model's
-- own reading stays recoverable and the chain replays in insertion order.
CREATE TABLE IF NOT EXISTS corrections (
    correction_id TEXT PRIMARY KEY,
    field_id TEXT NOT NULL REFERENCES fields(field_id),
    old_value TEXT,
    new_value TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_calls (
    call_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    purpose TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    total_tokens INTEGER NOT NULL,
    cached INTEGER NOT NULL,
    latency_ms INTEGER NOT NULL,
    http_status INTEGER NOT NULL,
    attempt INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    actual_cost_microusd INTEGER NOT NULL,
    list_price_microusd INTEGER NOT NULL
);

-- The operating point a build actually thresholded the fields table at, one row
-- per family. A scorecard's threshold_sweep records what a run found possible;
-- this records what was used, so the metrics page names the point in force
-- instead of choosing a fresh one at read time and hoping the two agree.
CREATE TABLE IF NOT EXISTS applied_thresholds (
    field_family TEXT PRIMARY KEY,
    threshold REAL NOT NULL,
    auto_accept_precision REAL NOT NULL,
    review_rate REAL NOT NULL,
    fit_split TEXT NOT NULL,
    threshold_split TEXT NOT NULL,
    run_id TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

-- How the crop that was actually served was cut, one row per field that has one.
-- Only the render holds the page image a row band is found on, so the render is
-- the only writer here, and a field is absent until its crop exists. That absence
-- is the whole reason this is not fields.crop_kind: that column is what the
-- extractor could tell before any page was looked at, and overwriting it with a
-- render's answer left one column meaning two things and a caption that could
-- contradict the picture under it. A row here is written with the PNG and
-- survives a rebuild for the same reason the PNG does.
CREATE TABLE IF NOT EXISTS rendered_crops (
    field_id TEXT PRIMARY KEY REFERENCES fields(field_id),
    crop_kind TEXT NOT NULL
);

-- The queue's total order, so paging reads one index rather than sorting a scan.
CREATE INDEX IF NOT EXISTS fields_queue ON fields (confidence, field_id);
CREATE INDEX IF NOT EXISTS fields_doc_line ON fields (doc_id, line_no);
CREATE INDEX IF NOT EXISTS corrections_field ON corrections (field_id);
CREATE INDEX IF NOT EXISTS exceptions_status ON exceptions (status);
