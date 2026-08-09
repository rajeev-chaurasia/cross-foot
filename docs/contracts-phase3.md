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

- `documents(doc_id, file_path, doc_type, quality_tier, route, split, error_kind)`
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
  validates against the field family, appends a `corrections` row, sets
  HUMAN_CORRECTED, returns the updated item. Rejects a value the family cannot parse
  with 422 and a message naming the family.
- `GET /api/crops/{doc_id}/{field_id}.png` -> lazily rendered crop, cached under
  `data/crops/`. Path segments are validated the same way manifest paths are; anything
  that escapes the crop root is a 400, never a file read.
- `GET /api/documents?route=&split=` and `GET /api/documents/{doc_id}`
- `GET /api/exceptions?type=&status=&min_impact_cents=&sort=impact` -> ranked by
  absolute dollar impact descending by default
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

## Determinism and honesty rules

- The review queue order is a total order: ascending confidence, then field_id. Two
  runs over the same data produce the same queue.
- Every number the UI shows comes from the database or a committed scorecard. The
  frontend computes no accuracy figures of its own.
- A field with no confidence yet is NEEDS_REVIEW, never silently auto accepted.
- The summary tile reports cost per document from the ledger's list price column, so a
  free local run still shows what the work would cost.
