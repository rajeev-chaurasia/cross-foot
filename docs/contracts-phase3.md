# Phase 3 interface freeze

The review surface: a FastAPI backend, a keyboard-driven review queue, an exceptions
dashboard ranked by dollars, and the published calibration figures. Same rules as
before: contract tests are written against these interfaces before implementation,
implementations must pass them unedited, and a change here routes through the
maintainer and re-freezes.

Phases 1 and 2 stay in force except where this document amends them.

## What this phase is for

Raw extraction accuracy on scanned documents is mediocre and always will be: a
17 character VIN on a heavy photocopy gives seventeen chances to read 0 as O. The
product claim is not accuracy, it is knowing which fields to distrust. The first
calibration run supports it: reference fields auto-accept at 100 percent precision
while sending 19.6 percent to review, amounts at 99.64 percent while sending 18.1
percent. The review queue is where that claim becomes visible, so the UI exists to
make one thing obvious: a reviewer sees only the uncertain fields, next to the pixels
they came from.

## Persistence (`crossfoot.db`)

SQLite, stdlib sqlite3, WAL, explicit SQL, no ORM, matching the repo's existing style.
`crossfoot serve` reads a dataset directory plus its saved extractions and materializes:

- `documents(doc_id, file_path, doc_type, quality_tier, route, split, error_kind, dealer_id,
  oem, period_start, period_end)`. The last four are the blocking identity: the facts the
  reconciler matches on that no extractor reads off a page. In production a dealer, a
  marque and a period are known at ingest because you know whose statement you are
  processing, so they are stored at ingest and read back whenever the document has to be
  reconciled again. Null for a file nothing could be extracted from.
- `fields(field_id, doc_id, line_no, name, family, raw_text, value, value_cents,
  value_date, source, crop_kind, page, x0, y0, x1, y1, confidence, status)`
- `exceptions(...)` mirroring `ExceptionRecord`
- `corrections(correction_id, field_id, old_value, new_value, reviewer, created_at)`

Corrections are append only. A correction never mutates the original extraction: it
writes a new row and moves the field's status to HUMAN_CORRECTED. The extraction record
is evidence of what the model said and must stay recoverable, which is also what makes
corrections usable later as eval labels.

## API surface (`crossfoot.api`)

All routes under `/api`, sync `def` handlers (FastAPI runs them in a threadpool and
SQLite prefers that), pydantic DTOs in `api/dto.py`, no auth (single operator tool).

- `GET /api/stats/summary` -> documents processed, fields extracted, auto accept rate,
  review queue depth, open exception count, gross dollars at risk, cost per document
- `GET /api/review/queue?status=&family=&tier=&sort=confidence&limit=&offset=` ->
  paged review items, default sort ascending by confidence so the least trusted field
  is first. Returns total count for pagination.
- `GET /api/review/items/{field_id}` -> field detail: raw text, canonical value,
  confidence, the signal breakdown that produced it, crop URL, document context, and
  the neighbouring fields on the same line
- `POST /api/review/items/{field_id}/accept` -> status HUMAN_ACCEPTED, idempotent
- `POST /api/review/items/{field_id}/correct` body `{"value": str, "reviewer": str}` ->
  validates against the field family, appends a `corrections` row, sets HUMAN_CORRECTED,
  then re-reconciles that one document and replaces its exception rows. Returns the updated
  item plus `reconciliation`: `{"exceptions_removed": int, "exceptions_added": int,
  "dollars_at_risk_change_cents": int}`, or null. Rejects a value the family cannot parse
  with 422 and a message naming the family.

  `dollars_at_risk_change_cents` is the change in the sum of absolute impact of the
  document's open exceptions, so a correction that clears risk is negative. `reconciliation`
  is null when the document cannot be reconciled: no ledger under the dataset directory, no
  statement line the extraction could build, or no blocking identity on the documents row.
  Re-reconciliation is the same code the build runs, over the same rows, with the newest
  correction standing in for each field's value. An exception a reviewer resolved keeps its
  status and note when the rerun produces it again; findings are matched across runs by
  type, statement line, ledger entry and match key, because `exception_id` numbers a
  document's findings in emission order and renumbers when one clears. `accept` changes no
  value and is unchanged.
- `GET /api/crops/{doc_id}/{field_id}.png` -> lazily rendered crop, cached under
  `data/crops/`. Path segments are validated the same way manifest paths are; anything
  that escapes the crop root is a 400, never a file read.
- `GET /api/documents?route=&split=` and `GET /api/documents/{doc_id}`
- `GET /api/exceptions?type=&status=&min_impact_cents=&sort=impact&limit=&offset=` ->
  paged, ranked by absolute dollar impact descending by default. Returns the count the
  filter matched, which is the whole listing rather than the page.
- `POST /api/exceptions/{exception_id}/resolve` body `{"resolution": str}`
- `GET /api/metrics` -> the latest committed scorecard as JSON, plus the calibration
  points and threshold sweep

The OpenAPI schema is snapshotted with syrupy and IS the frontend contract. A route or
DTO change shows up as a snapshot diff and re-freezes.

## Frontend (`frontend/`)

Vite, React, TypeScript, Tailwind, TanStack Query. Types generated from the OpenAPI
snapshot, never hand written. Three routes:

- `/` review queue. Split view: the crop on the left, the extracted value and its
  signal breakdown on the right. Keyboard first: `j` and `k` move, `a` accepts, `c`
  focuses the correction input, `Enter` saves, `?` shows the shortcuts. The queue shows
  what fraction of all fields it represents, because "12 percent of fields, not 100
  percent of documents" is the claim being demonstrated.
- `/exceptions` dashboard. Ranked table by dollar impact, filterable by type, each row
  expanding to the statement line and the ledger entry side by side so the disagreement
  is legible without leaving the page.
- `/metrics` the published numbers: per field accuracy by tier, the reliability
  diagram, the threshold sweep with the chosen operating point marked, and cost per
  document.

No auth, no state library beyond TanStack Query, no component library. Accessibility
floor: every control reachable by keyboard, visible focus ring, and the queue announces
its position.

## Plots (`crossfoot.evals.plots`)

matplotlib with the Agg backend, PNG at 2x, written next to the scorecard that produced
them so a figure can never drift from its numbers. Four artifacts: per field accuracy
heatmap (family by tier), reliability diagram with the ideal diagonal, threshold sweep
with the operating point marked, exception recall by type with a dollar weighted
overlay. Every figure caption names its scorecard run id.

## CLI additions

- `crossfoot serve [--dataset DIR] [--port 8000] [--reload]` builds the database if
  absent, then serves the API and the built frontend.
- `crossfoot plots [--scorecard PATH]` regenerates the figures for a scorecard.

## Clarifications (binding, added 2026-08-09 after test-writer review)

Schema gaps the first draft left open:

- `fields` gains a `signals` TEXT column holding the `FieldSignals` JSON. The detail
  route has to show the breakdown that produced a confidence, and a breakdown cannot be
  recomputed from a row that never stored it.
- `exceptions` gains `resolution TEXT` and `resolved_at TEXT`, both null until resolved.
  The frozen `ExceptionRecord` model is unchanged; this is storage, not a model change.
- Phase 2's `llm_calls` table lives in the same `crossfoot.db`, unchanged in shape. The
  summary tile needs it and there is no reason for a second database file.

Semantics the first draft left ambiguous:

- Queue with no `status` filter means every field, not just NEEDS_REVIEW. Otherwise the
  `status` parameter has nothing to narrow.
- `min_impact_cents` compares the same way the ranking sorts, on absolute value, so a
  -60000 exception clears a 50000 floor. A timing difference at zero impact still lists
  under a floor of zero.
- `gross_dollars_at_risk_cents` sums the absolute impact of OPEN exceptions only.
- `cost_per_document_microusd` divides ledger list price by the documents whose route is
  not UNPROCESSABLE, since an unreadable file cost nothing to extract.
- A correction's `old_value` is the value it replaced, so a chain of corrections
  reconstructs in order. The `fields` row is never mutated by a correction.

Crop containment, stated precisely:

- Single segment hostile forms (`..`, `..\outside`, a drive letter, a UNC or device
  prefix, a leading separator, a field_id that climbs out) are rejected with 400.
- Forms carrying percent encoded separators decode into extra path segments before
  routing, so they may surface as 404 rather than 400. Either is acceptable.
- In every case, bytes from outside the crop root must never appear in a response. That
  is the assertion that matters; the status code is secondary.

The OpenAPI snapshot cannot be generated until `crossfoot.api` imports. The first run
after the implementation lands creates it, and maintainer review of that diff is the
freeze.

## Clarifications (binding, added 2026-08-10 after security audit)

Bounds the first draft left open. Each one was a 500 or an unbounded write before it
was named here.

- `offset` on both paged routes is capped at 1,000,000,000. SQLite binds an offset as a
  64 bit integer, so a paged route has to refuse one it cannot bind rather than fail
  inside the query. Above the cap is a 422; at or below it and past the end stays an
  empty page carrying the true total.
- A correction's `value` is at most 256 characters, and `reviewer` is 1 to 128
  characters after trimming. The corrections table exists to say who changed what, so an
  attribution that is blank or whitespace is not a correction. Either violation is a 422
  naming the field.
- A resolution is at most 1000 characters.
- Markup inside those strings is stored as written. It reaches no HTML sink, so
  filtering it would only corrupt a statement value that legitimately contains it.
- A path segment carrying a null byte is not a traversal, but it cannot be used as a
  path either, so containment rejects it with 400 before it reaches the filesystem.
- The 404 for a missing scorecard names no directory. An operator learns the path from
  the log, not a client.
- The frontend mount answers an unknown extensionless path outside the API prefix with
  the app shell, because routing happens in the browser and a refresh on `/metrics` is
  not an error. A path carrying a suffix and any path under the API prefix keep their
  404, since answering either with HTML hides the miss.

## Clarifications (binding, added 2026-08-10 after end to end review)

Places where a number or a caption said more than it knew.

- `GET /api/stats/summary` gains `review_queue_share`, the queue depth over the fields
  extracted, divided by the API rather than the browser. It reads the whole database,
  every split included, and it falls as reviewers work. It is not the review rate a
  scorecard publishes, which is measured on the held out split at a fixed threshold and
  does not move. The two are never presented as the same figure.
- `GET /api/review/items/{field_id}` publishes `crop_kind` and `crop_unavailable_reason`,
  exactly one of them non null. `crop_kind` is what the render that produced the served
  crop settled on, never the extractor's guess, so the caption always describes the bytes
  `crop_url` will serve.
- `CropUnavailableReason` gains `no_page_image`, for a source that has rows rather than
  pages. It is decided before any PDFium call, because a healthy spreadsheet reported as
  an unreadable document is a false alarm a reviewer has to chase.
- The database gains `rendered_crops(field_id, crop_kind)`, written only by the render.
  Absence means the crop has not been cut yet, whatever sits on disk, so a rebuilt
  `fields` table cannot leave a stale caption over a cached image. `crossfoot ingest`
  does not touch it.
- `GET /api/metrics` serves the newest committed scorecard carrying field accuracy,
  calibration, or a threshold sweep. A reconciliation run commits a scorecard holding
  only reconciliation cells, and a full pipeline run ends with reconciliation, so
  newest by timestamp alone serves a scorecard with nothing the page can draw.
- The rule that every number comes from the database or a committed scorecard also
  forbids dividing two published counts in the browser.

## Clarifications (binding, added 2026-08-10 after the correction loop landed)

- `exceptions.exception_id` is derived from what the finding is about, not from the order
  it was emitted in: `exc-{mode}-{doc_id}-{exception_type}` followed by
  `-line-{statement_line_no}` and `-entry-{ledger_entry_id}` for whichever the finding
  carries. Unique within a document, because a statement line carries at most one exception
  and an unmatched ledger entry exactly one. Re-reconciling names the same finding the same
  thing, which is what lets `POST /api/exceptions/{exception_id}/resolve` close the finding
  the reviewer was reading rather than whatever has since taken that number.
- `exceptions.match_key` is removed, from the table and from `ExceptionRecord`. It existed
  only to tell two findings apart across reruns, which the id now does. The repeated
  reference a duplicate points at is named in its `explanation`.
- `exception_resolutions(exception_id, resolution, resolved_at, dollar_impact_cents,
  statement_amount_cents, ledger_amount_cents)` is added. A rerun replaces every exception
  a document owns, so a decision recorded only on the row cannot survive the finding
  clearing and coming back. The three amounts are the facts the decision was made about: a
  re-derivation that moves any of them reopens the finding, because a note about a nine
  dollar credit must not go on closing a million dollar discrepancy.
- `exceptions_removed` and `exceptions_added` count open findings only, the same population
  `dollars_at_risk_change_cents` measures. A resolved finding is not risk, so its arrival or
  departure is not a change in risk.
- Re-reconciling holds its write transaction across its reads. Concurrent corrections on one
  document serialize, so the deltas they report sum to the move the dashboard shows, and a
  resolve cannot be overwritten by a rerun already in flight.
- A document whose extraction produced no statement line is still reconciled, because the
  ledger entries its period expected are findings in their own right. The route and the
  build agree on which documents have exceptions.

## Determinism and honesty rules

- The review queue order is a total order: ascending confidence, then field_id. Two
  runs over the same data produce the same queue.
- Every number the UI shows comes from the database or a committed scorecard. The
  frontend computes no accuracy figures of its own.
- A field with no confidence yet is NEEDS_REVIEW, never silently auto accepted.
- The summary tile reports cost per document from the ledger's list price column, so a
  free local run still shows what the work would cost.
