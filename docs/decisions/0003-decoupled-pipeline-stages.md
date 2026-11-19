# ADR-0003: Four decoupled stages, each independently idempotent
Date: 2026-05-19
Status: Accepted

## Context
A naive monolithic scraper that enumerates, fetches, parses, and detects changes in a single pass becomes catastrophically expensive to debug and re-run: one network blip in the middle of a 28-hour fetch invalidates everything; a parser bug requires re-downloading the entire corpus to fix. We needed a design that survives partial failure and lets us re-run any stage independently.

## Decision
The pipeline is four stages, each communicating via Postgres tables (no in-memory hand-offs):

1. **Enumerate** (`scrapers/01_enumerate_ato.py` + `01b_enumerate_topics.py`) — walks the ATO API, populates `documents` catalogue, `topics` taxonomy, and `fetch_queue`.
2. **Fetch** (`scrapers/02_fetch_ato.py`) — drains `fetch_queue`, writes raw HTML to Supabase Storage, computes hashes, emits `document_versions` + `change_events`.
3. **Parse** (`parsers/03_parse_document.py`) — reads raw HTML from Storage, extracts structured data into `document_paragraphs`, `document_relationships`, `legislation_references`, `case_references`.
4. **Monitor** (`monitor/04_monitor_daily.py`) — daily cron re-enumerates + re-queues current/draft docs for hash recheck.

Each stage is idempotent (re-running it converges to the same final state) and can be invoked independently.

## Consequences
**Wins:** partial failure recoverable (just resume from the failing stage), parser bugs fixable without re-fetching, fetch can be paused/resumed mid-run, daily monitoring is a single small process. **Cost:** more inter-stage state stored in Postgres (`fetch_queue`, `document_versions`, `crawl_runs`); a small amount of duplicated boilerplate per stage (logging, Supabase client init). The duplication is ~50 lines and the stages may diverge as edge cases emerge — deliberately not factored out yet (HANDOFF.md §8.7).
