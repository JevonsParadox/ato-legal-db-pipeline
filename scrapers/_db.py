"""
Resilient Supabase write helpers used by the enumeration / monitor jobs.

Why this exists
---------------
Long-running enumerators walk the ATO API for tens of minutes, buffer
thousands of rows in memory, then upsert them in batches at the end. The
default `supabase.table(t).upsert(batch).execute()` is all-or-nothing:
a single bad row (FK violation, NOT NULL, string-too-long, weird Unicode)
raises an APIError that takes the whole batch with it and, in turn, crashes
the script. ~30 minutes of walk work is lost; nothing lands.

`safe_upsert` wraps a batched upsert so that:
  - batches that succeed land immediately,
  - a failing batch is retried row-by-row,
  - the bad row(s) are logged at ERROR with their docid (or row index)
    and skipped,
  - the function returns (succeeded_count, failed_count) so the caller can
    summarise at the end of the run.

The function never raises for a single bad row. It can still raise on
catastrophic failures (network outage, auth revoked, etc.) — those should
fail loud.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger(__name__)


def safe_upsert(
    supabase: Any,
    table: str,
    rows: list[dict],
    *,
    batch_size: int = 500,
    on_conflict: Optional[str] = None,
    ignore_duplicates: bool = False,
) -> tuple[int, int]:
    """Upsert `rows` to `table` in batches with per-row error isolation.

    Returns (succeeded_count, failed_count). Bad rows are logged at ERROR
    level with `docid` / `path` / `row#` as a label.
    """
    if not rows:
        return (0, 0)

    succeeded = 0
    failed = 0
    upsert_kwargs: dict[str, Any] = {}
    if on_conflict is not None:
        upsert_kwargs["on_conflict"] = on_conflict
    if ignore_duplicates:
        upsert_kwargs["ignore_duplicates"] = ignore_duplicates

    for batch_start in range(0, len(rows), batch_size):
        batch = rows[batch_start:batch_start + batch_size]
        try:
            supabase.table(table).upsert(batch, **upsert_kwargs).execute()
            succeeded += len(batch)
        except Exception as batch_err:
            log.warning(
                "Batch upsert to %s (rows %d-%d) failed: %s. Retrying per-row.",
                table, batch_start, batch_start + len(batch), batch_err,
            )
            for offset, row in enumerate(batch):
                try:
                    supabase.table(table).upsert([row], **upsert_kwargs).execute()
                    succeeded += 1
                except Exception as row_err:
                    failed += 1
                    label = (
                        row.get("docid")
                        or row.get("path")
                        or f"row#{batch_start + offset}"
                    )
                    log.error(
                        "Skipped bad row in %s [%s]: %s",
                        table, label, row_err,
                    )
    return (succeeded, failed)
