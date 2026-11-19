"""
04_monitor_daily.py
===================
Stage 4: Daily change detection + ATO-side visibility tracking (ADR-0007).

Runs on a schedule (GitHub Actions cron, ~06:00 AEST). Three responsibilities:

    1. Re-enumerate the ATO API (subprocess Stage 1a). This inserts any newly
       published docids into `documents` + `fetch_queue` and — crucially —
       refreshes `last_seen_in_search = now()` for every doc still visible in
       ATO search (Stage 1a's flush_buffers does this; ADR-0007).

    2. Visibility sweep (ADR-0007). Compare `last_seen_in_search` against a
       3-day cutoff:
         - not seen for 3+ days  → mark `removed_from_search_at`, probe the
           canonical URL, emit `removed_from_search` (+ `became_inaccessible`).
         - reappeared after being marked removed → clear the flag, emit
           `restored_to_search` (+ `restored_accessible`).

    3. Re-queue current/draft docs for a light hash re-fetch so the fetcher
       can detect content changes.

SAFETY: the sweep runs ONLY after a successful enumeration in this same
invocation. If enumeration fails (e.g. ATO API hiccup) `last_seen_in_search`
is not refreshed, and sweeping stale data would wrongly flag the whole corpus
as removed. So a failed enumeration aborts before the sweep and exits non-zero
(the workflow goes red and a human investigates). `--skip-enumeration`
therefore also skips the sweep unless `--force-sweep` is passed.

Run:
    python 04_monitor_daily.py                  # enumerate → sweep → re-queue
    python 04_monitor_daily.py --dry-run        # report only, no writes
    python 04_monitor_daily.py --skip-enumeration --dry-run   # inspect sweep
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import httpx
from supabase import create_client

# Shared resilient-upsert helper (lives in ../scrapers/_db.py).
sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scrapers"),
)
from _db import safe_upsert  # noqa: E402

log = logging.getLogger("monitor")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENUMERATOR = os.path.join(REPO_ROOT, "scrapers", "01_enumerate_ato.py")

# ADR-0007: 3 consecutive missed daily walks is a strong "removed" signal and
# debounces transient ATO API hiccups.
REMOVED_AFTER = timedelta(days=3)
# Cap URL probes per run so a large transition batch can't turn into thousands
# of outbound requests. Leftover transitions get probed on subsequent runs.
MAX_URL_PROBES = 200
ZERO_UUID = "00000000-0000-0000-0000-000000000000"


def start_run(supabase) -> str:
    row = supabase.table("crawl_runs").insert({
        "source": "ato",
        "mode": "monitor",
    }).execute().data[0]
    return row["id"]


def finish_run(supabase, run_id: str, **counts) -> None:
    supabase.table("crawl_runs").update({
        "finished_at": datetime.now(timezone.utc).isoformat(),
        **counts,
    }).eq("id", run_id).execute()


def enumerate_fresh(supabase) -> tuple[bool, int]:
    """Run Stage 1a as a subprocess. Returns (ok, pending_count).

    Stage 1a inserts new docids (ignore_duplicates) and touches
    `last_seen_in_search` for every doc still visible in ATO search. We run it
    out-of-process so its asyncio walk + own logging stay isolated from the
    monitor.
    """
    log.info("Running ATO enumeration (Stage 1a)…")
    proc = subprocess.run(
        [sys.executable, ENUMERATOR],
        cwd=REPO_ROOT,
        env=os.environ.copy(),
    )
    if proc.returncode != 0:
        log.error("Enumeration FAILED (exit %d). Skipping visibility sweep so "
                  "stale last_seen_in_search can't false-flag the corpus.",
                  proc.returncode)
        return False, 0
    pending = (
        supabase.table("fetch_queue")
        .select("id", count="exact").limit(1)
        .eq("status", "pending")
        .execute().count
    )
    return True, pending or 0


def _probe_url(client: httpx.Client, url: str | None) -> bool | None:
    """Single polite GET. True=reachable, False=dead, None=unknown/no url."""
    if not url:
        return None
    try:
        r = client.get(url, follow_redirects=True, timeout=15.0)
        return 200 <= r.status_code < 400
    except httpx.HTTPError:
        return False


def _paginate(query_fn) -> list[dict]:
    """Cursor-paginate a documents query by id to clear the 1000-row cap."""
    out: list[dict] = []
    last = ZERO_UUID
    while True:
        page = query_fn(last)
        if not page:
            break
        out.extend(page)
        last = page[-1]["id"]
    return out


def visibility_sweep(supabase, dry_run: bool) -> dict:
    """Mark docs removed-from / restored-to ATO search, probe URLs, emit events.

    Must run only after a successful enumeration (see module docstring)."""
    now = datetime.now(timezone.utc)
    cutoff = (now - REMOVED_AFTER).isoformat()
    now_iso = now.isoformat()

    # 1. Newly removed: stale last_seen, not already flagged.
    newly_removed = _paginate(lambda last: (
        supabase.table("documents")
        .select("id,docid,ato_url,ato_url_accessible")
        .lt("last_seen_in_search", cutoff)
        .is_("removed_from_search_at", "null")
        .gt("id", last).order("id").limit(1000)
        .execute().data
    ))
    # 2. Restored: previously flagged removed but seen again within the window.
    restored = _paginate(lambda last: (
        supabase.table("documents")
        .select("id,docid,ato_url,ato_url_accessible")
        .gte("last_seen_in_search", cutoff)
        .not_.is_("removed_from_search_at", "null")
        .gt("id", last).order("id").limit(1000)
        .execute().data
    ))

    log.info("Visibility sweep: %d newly removed, %d restored (cutoff %s)",
             len(newly_removed), len(restored), cutoff)
    if dry_run:
        for r in newly_removed[:10]:
            log.info("  [dry-run] would mark removed: %s", r["docid"])
        for r in restored[:10]:
            log.info("  [dry-run] would mark restored: %s", r["docid"])
        return {"removed": len(newly_removed), "restored": len(restored), "dry_run": True}

    events: list[dict] = []
    probes = 0
    with httpx.Client(headers={"User-Agent": "ato-legal-db-pipeline-monitor/1.0 (+ATO legal DB freshness check)"}) as client:
        # --- removed transitions ---
        for r in newly_removed:
            supabase.table("documents").update({
                "removed_from_search_at": now_iso,
            }).eq("id", r["id"]).execute()
            events.append({"document_id": r["id"], "docid": r["docid"],
                           "event_type": "removed_from_search",
                           "details": {"cutoff": cutoff}})
            if probes < MAX_URL_PROBES:
                probes += 1
                reachable = _probe_url(client, r.get("ato_url"))
                if reachable is not None:
                    supabase.table("documents").update({
                        "ato_url_accessible": reachable,
                        "ato_url_last_checked_at": now_iso,
                    }).eq("id", r["id"]).execute()
                    # Emit an accessibility transition only when it changed.
                    if reachable != (r.get("ato_url_accessible") is not False):
                        events.append({
                            "document_id": r["id"], "docid": r["docid"],
                            "event_type": "restored_accessible" if reachable else "became_inaccessible",
                            "details": {"ato_url": r.get("ato_url")},
                        })

        # --- restored transitions ---
        for r in restored:
            supabase.table("documents").update({
                "removed_from_search_at": None,
            }).eq("id", r["id"]).execute()
            events.append({"document_id": r["id"], "docid": r["docid"],
                           "event_type": "restored_to_search", "details": {}})

    if events:
        ok, bad = safe_upsert(supabase, "change_events", events)
        log.info("  emitted %d visibility change_events (%d failed)", ok, bad)
    log.info("  probed %d URLs", probes)
    return {"removed": len(newly_removed), "restored": len(restored),
            "events": len(events), "probes": probes}


def queue_hash_check(supabase, max_per_run: int = 5000) -> int:
    """Re-queue current/draft docs for a light re-fetch so the fetcher can
    hash-detect content changes. The ATO doesn't set reliable Last-Modified /
    ETag headers, so we just re-queue at low priority and hash on retrieval."""
    docs = (
        supabase.table("documents")
        .select("docid,ato_url,austlii_url,short_code,status")
        .in_("status", ["current", "draft"])
        .limit(max_per_run)
        .execute().data
    )
    rows = [
        {
            "docid": d["docid"],
            "ato_url": d["ato_url"],
            "austlii_url": d["austlii_url"],
            "short_code": d["short_code"],
            "priority": 500,  # lower priority than backfill / refetch items
            "status": "pending",
        }
        for d in docs
    ]
    if rows:
        ok, bad = safe_upsert(supabase, "fetch_queue", rows, on_conflict="docid")
        log.info("  fetch_queue (hash-check re-queue): %d succeeded, %d skipped", ok, bad)
    return len(rows)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--skip-enumeration", action="store_true",
                   help="Skip Stage 1a (also skips the sweep unless --force-sweep).")
    p.add_argument("--force-sweep", action="store_true",
                   help="Run the visibility sweep even without a fresh enumeration. "
                        "DANGEROUS on stale last_seen_in_search — only for testing.")
    p.add_argument("--skip-hash-check", action="store_true",
                   help="Skip re-queuing current/draft docs for hash recheck.")
    p.add_argument("--dry-run", action="store_true",
                   help="Report sweep actions without writing.")
    args = p.parse_args()

    supabase = create_client(
        os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    )

    run_id = start_run(supabase)
    discovered = 0
    enumeration_ok = False

    if not args.skip_enumeration:
        enumeration_ok, discovered = enumerate_fresh(supabase)
        log.info("Pending after enumeration: %d", discovered)
        if not enumeration_ok:
            finish_run(supabase, run_id, documents_discovered=0,
                       documents_changed=0, documents_failed=1)
            return 1  # workflow goes red; sweep deliberately not run

    # CRITICAL GUARD (added after the 2026-05-25 incident): a clean exit code
    # from the enumerator is NOT enough — if the walk completed but didn't
    # actually refresh last_seen_in_search (it refreshed 0 docs that run), the
    # whole corpus looks "stale" and the sweep flags everything as removed.
    # So before any *real* sweep, require last_seen to be broadly fresh.
    if (enumeration_ok or args.force_sweep) and not args.dry_run and not args.force_sweep:
        total = supabase.table("documents").select("id", count="exact").limit(1).execute().count or 0
        fresh_cutoff = (datetime.now(timezone.utc) - timedelta(hours=18)).isoformat()
        fresh = (supabase.table("documents").select("id", count="exact").limit(1)
                 .gte("last_seen_in_search", fresh_cutoff).execute().count or 0)
        if total == 0 or fresh < 0.5 * total:
            log.error("Visibility sweep ABORTED: only %d/%d docs have a fresh "
                      "last_seen_in_search — enumeration did not refresh visibility. "
                      "Not flagging any removals (would mass-false-flag the corpus).",
                      fresh, total)
            finish_run(supabase, run_id, documents_discovered=discovered,
                       documents_changed=0, documents_failed=1)
            return 1

    sweep_stats = {}
    if enumeration_ok or args.force_sweep:
        sweep_stats = visibility_sweep(supabase, dry_run=args.dry_run)
    else:
        log.info("Skipping visibility sweep (no fresh enumeration; "
                 "pass --force-sweep to override).")

    rechecked = 0
    if not args.skip_hash_check and not args.dry_run:
        rechecked = queue_hash_check(supabase)
        log.info("Queued %d existing docs for hash recheck", rechecked)

    # Rebuild the composite authority score (mv_document_authority) off the
    # latest citation graph + statuses. Cheap + non-fatal — a failure here must
    # not red the monitor run, so log and continue.
    if not args.dry_run:
        try:
            supabase.rpc("fn_refresh_authority").execute()
            log.info("Refreshed mv_document_authority")
        except Exception as e:
            log.warning("Authority refresh failed (non-fatal): %s", str(e)[:200])

    finish_run(
        supabase, run_id,
        documents_discovered=discovered,
        documents_changed=sweep_stats.get("removed", 0) + sweep_stats.get("restored", 0),
        documents_failed=0,
    )
    log.info("Done. sweep=%s", sweep_stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
