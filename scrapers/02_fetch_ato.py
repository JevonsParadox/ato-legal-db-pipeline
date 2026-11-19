"""
02_fetch_ato.py
===============
Stage 2: Pull canonical document content from ato.gov.au for everything in
fetch_queue.

Strategy (in order):
    1. Fast path — plain httpx with a realistic browser header set, polite
       rate limiting, exponential backoff on 429/403/5xx.
    2. Fallback — Playwright (headless Chromium) for any docid the fast path
       can't resolve. Real browser rendering bypasses most bot protections at
       the cost of ~10x compute and time.

For each successful fetch we:
    * Compute a sha256 of the raw HTML.
    * If the hash differs from documents.source_html_hash (or the document
      doesn't exist yet), store the raw HTML to Supabase Storage and queue
      a parse task.
    * If the hash matches, do nothing — content is unchanged.

Two paths in the database side:
    - INSERT a new document and document_version (version 1) if new.
    - INSERT a new document_version if changed, and emit a change_event.

The parsing itself is in 03_parse_document.py — we keep fetch and parse
separate so a parser bug doesn't require re-fetching.

Run:
    python 02_fetch_ato.py                  # process the queue
    python 02_fetch_ato.py --batch-size 50  # how many per run (rate-limit yourself)
    python 02_fetch_ato.py --use-playwright # force Playwright path
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import random
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from supabase import create_client

log = logging.getLogger("fetch")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15"
)
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
    # `br` (Brotli) ONLY if the `brotli` Python package is installed (httpx
    # delegates decompression to it). It's now pinned in requirements.txt —
    # but if it's ever uninstalled, this line means httpx is still asked for
    # gzip/deflate and won't silently mangle responses.
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "DNT": "1",
}

# Fail loud if brotli isn't importable. Without it, httpx will return raw
# brotli bytes mangled as UTF-8 for every doc on the ATO Akamai edge, which
# silently corrupts the corpus (discovered 2026-05-21).
try:
    import brotli  # noqa: F401
except ImportError as e:
    raise SystemExit(
        "FATAL: `brotli` package is required for httpx to decompress ATO's "
        "Content-Encoding: br responses. Run `pip install brotli` (or "
        "`pip install -r requirements.txt`)."
    ) from e

# Be a good citizen. The ATO site is government-funded infrastructure.
POLITE_DELAY_MIN = 1.5
POLITE_DELAY_MAX = 3.5
MAX_ATTEMPTS = 4

# If a previous run crashed between marking a row 'in_progress' and finalising
# it as 'done' / 'failed', the row stays stuck and won't be picked up by the
# `status=pending` query. On startup we sweep any rows whose last_attempt_at
# is older than this threshold and reset them to pending.
STUCK_IN_PROGRESS_MINUTES = 30

STORAGE_BUCKET = "ato-raw-html"   # create this in Supabase Storage


@dataclass
class FetchResult:
    docid: str
    html: Optional[str]
    status_code: int
    used_playwright: bool
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Fast path (httpx)
# ---------------------------------------------------------------------------
def fetch_one_httpx(client: httpx.Client, url: str) -> tuple[int, Optional[str]]:
    try:
        resp = client.get(url, follow_redirects=True)
        if resp.status_code == 200 and "text/html" in resp.headers.get("content-type", ""):
            return 200, resp.text
        return resp.status_code, None
    except httpx.HTTPError as e:
        log.warning("httpx error for %s: %s", url, e)
        return 0, None


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------
# Note on the removed Playwright fallback (2026-05-21):
#
# Earlier versions of this fetcher fell back to a headless-Chromium request
# (via Playwright) whenever the httpx fast path returned 403 or 429, on the
# theory (from HANDOFF §3.6) that a "real browser" could get through where
# httpx couldn't. Empirically — across 15,241 docs in our first real run —
# Akamai's fingerprint detection 403s the headless-Chromium attempt every
# time, regardless of UA string. Honest mitigations (matching the Chromium
# UA to the Chromium binary) didn't move the needle; dishonest ones
# (--disable-blink-features=AutomationControlled and similar webdriver-flag
# hides) would cross the line our ethics doc draws against anti-detection
# tricks.
#
# So the fallback is retired. The fetcher uses httpx only. When httpx
# returns 403/429, we mark the row failed with the original status; the
# operator can revisit later (manual ATO outreach is the documented
# escalation path).
#
# Playwright stays installed for diagnostic / future use but isn't on the
# automatic critical path.

def politely_sleep() -> None:
    time.sleep(random.uniform(POLITE_DELAY_MIN, POLITE_DELAY_MAX))


def fetch_with_fallback(
    client: httpx.Client, docid: str, url: str, force_playwright: bool = False
) -> FetchResult:
    # `force_playwright` kept in the signature for backwards-compat with
    # invocations that pass it, but the flag is now a no-op — Playwright is
    # never invoked from this script.
    last_status = 0
    for attempt in range(MAX_ATTEMPTS):
        status, html = fetch_one_httpx(client, url)
        last_status = status
        if status == 200 and html:
            return FetchResult(docid, html, status, False)
        if status in (403, 429):
            # Akamai bot-block or rate-limit. No fallback path that respects
            # our ethics doc would recover this. Mark failed and move on.
            return FetchResult(
                docid, None, status, False,
                error=f"HTTP {status} (Akamai block — no Playwright fallback per ethics policy)",
            )
        if 500 <= status < 600:
            # Server hiccup — backoff and try again.
            time.sleep(2 ** attempt)
            continue
        break
    return FetchResult(docid, None, last_status, False, error=f"HTTP {last_status}")


# ---------------------------------------------------------------------------
# Storage and DB writes
# ---------------------------------------------------------------------------
def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def store_raw_html(supabase, docid: str, html: str, version: int) -> str:
    """Upload raw HTML to Supabase Storage and return the path."""
    safe = docid.replace("/", "_")
    path = f"{safe}/v{version:04d}.html"
    supabase.storage.from_(STORAGE_BUCKET).upload(
        path,
        html.encode("utf-8"),
        {"contentType": "text/html", "upsert": "true"},
    )
    return path


def upsert_document_and_version(supabase, fr: FetchResult, queue_row: dict) -> None:
    assert fr.html is not None
    html_hash = sha256(fr.html)

    # Look up existing document by docid.
    existing = (
        supabase.table("documents")
        .select("id,source_html_hash")
        .eq("docid", fr.docid)
        .maybe_single()
        .execute()
        .data
    )

    if existing and existing["source_html_hash"] == html_hash:
        # No change — just record the fetch was attempted.
        supabase.table("fetch_queue").update(
            {"status": "done", "last_attempt_at": "now()"}
        ).eq("docid", fr.docid).execute()
        return

    if existing:
        document_id = existing["id"]
        next_version = (
            supabase.table("document_versions")
            .select("version_number", count="exact")
            .eq("document_id", document_id)
            .order("version_number", desc=True)
            .limit(1)
            .execute()
            .data
        )
        new_version_number = (next_version[0]["version_number"] + 1) if next_version else 2
        change_type = "content"
    else:
        # Minimal stub — the parser (Stage 3) will fill in title, dates, etc.
        doc_row = {
            "docid": fr.docid,
            "short_code": queue_row.get("short_code") or fr.docid,
            "doc_type": _doc_type_from_short_code(queue_row.get("short_code")),
            "title": "(pending parse)",
            "status": "current",
            "ato_url": queue_row["ato_url"],
            "austlii_url": queue_row.get("austlii_url"),
            "source_html_hash": html_hash,
            "last_fetched_at": "now()",
        }
        document_id = (
            supabase.table("documents").insert(doc_row).execute().data[0]["id"]
        )
        new_version_number = 1
        change_type = "initial"

    raw_path = store_raw_html(supabase, fr.docid, fr.html, new_version_number)

    supabase.table("document_versions").insert(
        {
            "document_id": document_id,
            "version_number": new_version_number,
            "source_html_hash": html_hash,
            "raw_html_path": raw_path,
            "change_type": change_type,
        }
    ).execute()

    supabase.table("documents").update(
        {"source_html_hash": html_hash, "last_fetched_at": "now()"}
    ).eq("id", document_id).execute()

    supabase.table("change_events").insert(
        {
            "document_id": document_id,
            "docid": fr.docid,
            "event_type": "new" if change_type == "initial" else "modified",
            "details": {"version": new_version_number},
        }
    ).execute()

    supabase.table("fetch_queue").update(
        {"status": "done", "last_attempt_at": "now()"}
    ).eq("docid", fr.docid).execute()


def _doc_type_from_short_code(short_code: Optional[str]) -> str:
    if not short_code:
        return "TR"  # fallback; parser will correct
    head = short_code.split()[0]
    return head if head in {
        "TR","TD","LCR","GSTR","GSTD","MT","SGR","SGD","CR","PR","FBTR",
        "PCG","ATO","DIS","TA","SMSFR","SMSFD","ITR",
    } else "TR"


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def reset_stuck_in_progress(supabase) -> int:
    """Reset any `in_progress` rows older than STUCK_IN_PROGRESS_MINUTES back
    to `pending` so they get retried. Run once at the start of every
    invocation — handles the case where a previous run was killed between the
    `in_progress` write and the `done` / `failed` write.
    """
    threshold = (
        datetime.now(timezone.utc) - timedelta(minutes=STUCK_IN_PROGRESS_MINUTES)
    ).isoformat()
    result = (
        supabase.table("fetch_queue")
        .update({"status": "pending"})
        .eq("status", "in_progress")
        .lt("last_attempt_at", threshold)
        .execute()
    )
    n = len(result.data or [])
    if n:
        log.info("Reset %d stuck `in_progress` rows (older than %d min) to pending", n, STUCK_IN_PROGRESS_MINUTES)
    return n


def log_status_summary(supabase) -> None:
    """One-line snapshot of fetch_queue status distribution. Uses count=exact
    with limit(1) so each request returns just the count and one row of
    payload — cheap to call after every batch. (`head=True` would skip the
    body entirely but isn't supported in the supabase==2.5.0 postgrest dep.)
    """
    counts: dict[str, int] = {}
    for status in ("done", "failed", "pending", "in_progress"):
        try:
            res = (
                supabase.table("fetch_queue")
                .select("docid", count="exact")
                .eq("status", status)
                .limit(1)
                .execute()
            )
            counts[status] = res.count or 0
        except Exception as e:
            log.warning("Could not count status=%s: %s", status, e)
            counts[status] = -1
    log.info(
        "Batch complete: %d done, %d failed, %d pending, %d in_progress",
        counts["done"], counts["failed"], counts["pending"], counts["in_progress"],
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--batch-size", type=int, default=100)
    p.add_argument("--use-playwright", action="store_true")
    args = p.parse_args()

    supabase = create_client(
        os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    )

    reset_stuck_in_progress(supabase)

    queue = (
        supabase.table("fetch_queue")
        .select("*")
        .eq("status", "pending")
        .order("priority")
        .order("scheduled_for")
        .limit(args.batch_size)
        .execute()
        .data
    )
    log.info("Picked up %d queue rows", len(queue))

    # Optional From header for ATO operators to identify / contact us. Set
    # FETCH_CONTACT_EMAIL in .env.local once decided; absent means we run with
    # the User-Agent alone (still polite, just unidentified).
    headers = dict(DEFAULT_HEADERS)
    contact = os.environ.get("FETCH_CONTACT_EMAIL", "").strip()
    if contact:
        headers["From"] = contact
        log.info("Identifying via From: %s", contact)

    current_row_id: Optional[str] = None
    try:
        with httpx.Client(headers=headers, timeout=30.0) as client:
            for row in queue:
                current_row_id = row["id"]
                # Any non-Interrupt exception inside this block must NOT take
                # down the batch. We've already seen a storage3
                # UnboundLocalError on a transient Storage upload — without
                # this guard, a single library bug or network hiccup wipes
                # out an entire batch worth of polite walking.
                try:
                    supabase.table("fetch_queue").update(
                        {"status": "in_progress", "attempt_count": row["attempt_count"] + 1}
                    ).eq("id", row["id"]).execute()

                    fr = fetch_with_fallback(
                        client, row["docid"], row["ato_url"],
                        force_playwright=args.use_playwright,
                    )

                    if fr.html:
                        upsert_document_and_version(supabase, fr, row)
                        log.info("OK %s (playwright=%s)", fr.docid, fr.used_playwright)
                    else:
                        supabase.table("fetch_queue").update(
                            {"status": "failed", "last_error": fr.error or f"HTTP {fr.status_code}"}
                        ).eq("id", row["id"]).execute()
                        log.warning("FAIL %s (%s)", fr.docid, fr.error)
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    err_label = f"{type(e).__name__}: {e}"[:500]
                    log.exception("Skipping %s after exception: %s", row["docid"], err_label)
                    try:
                        supabase.table("fetch_queue").update(
                            {"status": "failed", "last_error": err_label}
                        ).eq("id", row["id"]).execute()
                    except Exception:
                        log.warning(
                            "Could not mark %s as failed; will be reset by next "
                            "reset_stuck_in_progress sweep.", row["docid"],
                        )

                current_row_id = None
                politely_sleep()
    except KeyboardInterrupt:
        log.warning("Interrupted. Releasing the in-flight row back to pending.")
        if current_row_id is not None:
            try:
                supabase.table("fetch_queue").update({"status": "pending"}).eq("id", current_row_id).execute()
            except Exception:
                pass
        raise
    finally:
        log_status_summary(supabase)

    return 0


if __name__ == "__main__":
    sys.exit(main())
