# Architecture

How Crossfoot is put together, and why the seams fall where they do. The numbers live in
the [README](../README.md); this is the shape of the thing.

Colour is meaningful throughout. Blue is generation and evaluation, which exists only to
make the numbers honest. Green is the production pipeline, the part that would run against
a real dealer's mail. Amber is what a human touches. Red is a failure that the run survives.
Grey is durable state on disk.

## The system

```mermaid
flowchart TB
    subgraph gen["Generation and evaluation, never in the product path"]
        direction LR
        GEN["generator/<br/>ledger, compose, discrepancy,<br/>renderers, degrade, corrupt"]:::gen
        EV["evals/<br/>metrics, runner, plots"]:::gen
    end

    subgraph pipe["Pipeline"]
        direction LR
        RT["extraction/router<br/>reads magic bytes"]:::pipe
        DET["extraction/<br/>pdf_text, tabular, xlsx"]:::pipe
        VIS["extraction/llm_vision<br/>rasterize, ask twice"]:::pipe
        CONF["confidence/<br/>signals, scorer, calibration"]:::pipe
        REC["reconcile/<br/>engine, statement"]:::pipe
    end

    subgraph infra["Provider access"]
        direction LR
        SPILL["llm/spillover<br/>retry, cooldown, failover"]:::pipe
        CLIENT["llm/client<br/>one OpenAI compatible client"]:::pipe
        RATE["llm/ratelimit<br/>token bucket per provider"]:::pipe
    end

    subgraph store["State"]
        direction LR
        DS[("data/dataset<br/>corpus and manifest")]:::store
        RS[("runstate.db<br/>per document checkpoints")]:::store
        CD[("costs.db<br/>append only call ledger")]:::store
        LC[("llm_cache.db<br/>response cache")]:::store
        RV[("crossfoot.db<br/>review database")]:::store
        SC[("scorecards/<br/>committed numbers")]:::store
    end

    subgraph surf["Surfaces"]
        direction LR
        API["api/<br/>FastAPI, no auth"]:::human
        UI["frontend/<br/>queue, exceptions, metrics"]:::human
    end

    GEN --> DS
    DS --> RT
    RT -->|"digital pdf, csv, xlsx"| DET
    RT -->|"scanned pdf"| VIS
    RT -->|"unreadable"| ERR["typed IngestError<br/>the run continues"]:::err
    VIS --> SPILL --> CLIENT
    RATE -.paces.-> CLIENT
    CLIENT --> CD
    CLIENT --> LC
    VIS --> RS
    DET --> CONF
    VIS --> CONF
    CONF --> REC
    CONF --> EV
    REC --> EV
    EV --> SC
    CONF --> RV
    REC --> RV
    RV --> API --> UI
    SC --> API

    classDef gen fill:#e8f0fe,stroke:#3367d6,color:#10233f
    classDef pipe fill:#e6f4ea,stroke:#137333,color:#0b2c17
    classDef human fill:#fef7e0,stroke:#b06000,color:#3d2200
    classDef err fill:#fce8e6,stroke:#c5221f,color:#3d0f0e
    classDef store fill:#e8eaed,stroke:#5f6368,color:#202124
```

The blue box is the part that would not exist in production. It writes the answer key, and
an AST test fails the build if anything green ever imports it.

## What each package owns

| Package | Owns | Deliberately does not |
| --- | --- | --- |
| `generator/` | The corpus and the answer key | Know anything about extraction |
| `extraction/` | Bytes to `ExtractedDocument` | Decide whether a reading is right |
| `confidence/` | Signals to a probability, thresholds | See the manifest, ever |
| `reconcile/` | Statement lines against ledger entries | Care which extractor produced a line |
| `llm/` | One client, retry, spillover, cost, cache | Know what a statement is |
| `evals/` | Scoring against truth, scorecards, figures | Run in the product path |
| `db/` and `api/` | The review surface | Recompute any number the UI shows |

## Reading one document

```mermaid
sequenceDiagram
    autonumber
    participant F as Statement file
    participant R as router
    participant X as extractor
    participant S as spillover
    participant P as provider
    participant C as confidence

    F->>R: bytes
    R->>R: magic bytes, then probe for a text layer
    alt carries a text layer, or is csv or xlsx
        R->>X: deterministic path
        X->>X: word boxes, header synonyms, encoding recovery
    else image only
        R->>X: vision path, rasterize at 180 dpi
        loop twice, temperature 0 then 0.4
            X->>S: page images plus a schema
            S->>P: call the first profile that can serve it
            alt refused or rate limited
                P--)S: 429, 5xx, or a timeout
                S->>S: back off, then retry or spill to the next profile
                S->>P: call again
            end
            P--)S: JSON
            S--)X: reply, priced into the ledger
        end
        X->>X: validate against the frozen model, one repair turn
    end
    X->>C: fields with raw and canonical values
    C->>C: eight signals, then a per family logistic regression
    alt at or above the family threshold
        C-->>C: auto accepted
    else below it, or nothing could score it
        C-->>C: queued for a human
    end
```

Two samples matter because agreement between them is a signal, and disagreement is often
the only evidence that a character was guessed.

## A correction closing the loop

This is the part that makes it a product rather than a report.

```mermaid
sequenceDiagram
    autonumber
    participant H as Reviewer
    participant API as api/routes/review
    participant DB as crossfoot.db
    participant RC as db/reconciliation
    participant L as ledger.json

    H->>API: correct a misread amount
    API->>API: validate against the field family
    API->>DB: append a corrections row, set human_corrected
    Note over DB: the original extraction is never overwritten,<br/>so it survives as evidence and as a future label
    API->>RC: re-reconcile this one document
    RC->>DB: read its fields, newest correction winning
    RC->>L: match against the dealer's books
    RC->>DB: replace this document's exceptions
    Note over RC,DB: findings are matched across runs by type, line,<br/>ledger entry, so a resolved one stays resolved
    RC-->>API: exceptions removed, added, dollars moved
    API-->>H: cleared 1 exception, 1,840.00 dollars less at risk
```

The reconciliation the correction triggers is the same code the database build runs, so the
dashboard cannot drift from the scorecard.

## How a field earns or loses trust

```mermaid
stateDiagram-v2
    direction LR
    [*] --> Extracted
    Extracted --> Scored: eight signals attached
    Scored --> AutoAccepted: at or above the family threshold
    Scored --> NeedsReview: below it
    Scored --> NeedsReview: nothing could score it
    NeedsReview --> HumanAccepted: reviewer agrees
    NeedsReview --> HumanCorrected: reviewer overrides
    HumanCorrected --> Reconciled: the document is matched again
    AutoAccepted --> [*]
    HumanAccepted --> [*]
    Reconciled --> [*]

    note right of NeedsReview
        A field nothing could score stays
        here. Absence of evidence is not
        confidence.
    end note
```

## Where the numbers come from

```mermaid
flowchart TD
    A["220 documents<br/>stratified by type and tier"]:::data
    A --> TR["train, 105 docs"]:::train
    A --> CA["calibration, 52 docs"]:::cal
    A --> TE["test, 53 docs"]:::test

    TR --> F["fit the scorers<br/>one per field family"]:::step
    CA --> PL["fit Platt scalers<br/>then choose thresholds"]:::step
    F --> PL
    PL --> P["report on test<br/>never fitted, never tuned"]:::step
    TE --> P
    P --> SC["committed scorecard<br/>with its git sha"]:::out

    classDef data fill:#e8eaed,stroke:#5f6368,color:#202124
    classDef train fill:#e6f4ea,stroke:#137333,color:#0b2c17
    classDef cal fill:#fef7e0,stroke:#b06000,color:#3d2200
    classDef test fill:#fce8e6,stroke:#c5221f,color:#3d0f0e
    classDef step fill:#e8f0fe,stroke:#3367d6,color:#10233f
    classDef out fill:#f3e8fd,stroke:#7b1fa2,color:#2e1040
```

The order matters: the Platt scaler is fit before the threshold is chosen, because a
threshold picked on uncalibrated scores names a different operating point once the scores
under it move. Both consume the calibration split, which is a real caveat and is recorded
in `docs/contracts-phase3.md`.

`fit_scorers` refuses any split but train and `choose_thresholds` refuses any split but
calibration. Both check the split tag on every row rather than the caller's word for it,
and raise `SplitDisciplineError` instead of quietly inflating a scorecard.

## Surviving a bad provider

Every arrow here was drawn after something went wrong on a real run.

```mermaid
flowchart TD
    START["a vision call"]:::pipe --> POOL{"profiles that can<br/>serve vision and<br/>a schema"}:::pipe
    POOL --> TRY["call the first"]:::pipe
    TRY --> OK{"answered?"}:::pipe
    OK -->|yes| DONE["priced into the ledger"]:::store
    OK -->|"429 or 5xx"| BACK["back off,<br/>honour Retry After"]:::err
    BACK --> RETRY{"attempts left?"}:::pipe
    RETRY -->|yes| TRY
    RETRY -->|no| NEXT
    OK -->|"402, or quota spent"| NEXT["cool this profile down,<br/>take the next"]:::err
    OK -->|"400"| FATAL["malformed, do not retry<br/>and do not spill"]:::err
    NEXT --> MORE{"another profile?"}:::pipe
    MORE -->|yes| TRY
    MORE -->|no| FAIL["the document is owed a retry,<br/>the run continues"]:::err
    FATAL --> FAIL

    classDef pipe fill:#e6f4ea,stroke:#137333,color:#0b2c17
    classDef err fill:#fce8e6,stroke:#c5221f,color:#3d0f0e
    classDef store fill:#e8eaed,stroke:#5f6368,color:#202124
```

The 400 branch is the expensive lesson. A provider that cannot read images answers 400, and
a 400 is correctly classified as fatal, so a capability blind pool with such a provider
second lost 36 of 105 documents in one run. The pool is filtered by capability where it is
constructed, and a document that fails everywhere is owed a retry rather than marked bad.

## State on disk

```mermaid
erDiagram
    documents ||--o{ fields : "has"
    documents ||--o{ exceptions : "raises"
    fields ||--o{ corrections : "accumulates"
    exceptions ||--o| exception_resolutions : "may be closed by"
    fields ||--o| rendered_crops : "may have a cut crop"

    documents {
        text doc_id PK
        text route
        text split
        text dealer_id "blocking identity"
        text period_start "known at ingest"
    }
    fields {
        text field_id PK
        text value "never mutated by a correction"
        real confidence
        text status
        text signals "the breakdown that produced it"
    }
    corrections {
        text correction_id PK
        text old_value
        text new_value
        text reviewer "never blank"
    }
    exceptions {
        text exception_id PK "derived from what it is about"
        int dollar_impact_cents
        text status
    }
    exception_resolutions {
        text exception_id PK
        int dollar_impact_cents "the facts the decision was made about"
    }
```

Two details carry weight. `exception_id` is derived from the finding's type, line and
ledger entry rather than from emission order, so re-reconciling names the same finding the
same thing and a reviewer cannot close one exception while reading another. And a
resolution stores the money it was made about, so a re-derivation that moves the amount
reopens the finding instead of leaving a note about nine dollars closing a six figure
discrepancy.

`data/` also holds three stores the review database does not own: `runstate.db` for per
document checkpoints, which is what makes `--resume` safe; `costs.db`, an append only
record of every call attempt; and `llm_cache.db`, keyed on model, prompt and image digest.

## Further reading

- [walkthrough.md](walkthrough.md) walks the three screens with screenshots.
- [contracts-phase1.md](contracts-phase1.md), [phase 2](contracts-phase2.md) and
  [phase 3](contracts-phase3.md) are the frozen interfaces, each amended in place with
  dated clarifications when reality disagreed with them.
