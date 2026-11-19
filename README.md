# ATO Legal Database pipeline

A data pipeline that builds and maintains a structured, searchable mirror of the **Australian Taxation Office's Legal Database**: taxation rulings, determinations, GST rulings, practice statements, class and product rulings, ATO IDs and more. It enumerates every document, fetches it politely, parses it into paragraphs and a citation graph, and re-checks the corpus every day for changes.

This repo is the data backend only. A separate application (not public) sits on top of it for semantic search and AI research.

## Why

The ATO's Legal Database is authoritative and useful, but the public interface is a single-page app with weak search, no API, no bulk download, no change alerts and no way to follow how one ruling replaces or relies on another. Tax practitioners care most about exactly those relationships. This pipeline turns the corpus into data that can answer them.

## What it has produced

As at the last full run (May 2026):

| Table | Rows | What it holds |
|---|---:|---|
| `documents` | 15,254 | Rulings corpus, with status (current, withdrawn, replaced, draft) |
| `document_paragraphs` | 421,637 | Numbered paragraphs with heading hierarchy, so "TR 2024/1, para 47" is a real row |
| `document_relationships` | 102,937 | Citation graph: replaces, amends, withdraws, cites, considers |
| `legislation_references` | 130,365 | Every Act and section cited, normalised (ITAA 1997, ITAA 1936, GST Act and others) |
| `cases` / `case_references` | 14,209 / 3,386 | Court and tribunal decisions cited, linked out to canonical sources |
| `topics` | 1,115 | The ATO subject taxonomy |

99.8% of fetched documents parse into structured paragraphs.

## How it works

```
 Stage 1  Enumerate   index pages ─► fetch_queue                     (scrapers/01_*)
 Stage 2  Fetch       canonical ATO HTML ─► object storage + versions (scrapers/02_fetch_ato.py)
 Stage 3  Parse       metadata · paragraphs · citations · legislation · cases (parsers/03_*)
 Stage 3b Embed       paragraph embeddings in pgvector               (parsers/04_embed_paragraphs.py)
 Stage 4  Monitor     daily re-enumeration + hash re-check ─► change_events (monitor/, GitHub Actions)
```

- **Enumerate.** Discovers every document per type and queues it. Idempotent on the document ID.
- **Fetch.** Plain HTTP with a 1.5–3.5 second polite delay, falling back to headless Chromium (Playwright) when blocked. Raw HTML is versioned and hashed; a changed hash emits a `change_event`.
- **Parse.** Extracts title, status and dates; numbered paragraphs with their heading hierarchy; the doc-to-doc citation graph, classified by cue words; every legislation reference; and case citations such as `[2020] FCAFC 158`.
- **Embed.** Paragraph-level embeddings, so semantic search returns a specific paragraph, not just a document.
- **Monitor.** A daily GitHub Actions job finds new documents and re-checks existing ones, so withdrawals and replacements show up as events.

## Questions the database answers

| Question | How |
|---|---|
| What is the ATO's current view on s 8-1 ITAA 1997? | `v_documents_by_section` joins legislation references to documents, current first |
| Trace the lineage of TR 2018/5 | `fn_document_lineage()`: a recursive query over the citation graph |
| What else relies on this ruling? | `v_inbound_citations`: the inverted citation graph |
| What changed in the last 7 days? | `change_events`, time-ordered |
| Find rulings on scrip-for-scrip rollovers under Subdiv 124-M | Full-text search plus a legislation filter |

## Stack

Python · Postgres (Supabase) · pgvector · Supabase Storage · httpx · Playwright · GitHub Actions

## Running it

```bash
pip install -r requirements.txt
python -m playwright install --with-deps chromium

# Postgres with pgvector: run the SQL in order
psql "$DATABASE_URL" -f sql/01_schema.sql
psql "$DATABASE_URL" -f sql/02_indexes.sql
psql "$DATABASE_URL" -f sql/03_views.sql

cp .env.example .env   # add your own database URL and keys

python scrapers/01_enumerate_ato.py
python scrapers/02_fetch_ato.py --batch-size 500   # repeat until the queue is empty
python parsers/03_parse_document.py
python parsers/04_embed_paragraphs.py
```

A full backfill takes many hours by design, given the polite delays. Run it from one stable IP.

## Responsible use and licensing

- ATO content is published under Crown Copyright terms that allow copying and redistribution, provided nothing implies ATO or Commonwealth endorsement. This project is not affiliated with or endorsed by the ATO.
- Legislation and case-law text are not ingested; they are linked to their canonical sources.
- Requests are rate-limited (1.5–3.5 seconds apart). Please keep the delays if you run it.

## Status

Built May–November 2026 over 57 commits; published here as a clean snapshot of the private repo.

It ran in production from May to November 2026 with a daily monitor, feeding a semantic-search and AI research app. It was paused in November 2026, when that app was wound down. In this public copy the daily GitHub Actions job is manual-only, because it needs the private database's keys.

Design decisions are recorded in [`docs/decisions/`](docs/decisions/) and day-to-day operation in [`docs/operational-runbook.md`](docs/operational-runbook.md).

Built with AI coding agents under human review.
