# ADR-0006: Cases captured as metadata + ATO classification + deep-link, no full text
Date: 2026-05-21
Status: Accepted

## Context
Stage 1a's first real run surfaced 18,831 leaves in the ATO Type tree's `Cases > <Court> > <Year> > <Case name>` subtree — the ATO's own curated index of tax-relevant court and tribunal decisions. The original HANDOFF (§3.9) called for excluding all case-law content because AustLII / Jade already host the judgments and our commercial use of their text is legally uncertain. That decision is correct for case TEXT but conflates two distinct asset classes:

1. **Case TEXT** — the judgment itself. Owned by the courts; redistributed by AustLII (non-commercial terms) and Jade (commercial competitor). We should not ingest this.
2. **Case METADATA + the ATO's classification** — case name, court, year, ATO's URL, and the citation graph linking each case to every ATO ruling that mentions it. This is the ATO's own work-product under Crown Copyright permissive terms, and it's the highest-leverage piece of practitioner-product value we can capture cheaply.

Practitioners asking "what did the courts find on s 8-1 deductions" currently get only the ATO's view from our corpus. With case metadata captured as a sibling entity to documents, the same question returns: ATO's view, every case the ATO references, the DIS responses where they exist, and a one-click out to AustLII for the actual judgment text.

## Decision
Capture cases as a first-class entity in the pipeline. The new `cases` table holds one row per unique court/tribunal decision the ATO indexes. The existing `case_references` table (per-doc citation occurrences) gets a nullable `case_id` foreign key, allowing one-hop "all docs citing this case" lookups.

We **do not** ingest case body text, **do not** mirror AustLII / Jade, and **do not** parse per-court HTML formats. The judgment text is reached via deep-link from `cases.austlii_url` (or `cases.ato_url` as fallback). The web app's future case page renders the ATO-position-plus-citation-graph view, with an outbound link for the judgment itself.

## Consequences
**Wins:**
- Practitioner value-prop closes a real gap (court findings, not just ATO view) without the legal / scope cost of full-text mirroring.
- The Stage 1a walker already visits these leaves; we're just stopping the filter that dropped them.
- Daily monitor (once properly wired — separate work) automatically picks up new ATO case classifications because it re-walks the same tree.
- Schema-level dedup via `cases.slug` makes the data idempotent across re-walks.

**Cost:**
- ~18,800 new rows in `cases` plus eventual foreign-key fill-in on `case_references` after the parser runs. Small.
- Stage 1a re-run required after this change to retro-populate the cases table (~3 hour walk, idempotent for the 15,241 existing documents).
- Match rate from doc-text citations (`"[YEAR] COURT NN"` regex) to `cases.id` will not be 100% — historical / foreign / unusually-formatted citations will stay unlinked. Accepted; the FK is nullable.

**Future revisit conditions:** If practitioner pilot feedback shows the metadata layer is insufficient and they explicitly want in-app judgment text, revisit a Phase 2-style decision on full-text ingestion. Likely path then would be HCA / FCA direct (those publish under more permissive terms than AustLII's commercial restrictions); avoid AustLII redistribution. Until that signal, "metadata + deep-link" is the contract.
