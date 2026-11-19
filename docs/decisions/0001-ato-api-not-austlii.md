# ADR-0001: Enumerate via the ATO JSON API, not AustLII
Date: 2026-05-19
Status: Accepted

## Context
We need a complete, up-to-date catalogue of ATO Legal Database documents (TR, TD, GSTR, PCG, etc.) plus their subject taxonomy. The original plan was to enumerate via AustLII (austlii.edu.au), which has good ATO ruling coverage. Mid-design we discovered the ATO's own public JSON browse API at `/API/v1/law/lawservices/browse-content/` — unauthenticated, unprotected by Akamai, and explicitly designed to feed the ATO Legal Database SPA. Confirmed by inspecting `single.legalDatabase.web.min.js`: plain `$.ajax` GET, `dataType: "json"`, no auth headers.

## Decision
Enumerate the corpus via the ATO browse API on two axes — Type tree (doc_type/year/number) via `Mode=type`, Topic tree (subject taxonomy) via `Mode=topic`. Both yield the same docids, joined many-to-many via `document_topics`. Implementation lives in `scrapers/01_enumerate_ato.py` and `scrapers/01b_enumerate_topics.py`. Concurrency 4, 0.3s polite delay.

## Consequences
**Wins:** legally cleaner (Crown Copyright with permissive terms, no AustLII commercial-use uncertainty), more current (ATO's own data is authoritative; AustLII mirrors), richer metadata (titles, hierarchy paths, docids returned directly), and politer (we don't hammer a non-profit when the source offers a public API). **Risk:** the ATO API is undocumented and could change without notice. **Mitigation:** the AustLII enumerator (`scrapers/01_enumerate_austlii.py`) is retained as a fallback (see ADR-0004), and the daily monitor (`monitor/04_monitor_daily.py`) will detect schema drift early via empty-walk alarms.
