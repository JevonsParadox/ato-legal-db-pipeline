# Operational runbook

Lifted from `HANDOFF.md` §7 and reorganised as a standalone reference. The HANDOFF document remains the canonical project narrative; this file is the day-to-day operator's checklist.

---

## First-time setup

### 1. Supabase (web UI, ~5 min)

- Create project in **Sydney region** (`ap-southeast-2`).
- Run, in order, in the SQL editor:
  1. `sql/01_schema.sql` (tables + seed data — the editor will warn about missing RLS; choose **Enable RLS** before running)
  2. `sql/02_indexes.sql` (gin FTS, ivfflat, trgm, btree)
  3. `sql/03_views.sql` (customer-facing views — created with `security_invoker = true` so RLS evaluates as the querying user, not the view creator)
  4. `sql/04_rls_policies.sql` (enables RLS idempotently + adds permissive read policies for content/lookup tables; pipeline-internal tables `fetch_queue`, `change_events`, `crawl_runs` intentionally left without policies → service-role only)
  5. `sql/05_view_security_invoker.sql` (idempotent `ALTER VIEW ... SET (security_invoker = on)` for the 5 views — required for any project provisioned before the 03 file had the `with` clause; harmless no-op otherwise. Closes Supabase's "Security Definer View" critical alerts)
  6. `sql/06_cases.sql` (cases master table — captures the ATO's curated index of tax-relevant court / tribunal decisions; adds nullable `case_references.case_id` FK; adds `v_documents_by_case` view. Stage 1a needs a re-run after applying this migration so the new table populates with ~18.8k cases.)
  7. `sql/07_visibility_tracking.sql` (4 new columns on `documents` for tracking ATO-side visibility separately from editorial status — `last_seen_in_search`, `removed_from_search_at`, `ato_url_accessible`, `ato_url_last_checked_at`; new `v_searchable_documents` view that the web app uses as its primary search source. Per ADR-0007. Stage 1a's `flush_buffers` is patched to update `last_seen_in_search` on every observed doc; the monitor — once rewired per HANDOFF Pending #1 — uses these fields to detect quietly-removed docs.)
- Create Storage bucket `ato-raw-html` (private).
- From Project Settings → API, copy: project URL, anon public key, service_role secret, and JWT Secret (save the last in a password manager for W5 Clerk setup).

**Verify** before moving on: lookup tables `document_types` (35 rows), `document_statuses` (6), `acts` (6) should be readable via both service-role and anon. Pipeline-internal tables should be readable via service-role only.

### 2. Local / VPS

```bash
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env.local        # or .env — pipeline reads via `set -a; source <file>; set +a`
# Edit and paste SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY. NEVER commit.
set -a; source .env.local; set +a
```

### 3. Dry-run sanity checks

```bash
python scrapers/01_enumerate_ato.py --dry-run    # ~30s; expect "Walk complete: N leaves discovered"
python scrapers/01b_enumerate_topics.py --dry-run
```

---

## Initial corpus load

```bash
# Enumerate (~40–80 min total)
python scrapers/01_enumerate_ato.py
python scrapers/01b_enumerate_topics.py

# Verify in Supabase:
#   - documents table: ~50k rows for Phase 1 doc types
#   - topics table: hierarchical paths populated
#   - document_topics: non-empty

# Fetch HTML (slow — overnight on a VPS, ~28 hours wall clock)
while true; do
  python scrapers/02_fetch_ato.py --batch-size 500
  REMAINING=$(psql "$DB_URL" -tA -c "select count(*) from fetch_queue where status='pending'")
  [ "$REMAINING" = "0" ] && break
done

# Parse
python parsers/03_parse_document.py
```

---

## Steady state

> ⚠️ **Daily monitor is currently PARKED** (2026-05-21). The workflow has been reduced to a single no-op echo step so the cron stays scheduled without firing failure emails, but `monitor/04_monitor_daily.py` is NOT invoked. Rewiring it is tracked in `HANDOFF.md` "Pending" as item #1 (CRITICAL — corpus freshness depends on it). Until that lands, treat the section below as the **target state** the monitor should restore.

GitHub Actions runs `monitor/04_monitor_daily.py` at **06:00 AEST** (19:00 UTC) daily via `.github/workflows/daily-monitor.yml`. The monitor re-enumerates the ATO API, queues new docids and hash-recheck candidates, and the fetcher/parser pick them up on the next invocation.

Required GitHub repo secrets (set in repo Settings → Secrets and variables → Actions):

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`

Manual re-fetch of a specific docid:

```sql
UPDATE fetch_queue SET status='pending' WHERE docid='TXR/TR20241/NAT/ATO/00001';
```

Then run `python scrapers/02_fetch_ato.py --batch-size 1`.

---

## Debugging

| Symptom | Cause | Fix |
|---|---|---|
| Fetcher returning lots of 403s | Akamai is hardening; headers no longer convincing | Increase `REQUEST_DELAY_S` in `02_fetch_ato.py`; verify User-Agent + Referer + Accept-Language still match a real browser; check if Playwright fallback rate is climbing |
| Parser missing a citation pattern | New doc-type variant or layout change | Add regex to the relevant per-type function in `03_parse_document.py`; re-run on a single docid for fast iteration |
| Topic enumeration missing docids | Type enumeration was incomplete first | Re-run `01_enumerate_ato.py`, then `01b_enumerate_topics.py` |
| Monitor not detecting a change | Fetcher hasn't picked up the changed doc yet, or change is in title only | Check `documents.body_hash` vs latest version; `title_hash` is separate |
| Enumerator hangs / silently completes early | ATO API shape change | First fallback: try `scrapers/01_enumerate_austlii.py`. Second: inspect `single.legalDatabase.web.min.js` for any rename of `retrieveBrowseJsonContent` or the `/browse-content/` endpoint path |

---

## Things NOT to do (codified in HANDOFF.md §8)

1. Do not delete `scrapers/01_enumerate_austlii.py` — the parser imports prefix tables from it (see ADR-0004).
2. Do not embed documents yet — wait for FTS-insufficient feedback from the practitioner pilot (HANDOFF.md §6.2).
3. Do not ingest legislation or case text — link out to legislation.gov.au and AustLII/Jade.
4. Do not hammer the ATO API — 4 concurrent requests + 0.3s polite delay is the agreed cap.
5. Do not refactor the two enumerators to share code yet — the duplication is ~50 lines and they may diverge as edge cases emerge.
