# Data collection ethics

This document captures the legal and ethical basis for how the pipeline fetches content from the ATO Legal Database. Show this to anyone (legal counsel, journalists, ATO operators) who asks "what's your basis?"

## Source

- **URL pattern:** `https://www.ato.gov.au/law/view/document?docid=...`
- **Enumeration API:** `https://www.ato.gov.au/API/v1/law/lawservices/browse-content/`
- **Publisher:** Australian Taxation Office
- **Audience:** publicly accessible without authentication

## Legal basis

ATO content is **Crown Copyright with explicit permissive terms** — the ATO publishes it under a licence that grants users the right to "copy, adapt, modify, transmit and distribute" the material. This pipeline:

- **Copies** the HTML into Supabase Storage as a versioned snapshot
- **Adapts** it via Stage 3 parsing into a structured corpus (`document_paragraphs`, citation graph, etc.)
- **Re-transmits** it via a customer-facing search application

All three are explicitly permitted by the licence. No third-party copyrighted material (legislation text, case-law text) is ingested — those are linked out to legislation.gov.au and AustLII/Jade respectively (HANDOFF §3.9).

## Access pattern

- **Single user, sequential.** No multi-process or multi-machine parallelism. One Python process making one HTTP request at a time.
- **Polite randomised delays.** 1.5–3.5 seconds between requests (`POLITE_DELAY_MIN` / `POLITE_DELAY_MAX` in `scrapers/02_fetch_ato.py`). Average ~2.5s/doc.
- **Realistic User-Agent.** The ATO API filters on User-Agent and rejects custom identifiers with HTTP 403 (confirmed empirically with `ATOLegalIndexer/1.0` in earlier testing). We use a current Safari User-Agent so the API will respond. This is **compatibility, not deception** — the content is intended for public consumption and there is no access control to circumvent.
- **Optional `From:` header for identification.** When `FETCH_CONTACT_EMAIL` is set in the environment, every HTTPS request carries a `From:` header with our contact address. This gives ATO operators an unambiguous way to identify and contact us. Currently unset; will be configured before any production / cron run.
- **Conservative retry policy.** On HTTP 403 or 429 (rate-limit / bot-block), we do **not** escalate. The httpx attempt fails, the row is marked `status=failed` in `fetch_queue`, and the operator can investigate via the documented escalation path (contact ATO). We never increase request rate, rotate IPs, use proxies, or otherwise try to evade rate-limiting.
- **No Playwright fallback in the automatic path.** An earlier version of this fetcher fell back to a headless-Chromium request via Playwright on 403/429. Empirically (15,241-doc run, 2026-05-21) Akamai 403s headless-Chromium every time regardless of UA string. Honest mitigations (matching the Chromium UA to the Chromium binary) didn't move the needle; the dishonest ones (disabling the `navigator.webdriver` flag and similar) would cross this document's line on anti-detection tricks. The fallback is retired. Playwright stays installed for diagnostic use but isn't invoked by the production fetcher. Practical effect: corpus coverage caps at fast-path success rate (~99.1% on first run; the residual ~0.9% sits in `fetch_queue` with `last_error` populated, available for case-by-case manual review).

## Scale

- **15,241 documents** in the Phase 1 corpus (24 ATO ruling doc types)
- At ~2.5s/doc, the full backfill takes ~10.6 hours of low-rate traffic
- Daily monitor re-fetches changed documents only — typically <100 per day in steady state

## What we will not do

| Practice | Why we don't |
|---|---|
| Multi-threaded / concurrent fetching | Pushes us from "polite single user" to "scraper" |
| IP rotation, residential proxies | Strong signal of access-control evasion |
| Anti-bot fingerprint masking beyond standard browser UA | Same — looks like circumvention |
| Caching to skip subsequent ATO requests | We *do* cache, but only to avoid re-fetching unchanged docs (hash-based change detection) — we still fetch on schedule to detect changes |
| Republishing third-party content (legislation text, case-law text) | Link out only — those aren't covered by Crown Copyright permissive terms |
| Selling access to the raw HTML | Not in scope; the customer product surfaces the structured derivative |

## Escalation protocol

If the ATO API begins consistently returning 403/429:

1. **Stop the fetcher** (`Ctrl-C`; the per-row commit pattern means no work is lost)
2. Increase `POLITE_DELAY_MIN` / `POLITE_DELAY_MAX` and observe
3. If still blocked after 24 h, **contact ATO via official channels** before resuming
4. **Never** "escalate" by adding parallelism, masking, or proxies

## Audit trail

Every fetched document creates rows in:
- `document_versions` — sha256 hash, fetch timestamp, raw HTML stored at `ato-raw-html/{docid}/{version_id}.html`
- `change_events` — emits a `new` / `modified` event with metadata
- `fetch_queue` — tracks `status`, `last_attempt_at`, `attempt_count`, `last_error`

These tables together constitute a full, time-stamped audit of every request made.

## Contact

`FETCH_CONTACT_EMAIL` env var when set is the canonical identifier. To report a concern about the pipeline's behaviour, contact the address that appears in the `From:` HTTP header on any of our requests.
