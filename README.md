# Crossfoot

Dealer statement reconciliation that reads the nasty documents, scores its own confidence
on every field, and asks a human only when it should.

Status: scaffolding. The pipeline, review queue, and published accuracy numbers land over
the coming weeks. Nothing below this line is a claim yet.

## What is coming

- A synthetic but realistic dealership dataset: OEM parts statements, warranty credit
  memos, floorplan and incentive statements, rendered as clean PDFs, rough scans, messy
  CSVs, and spreadsheets with merged cells, with ground truth known by construction
- Deterministic plus LLM extraction with a calibrated confidence score on every field
- A review queue that shows the source snippet next to each uncertain field
- Reconciliation against the dealer ledger with an exceptions dashboard ranked by dollars
- A scorecard file that the README regenerates its numbers from, never hand-typed

## License

Apache-2.0
