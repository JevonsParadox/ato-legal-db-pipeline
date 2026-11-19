# ADR-0002: HTML-first ingestion, PDFs as secondary artifact
Date: 2026-05-19
Status: Accepted

## Context
Every ATO Legal Database document is published in both HTML and PDF. PDFs are convenient for download but lossy for structured extraction — OCR / layout drift introduces errors, and citation links + heading hierarchy + paragraph numbering all degrade. HTML carries the same content as semantic markup with stable selectors per doc type.

## Decision
Stage 2 (`scrapers/02_fetch_ato.py`) fetches and archives the HTML view (`/law/view/document?docid=...`) only. Stage 3 (`parsers/03_parse_document.py`) extracts numbered paragraphs, citations, legislation references, and case-law references from that HTML. PDFs are not ingested; if a customer surface ever needs a PDF download, link out to the ATO source rather than re-host.

## Consequences
**Wins:** higher-fidelity citation graph, no OCR error budget, smaller storage footprint (HTML compresses better than PDF), easier per-doc-type parser maintenance. **Cost:** the HTML view is Akamai-protected — Stage 2 needs browser-mimicking headers (httpx fast path ~95% success) plus a Playwright Chromium fallback for the remainder. The browse API itself is unprotected, so this Akamai cost is bounded to one URL pattern.
