# Architecture Decision Records

Why we made each non-obvious call for the pipeline. Short, terse, dated.

## Format

```
# ADR-NNNN: Title
Date: YYYY-MM-DD
Status: Accepted | Superseded by ADR-XXXX | Reverted

## Context — one paragraph on the situation.
## Decision — one paragraph on what we chose.
## Consequences — one paragraph on the trade-off accepted.
```

## When to write a new one

- A non-obvious tech choice that future-you (or your AI agent) would re-litigate.
- A pattern that contradicts what would be the default guess.
- A "no" decision worth recording so we don't relitigate it.
- A migration / deferral with conditions for when to revisit.

Don't write ADRs for: small bug fixes, dependency bumps, naming choices, refactors that don't change behaviour.

## Index

| # | Date | Status | Title |
|---|---|---|---|
| [0001](0001-ato-api-not-austlii.md) | 2026-05-19 | Accepted | Enumerate via the ATO JSON API, not AustLII |
| [0002](0002-html-first-not-pdf.md) | 2026-05-19 | Accepted | HTML-first ingestion, PDFs as secondary artifact |
| [0003](0003-decoupled-pipeline-stages.md) | 2026-05-19 | Accepted | Four decoupled stages, each independently idempotent |
| [0004](0004-keep-austlii-prefix-tables.md) | 2026-05-19 | Accepted | Retain `01_enumerate_austlii.py` for its docid prefix tables |
| [0005](0005-pipeline-sql-source-of-truth.md) | 2026-05-19 | Accepted | Pipeline `sql/` is the source of truth for schema; web app mirrors |
| [0006](0006-cases-metadata-only.md) | 2026-05-21 | Accepted | Cases captured as metadata + ATO classification + deep-link, no full text |
| [0007](0007-visibility-tracking.md) | 2026-05-23 | Accepted | Track ATO-side visibility separately from ATO editorial status |
