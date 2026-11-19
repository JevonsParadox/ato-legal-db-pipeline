"""
backfill_status_from_shortcode.py
=================================
One-off data fix: `documents.status` is uniformly 'current' for all ~15k docs
(the Stage-1a enumerator's placeholder; the parser never persisted the real
status). Derive a correct status from the `short_code` editorial marker and
backfill it.

This is the cheap, high-coverage half of the fix (handles the ~6,760 docs whose
short_code carries a marker). The durable half is parsers/03_parse_document.py
now persisting the ATO-HTML status going forward (so a future re-parse corrects
any withdrawn docs that lack a title marker). See HANDOFF Pending.

Mapping (first match wins):
    "... (Withdrawn)"  or  "...<digit>W"   -> withdrawn
    "... (Replaced)"                        -> replaced
    "... (Finalised)"  (a draft whose final issued) -> replaced
    is_draft (no marker above)              -> draft
    else                                    -> current  (unchanged)

Status codes are constrained by the `document_statuses` FK lookup
(current/draft/replaced/withdrawn/archived/consolidated — note NO 'superseded').
A finalised draft is "replaced" by its final; statusPill renders that with the
grey superseded tone.

Idempotent: only rows whose computed status differs are updated, in batched
UPDATEs keyed on id (NOT upsert — a partial-column upsert fails the NOT-NULL
short_code constraint; see the enumerator's _touch_last_seen for the same trap).

Run:
    python scripts/backfill_status_from_shortcode.py --dry-run
    python scripts/backfill_status_from_shortcode.py
"""
from __future__ import annotations

import argparse
import logging
import os
import re
from collections import Counter

from supabase import create_client

log = logging.getLogger("backfill_status")

_WITHDRAWN_RE = re.compile(r"\(withdrawn\)\s*$", re.IGNORECASE)
_W_SUFFIX_RE = re.compile(r"[0-9]W$")
_REPLACED_RE = re.compile(r"\(replaced\)\s*$", re.IGNORECASE)
_FINALISED_RE = re.compile(r"\(finalised\)\s*$", re.IGNORECASE)


def derive_status(short_code: str | None, is_draft: bool) -> str:
    sc = short_code or ""
    if _WITHDRAWN_RE.search(sc) or _W_SUFFIX_RE.search(sc):
        return "withdrawn"
    if _REPLACED_RE.search(sc):
        return "replaced"
    if _FINALISED_RE.search(sc):
        return "replaced"
    if is_draft:
        return "draft"
    return "current"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="Report changes without writing.")
    args = p.parse_args()

    supabase = create_client(
        os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    )

    # Paginate all docs by id (1000-row PostgREST cap).
    rows: list[dict] = []
    last = "00000000-0000-0000-0000-000000000000"
    while True:
        page = (
            supabase.table("documents")
            .select("id,short_code,is_draft,status")
            .gt("id", last).order("id").limit(1000)
            .execute().data
        )
        if not page:
            break
        rows.extend(page)
        last = page[-1]["id"]

    before = Counter(r["status"] for r in rows)
    # id buckets per target status, only where it changes.
    to_update: dict[str, list[str]] = {}
    after = Counter()
    for r in rows:
        target = derive_status(r["short_code"], r["is_draft"])
        after[target] += 1
        if target != r["status"]:
            to_update.setdefault(target, []).append(r["id"])

    total_changes = sum(len(v) for v in to_update.values())
    log.info("Scanned %d docs.", len(rows))
    log.info("Status BEFORE: %s", dict(before))
    log.info("Status AFTER:  %s", dict(after))
    log.info("Rows to change: %d  (%s)",
             total_changes, {k: len(v) for k, v in to_update.items()})

    if args.dry_run:
        log.info("[dry-run] no writes.")
        return 0

    updated = 0
    for status, ids in to_update.items():
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            try:
                resp = (
                    supabase.table("documents")
                    .update({"status": status})
                    .in_("id", chunk)
                    .execute()
                )
                updated += len(resp.data or [])
            except Exception as e:  # noqa: BLE001 — log + continue
                log.error("Update to %s failed for %d ids (%s…): %s",
                          status, len(chunk), chunk[0], e)
    log.info("Done. Updated %d rows.", updated)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
