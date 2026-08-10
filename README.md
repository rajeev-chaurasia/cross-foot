# Crossfoot

Crossfoot reads dealership statements, scores its own confidence on every field it
extracts, reconciles the result against the dealer ledger, and sends only the fields it
does not trust to a human.

Start with the worst number. On the held out test split, reference fields on the
`scan_heavy` tier come back **9.2 percent** correct, 13 of 142. Amounts on that tier are
39.8 percent, 33 of 83. That floor is not going to move much: a 17 character VIN on a
bad photocopy gives seventeen chances to read `0` as `O`.

The claim is not accuracy. The claim is that the system knows which fields to distrust.
Confidence is fit on the train split, thresholds are chosen on the calibration split, and
these are the numbers the held out test split produced at those thresholds:

| Held out test split | Value |
| --- | --- |
| Fields scored | 1775 |
| Auto accept precision | 96.63 percent |
| Review rate | 26.37 percent |
| Injected discrepancies caught, matching engine fed truth | 71 of 71, zero false detections |
| Injected discrepancies caught, matching engine fed extractions | 51 of 71, 150 false detections |

A reviewer looks at about one field in four. What they skip is 96.6 percent correct. The
last two rows are the same matcher over the same discrepancies, once on ground truth lines
and once on what the pipeline actually read, so the distance between them is extraction
error and nothing else.

Two earlier versions of this page published better numbers, and both were wrong in ways
worth naming.

The first, 98.02 percent at 16.02 percent review, was partly bought with information no
real document carries. Four confidence features were read out of the dataset manifest
rather than out of the artifact: the generator's degradation tier, the true statement
period, the true marque, and the true per line type. Every one is now derived from the file
and the extraction, or dropped, and what each cost is below under "What the honest signals
cost".

The second, 97.49 percent at 28.84 percent review, was measured over a corpus that had
quietly lost 26 of its 210 documents. Seventeen XLSX files were carrying an `unrecognized`
verdict from a router that predated the XLSX extractor. Eight scanned statements had failed
schema validation twice, which the run recorded as a bad document; every one of those
responses had stopped at exactly 4096 tokens, the default context the local model was being
served with, so the JSON was cut off mid object. One more was dropped outright when a
provider timed out, and nothing in the scorecard said a document was missing. All 26 are
read here, and the numbers got worse, because eight of the documents that came back are
hard scans.

![Auto accept precision against review rate, per family](scorecards/20260810T053457-a134f91/threshold-sweep.png)

Filled marker: the operating point chosen on the calibration split. Open marker: what the
held out test split reached at that same threshold. The arrow between them is the
generalization gap, drawn rather than described. Figure and numbers both come from
`scorecards/20260810T053457-a134f91/`.

[docs/walkthrough.md](docs/walkthrough.md) is the same system in diagrams and screenshots:
what happens to one document, how a field earns or loses trust, and what the three review
surfaces actually look like.

## The problem

Every month an OEM sends each of its dealers a stack of statements: parts billing,
warranty credit memos, floorplan interest, incentive payments. Each one is a list of line
items the factory believes it owes or is owed. The dealership has its own ledger of what
it believes. Someone in the office reconciles the two by hand.

The documents arrive as clean PDFs, as photocopies of photocopies, as CSV exports with
whatever header names the export tool felt like, and as spreadsheets with merged title
cells. A missed short pay or a duplicated claim is real money, and the errors that matter
are the ones nobody has time to look for.

The interesting part is not reading the documents. It is knowing which readings to act on.

## What it does

```
gen -> extract -> confidence -> reconcile -> review
```

1. **Generate.** `crossfoot gen` builds the corpus: a ledger, statements composed from it,
   discrepancies injected into the statements, then rendering to PDF through Chromium,
   degradation to scans through Augraphy at 200 DPI, and messy CSV and XLSX exports. Every
   printed string is recorded, so ground truth is known by construction rather than
   labelled after the fact.
2. **Route and extract.** Documents are routed by file signature, never by the manifest,
   so a mislabelled artifact fails the way it would in production. Digital PDFs, CSVs and
   XLSX go to deterministic extractors. Scanned PDFs are rasterized and sent to a vision model
   under a JSON schema, sampled twice per document (temperature 0, then temperature 0.4
   with the prompt field order shuffled) so per field agreement becomes a signal.
3. **Score confidence.** Each field gets a `FieldSignals` record: self consistency across
   the two samples, a VIN check digit validator, a date window read off the other dates the
   same extraction produced, a grammar match against the marque the document's own
   reference numbers vote for, whether the document's line amounts crossfoot to its printed
   total, a confusable glyph ratio over `{O0, I1l, S5, B8, Z2}`, and the route the router
   chose from the file's bytes. A logistic regression per field family, hand rolled in
   numpy, turns those into a probability. Every one of those is computable from the
   artifact alone; truth enters a fit only as the label on a row.
4. **Reconcile.** Three passes against the ledger: exact reference plus amount, exact
   reference with a differing amount, then a fuzzy pass scored on reference similarity,
   amount proximity, and date proximity. Unmatched lines and unconsumed ledger entries
   become typed exceptions with signed dollar impact.
5. **Review.** `crossfoot serve` builds a SQLite review database and serves a keyboard
   driven queue sorted by ascending confidence, with the cropped pixels the value came
   from next to it, plus an exceptions dashboard ranked by dollars. Corrections are append
   only: the original extraction stays recoverable as evidence and as a future eval label.

## Results

All figures below are the held out test split, from
`scorecards/20260810T053457-a134f91/scorecard.json` unless named otherwise.

### Per field accuracy by tier

Canonical accuracy, correct over the fields the artifact actually printed:

| Family | clean digital | scan light | scan heavy | csv | xlsx |
| --- | --- | --- | --- | --- | --- |
| amount | 95.8 (205/214) | 66.9 (93/139) | **39.8 (33/83)** | 100.0 (53/53) | 89.2 (33/37) |
| date | 97.3 (178/183) | 32.2 (38/118) | 37.5 (27/72) | 100.0 (53/53) | 89.2 (33/37) |
| reference | 93.2 (287/308) | 54.0 (101/187) | **9.2 (13/142)** | 100.0 (90/90) | 93.5 (58/62) |
| text | 93.2 (151/162) | 66.3 (69/104) | 60.9 (39/64) | 100.0 (53/53) | 100.0 (33/33) |

![Per field accuracy by quality tier](scorecards/20260810T053457-a134f91/field-accuracy-heatmap.png)

Most of the scanned tier loss is fields the model never returned at all, not fields it
returned wrong. That distinction matters more than it looks, because auto accept precision
counts only what came back. Splitting the two:

| Family and tier | Returned, percent of printed | Correct, percent of returned |
| --- | --- | --- |
| amount, scan light | 66.9 (93/139) | 100.0 (93/93) |
| amount, scan heavy | 69.9 (58/83) | 56.9 (33/58) |
| date, scan light | 32.2 (38/118) | 100.0 (38/38) |
| date, scan heavy | 41.7 (30/72) | 90.0 (27/30) |
| reference, scan light | 57.2 (107/187) | 94.4 (101/107) |
| reference, scan heavy | 63.4 (90/142) | 14.4 (13/90) |
| text, scan light | 66.3 (69/104) | 100.0 (69/69) |
| text, scan heavy | 70.3 (45/64) | 86.7 (39/45) |

So when the confidence section below reports 98.80 percent precision on dates while this
table reports 32.2 percent accuracy on light scan dates, both are true and the denominators
differ: the model returned 38 of 118 dates and every one of them was right. It abstains
rather than guesses. That is defensible behaviour and it is not visible in a precision
number, which is why both tables are here.

Reference on heavy scans is the one cell where the model reads confidently and reads
wrong: 90 of 142 references returned, 13 of them right. That is the VIN transcription
problem, and it is exactly the cell the confidence model has to catch. Across the whole
test split the extractor produced 6 spurious fields, values that resolve to no truth field
at all, out of 1781 extracted.

### Confidence: what was promised, what held

Per family, the operating point chosen on the calibration split and what the held out test
split reached at that same threshold. Both points are in the committed sweep.

| Family | Threshold | Calibration precision | Calibration review rate | Test precision | Test review rate |
| --- | --- | --- | --- | --- | --- |
| amount | 0.6823 | 99.50 | 6.91 | 96.38 | 6.55 |
| date | 0.8917 | 99.66 | 0.00 | 98.80 | 0.00 |
| reference | 0.9147 | 99.55 | 64.60 | 94.06 | 68.49 |
| text | 0.7294 | 97.04 | 0.00 | 96.37 | 0.00 |

Every family lost precision on held out data, and every family missed the precision target
its threshold was chosen to hit. The targets are 99.5 percent for amounts and references,
99 percent for dates, 97 percent for text. On test they delivered 96.38, 94.06, 98.80 and
96.37. A threshold chosen to a target on one split does not carry that target to another,
and 52 calibration documents are a small sample doing a load bearing job.

Reference reviews more than two fields in three and still only reaches 94.06 percent on
what it accepts. It is the family carrying the heavy scan VINs, where 90 of 142 references
come back and 13 of those are right, so a model that cannot separate them has little left
to do but send most of them to a human. Nearly the whole review burden this system creates
is one family on one tier.

Amount over promises hardest in absolute terms, 99.50 against 96.38 at a review rate near
7 percent either way. What it lost was a sign check against the line type, and no extractor
produces a line type, so for a single amount what remains is whether the text parsed. The
arithmetic standing in for it is the document level crossfoot and the residual suspect flag
beside it.

Date and text auto accept everything at both points. Their thresholds sit below the
confidence of every field in the family, which is the model saying it distrusts none of
them. On a split where 32.2 percent of scan light dates are read correctly, that is a
weakness of the date signals rather than a strength: what saves the precision number is
that the reader abstains instead of guessing, not that the scorer caught anything.

### What the honest signals cost

`SignalContext` used to be handed a `ManifestRecord`. Four of the values it took from there
have no inference time equivalent, and `src/crossfoot/scoring.py`, which materializes the
review database the API and the UI read, took the same route. What replaced each:

| Signal | Was | Is |
| --- | --- | --- |
| categorical base rate | the generator's `quality_tier`, one hot encoded | `ExtractionRoute`, read off the file's bytes by the router |
| date window | the true `period_start` and `period_end` | the statement date this extraction produced, or the middle of the document's other dates, and never a date's own |
| amount validator | parsed, and the sign agreed with the true line type | parsed |
| reference grammar | the true `oem`'s grammar | the marque this document's own reference numbers vote for, falling back to whether any marque recognizes the value |

Measured by refitting on train, rechoosing thresholds on calibration and rescoring test
each time, the tier label alone was worth about 16 percentage points of review rate:
merging `scan_light` and `scan_heavy` into one scanned value cost 4 points, and removing
tier information entirely cost 37. The route lands between them, because a document does
announce whether it carries a text layer and does not announce how badly it was
photocopied.

`tests/unit/test_truth_boundary.py` walks the AST of every module under `src/crossfoot`
except the generator, the eval harness and the manifest model itself, and fails the build
if any of them imports the manifest or mentions `manifest.json`. It also pins the field
lists of `FieldSignals` and `SignalContext`, because an import guard alone would not catch
the leak coming back as a new field.

![Reliability of the confidence score](scorecards/20260810T053457-a134f91/reliability-diagram.png)

Expected calibration error on test, computed from the committed calibration bins with
`expected_calibration_error` in `src/crossfoot/confidence/calibration.py`: amount 0.027,
date 0.043, reference 0.093, text 0.051. The contract sets a ceiling of 0.05 and calls for
Platt scaling fit on the calibration split above it. Reference is well over the ceiling and
text sits just past it; the remedy has not been applied. See Limitations.

### Reconciliation: how much of the error is extraction

The matching engine runs in two modes over the same code. Oracle mode feeds it ground
truth statement lines. End to end mode feeds it extracted lines. The distance between them
is the error extraction adds to matching.

| Exception type | Oracle caught | End to end caught | End to end false detections |
| --- | --- | --- | --- |
| missing from ledger | 14/14 | 8/14 | 14 |
| missing from statement | 13/13 | 13/13 | 129 |
| amount mismatch | 14/14 | 11/14 | 3 |
| duplicate | 10/10 | 6/10 | 0 |
| short pay | 7/7 | 3/7 | 4 |
| timing difference | 13/13 | 10/13 | 0 |
| **total** | **71/71, zero false** | **51/71 (71.8 percent)** | **150** |

Sources: `scorecards/recon-oracle-20260810T053715/scorecard.json` and
`scorecards/recon-end_to_end-20260810T053640/scorecard.json`, same dataset hash, same test
split, same commit. By dollars, end to end recovers 334,347.59 of 367,465.71 injected, or
91.0 percent, because the largest discrepancies sit on lines that were read correctly.

![Exception recall by type, counts with a dollar weighted overlay](scorecards/recon-end_to_end-20260810T053640/exception-recall.png)

The 150 false detections are the real finding here, and 129 of them are one failure mode:
when extraction misses a statement line, the ledger entry it should have matched looks
unclaimed, and the engine reports it as missing from the statement. Recall degrades
gracefully under extraction error; precision does not. Note which way the false count moved
when six more documents began to be read: recall rose on five of six exception types, and
the false detections rose with it, because more documents read imperfectly means more
ledger entries left looking unclaimed.

Oracle mode is not vacuously perfect. An earlier committed oracle run over the train
split, `scorecards/recon-oracle-20260809T090106/scorecard.json` at commit `433d01a`,
caught 137 of 141 with 4 false detections.

### Cost

Actual spend on the published run was zero. The vision path was served by a local model
through an OpenAI compatible gateway, so the tokens cost electricity, not money.

Reporting zero would make a local run look free rather than cheap, so the price table in
`src/crossfoot/constants.py` carries an explicit **equivalence**: what the same tokens
would list for at a comparable hosted vision model. Under that equivalence the cost ledger
for the whole corpus, all three splits, totals 0.1831 USD across the 210 documents that
were processed, about **0.00087 USD per document**. Of that, 0.0614 USD is cloud traffic
priced at a real list rate and 0.1218 USD is the local model priced as if it were hosted.
The prices themselves are marked `# unverified` in the table until confirmed against each
provider's public page.

This is the one number on this page that is not in a committed scorecard. `crossfoot eval`
does not populate the scorecard's `costs` section yet, so every committed scorecard has
`"costs": []`, and the figure above comes from the run's own cost ledger under `data/`,
which is not committed. Treat it accordingly.

## Why the numbers are worth anything

**Ground truth by construction.** Nothing is hand labelled. The generator composes each
statement from a ledger it also writes, injects discrepancies it records, and the renderer
captures every string it prints into `rendered_values`. Truth is what was printed, known
before any extractor sees the file.

**A field counts as expected only if the artifact printed it.** CSV exports carry no
statement totals; XLSX exports carry only some header fields. Counting those against the
extractor measured the format, not the pipeline. So the scorecard publishes both
denominators side by side, `fields_in_truth` and `fields_expected`, and a reader can judge
the change instead of taking it on trust. On this test split they are 2227 and 2194.

**Document level splits, and the discipline is code.** Splits are assigned per document,
stratified by document type and quality tier, 50/25/25 across 105 train, 52 calibration,
and 53 test documents, so no field from one statement can appear on both sides of a split.
`fit_scorers` refuses any split but train and `choose_thresholds` refuses any split but
calibration, both checking the caller's stated intent and the split tag on every row.
Misuse raises `SplitDisciplineError` rather than quietly inflating a scorecard.
`sweep_point`, which measures a threshold already chosen, is deliberately not guarded and
carries a docstring saying why.

**Nothing that scores a document can see the answers.** One test walks the AST of every
module under `src/crossfoot` and fails if any of them imports the generator or the manifest
models, or mentions `manifest.json`. Five modules are exempt and each is named with its
reason: the generator writes the answer key, the eval harness scores against it, the
manifest model is it, and `ingest_db.py` and `cli.py` are the dataset commands. An earlier
version named three packages instead, which left `scoring.py` outside the net and let the
manifest reach the review database; that is the leak this build closed.

**Scorecards are committed with their git sha.** Each carries `run_id`, `git_sha`,
`dataset_config_hash`, `master_seed`, and the split it reports. The dataset hash on all
three test split scorecards here is `e14a532c` and the seed is 42, so anyone can
regenerate the corpus and confirm they are reading numbers about the same 220 files. All
three were produced at commit `a134f91`, after the corpus repair and before any later
change, so each `git_sha` names code that reproduces it. The scorecard also records every
model that served the run, which is how `qwen2.5vl:7b` and `qwen2.5vl-crossfoot:7b` both
appear: the corpus was extracted across both, and the second is the first with a context
window wide enough to finish a long statement.

**Figures live next to the scorecard that produced them.** `crossfoot plots` writes each
PNG into the scorecard's own directory and stamps the run id, split, dataset hash, and git
sha into the caption. A figure cannot drift from its numbers because it has nowhere else
to live.

## Reproduce it

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
just setup                  # uv sync, pre-commit hooks, playwright chromium
just                        # ruff, mypy strict, pytest
```

Build the corpus and score the deterministic tiers. This much needs no API key and no GPU:

```bash
uv run crossfoot gen --seed 42 --out data/dataset
uv run crossfoot eval --dataset data/dataset --split test
uv run crossfoot reconcile --dataset data/dataset --split test --mode oracle
```

`gen` renders through Playwright Chromium and degrades scans through Augraphy, so it is
the slow step. The same seed produces byte identical output, asserted by
`tests/contract/test_generator_determinism.py`.

The scanned tier needs a vision model that also supports `json_schema` response formats.
Point `CROSSFOOT_LLM_BASE_URL` at any OpenAI compatible endpoint, local or hosted, or set
a provider key from `.env.example`:

```bash
just model                                                          # local vision model, wide context
uv run crossfoot probe                                              # what is reachable
uv run crossfoot extract --dataset data/dataset --split train --mode live
uv run crossfoot extract --dataset data/dataset --split calibration --mode live
uv run crossfoot extract --dataset data/dataset --split test --mode live --resume
uv run crossfoot calibrate --dataset data/dataset                   # fit, then choose
uv run crossfoot eval --dataset data/dataset --split test           # scores the saved run
uv run crossfoot reconcile --dataset data/dataset --split test --mode end_to_end
uv run crossfoot plots
```

Then the review surface:

```bash
cd frontend && npm install && npm run build && cd ..
uv run crossfoot serve --dataset data/dataset
```

Honest scope. `crossfoot eval` scores the saved extractions from `crossfoot extract` when
they exist and falls back to the offline routes when they do not, so running it on a fresh
checkout scores CSV and digital PDF only and every scanned document reads as zero. The
published scanned tier numbers came from one specific local vision model; a different
model gives different numbers, and reproducing this table exactly means reproducing that
choice. `data/` is not committed, so the corpus, the extractions, and the ledgers are
rebuilt locally rather than downloaded.

## Design notes worth defending

**The provider was chosen by probing, not by a leaderboard.** Every candidate got one
direct call carrying an image and a `json_schema` response format, against its own default
model. The capability matrix in `docs/contracts-phase2.md` is the result, and it is a
matrix of what each provider would actually do rather than what a benchmark says it can.
Groq answered that content must be a string and that it does not support the
`json_schema` response format, so it is text only. Before that was known, a capability
blind spillover chain with Groq sitting second lost 36 of 105 documents in one run: a 400
is correctly classified as fatal, so those documents neither retried nor spilled over. The
vision pool is now capability filtered where it is constructed. The published run ended up
served mostly by a local model, not because local is fashionable, but because free tier
daily quota ran out partway through a 220 document corpus, and a run that cannot finish is
not a run.

**With structured outputs, the schema is the specification, so do not restate it.** The
extraction prompt is one sentence: extract every field and every line item from this
document type. An earlier version described the shape in prose and listed the field names.
The smaller vision model then returned a schema valid response with an empty line array on
every scanned document, which read as an accuracy collapse rather than the prompt fault it
was. Naming internal field names (`vin`, `line_amount`) sent it hunting for column headers
the page never prints and it answered with correctly shaped rows whose every value was
null: invalid output on two document types and all null rows on a third. The reasoning is
kept in the docstring of `_user_prompt` in `src/crossfoot/extraction/llm_vision.py` so
nobody helpfully adds the detail back.

**Oracle mode is a reporting requirement, not a debugging convenience.** Running the
matching engine over ground truth lines and over extracted lines gives two numbers whose
difference is attributable. Without it, "we caught 65 percent of discrepancies" is one
number that could mean a weak matcher or a weak reader, and the fix for those is not the
same fix. `reconcile()` takes a `StatementDoc` in both modes so it is provably the same
code path.

**Correctness never depends on model reported coordinates.** Review crops come from
pdfplumber word boxes on the deterministic path, and from row stripes detected by a
horizontal projection profile on the vision path. A model supplied bounding box only
refines a band it already agrees with, and is discarded silently when it fails a sanity
check. Bounding boxes are never scored.

Row detection works far less often than that description suggests, and the failure is not
random. Measured over the whole review database, 493 of 1514 scanned fields render as a row
band and the other 1021 fall back to the whole page, so banding is the minority at 32.6
percent. It is almost entirely a property of the marque's layout: Kaizen bands 77.3 percent
of the time and Meridian 65.5, while Northstar manages 5.1 percent and Atlas never bands
once across 371 fields. On an Atlas scan the reviewer is handed the entire statement and
told to find the value, which the crop caption says in as many words rather than implying a
tight crop that is not there.

## Limitations

**The documents are synthetic.** These are generated statements: realistic in format,
built from four fictional marques, with the paperwork conventions of real OEM statements
but none of their content. No real dealership has run this and no real statement has been
through it. Every number on this page describes how the pipeline performs on the
generator's world. Real scans are dirtier in ways a generator does not know to imitate,
real chart of accounts data is messier than a synthesized ledger, and the discrepancy mix
here is one a script chose. Treat the numbers as a measurement of the method, not a
forecast of production accuracy.

**Prompt injection is defended structurally and has not been tested against a live model.**
Statement pages reach the model as rendered images and in no other form, so no text printed
on a document is ever concatenated into a prompt. That is tested rather than asserted: a
page carrying hostile instruction text produces byte identical request text to an ordinary
page, and the schema repair retry sends only where validation broke, never the value that
broke it. Model output is parsed against a fixed per document type schema and reaches no
path, SQL, shell, URL, or HTML sink, so a value that comes back wrong stays a wrong value
and never becomes an action. What is not tested is whether a live model obeys an
instruction printed on a page it read correctly. No model has been run against a hostile
document here, the corpus contains no adversarial documents, and no published number
describes resistance to injection. The pipeline's answer to an obeyed instruction is
arithmetic rather than trust: a total that contradicts its own line items fails the
crossfoot check and is routed to review, which is tested against a fake client.

**Two families are miscalibrated on test.** Reference at 0.093 expected calibration error
is well over the 0.05 ceiling the contract sets, and text at 0.051 sits just past it. Platt
scaling fit on the calibration split is specified as the remedy and has not been applied, so
the confidence numbers those two families report are more confident than they have earned.
Amount at 0.027 and date at 0.043 are inside the ceiling.

**Every family missed its precision target on held out data.** Thresholds were chosen on
calibration to hit 99.5 percent for amounts and references, 99 for dates, 97 for text; test
delivered 96.38, 94.06, 98.80 and 96.37. Thresholds chosen on 52 calibration documents are
a small sample doing a load bearing job, and the gap between the promise and the delivery is
drawn as an arrow on the sweep figure rather than left to be discovered.

**End to end reconciliation precision is poor.** 150 false detections against 51 true. A
reviewer working that queue by dollar rank would still find real money first, and 91 percent
of the injected dollars are recovered, but the queue is mostly noise, and the noise is a
downstream symptom of missed extractions rather than a matching bug.

**The reference operating point is barely an operating point.** Sending more than two
fields in three to a human is not automation, and the family still misses one accepted field
in seventeen. What would fix it is a per character transcription confidence from the vision
model, which the current response schema does not ask for.

**Date and text never send anything to review.** Both thresholds sit below every confidence
in their family, so the scorer separates nothing there. Their precision numbers hold up only
because the reader abstains on hard pages instead of guessing, and abstention is not
something the confidence model earned.

**Cost is not in a committed scorecard,** the price table entries are marked unverified,
and the local model's price is an equivalence rather than a measurement.

**Single operator tool.** No authentication, no multi tenancy, and the review database is
rebuilt from the dataset rather than migrated.

## License

Apache-2.0
