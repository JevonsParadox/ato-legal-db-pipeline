"""
04_embed_paragraphs.py
======================
Phase B / B2-α: generate paragraph embeddings (1536d) for every row in
`document_paragraphs` that doesn't have one yet.

Model: OpenAI `text-embedding-3-small` (matches the schema's `vector(1536)`).
Cost: ~$0.02 per 1M tokens → ~$1.30 for the full ~422k-paragraph corpus
(avg ~150 tokens/paragraph). Resumable: the `embedding IS NULL`
predicate is the cursor; killing + restarting the script picks up where it
left off.

Run:
    pip install -r requirements.txt              # picks up openai==1.55.0
    export OPENAI_API_KEY=sk-...
    set -a && . ./.env.local && set +a           # SUPABASE_URL + SERVICE_ROLE_KEY
    python parsers/04_embed_paragraphs.py        # full pass
    python parsers/04_embed_paragraphs.py --limit 1000   # smoke test first

A first batch of 100 takes ~5–10s; the full corpus is ~30–60 min depending
on OpenAI's rate-limit headroom. The script respects the rate-limit headers
that come back on 429 / 5xx with a bounded exponential backoff.

After completion: re-tune the ivfflat `lists` parameter on
`idx_paragraphs_embedding` (rule of thumb sqrt(rowcount); see
ato-app/docs/perf-runbook.md for the procedure).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import Iterable

from openai import OpenAI, RateLimitError, APIError
from supabase import create_client

log = logging.getLogger("embed")

MODEL = "text-embedding-3-small"
DIM = 1536
BATCH_SIZE = 100      # OpenAI accepts up to 2048 inputs/call; 100 is a good
                      # latency/recovery trade-off — a 429 mid-batch loses 100
                      # rows of work, not 2k.
PAGE_SIZE = 1000      # how many rows we pull from Supabase per round trip.
MAX_CHARS = 8000      # text-embedding-3-small accepts up to 8192 tokens; cap
                      # at 8k characters defensively (one para is usually
                      # < 1k chars; outliers truncate rather than throw).


def _batched(seq: list, n: int) -> Iterable[list]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _embed(client: OpenAI, texts: list[str]) -> list[list[float]]:
    """One embedding call with bounded exponential backoff on 429/5xx."""
    delay = 1.0
    for attempt in range(6):
        try:
            resp = client.embeddings.create(model=MODEL, input=texts)
            return [d.embedding for d in resp.data]
        except RateLimitError as e:
            log.warning("rate-limited (attempt %d): %s — sleeping %.1fs",
                        attempt + 1, e, delay)
            time.sleep(delay)
            delay = min(delay * 2, 32.0)
        except APIError as e:
            if attempt >= 4:
                raise
            log.warning("API error (attempt %d): %s — sleeping %.1fs",
                        attempt + 1, e, delay)
            time.sleep(delay)
            delay = min(delay * 2, 16.0)
    raise RuntimeError(f"embedding failed after retries on a {len(texts)}-batch")


def _is_transient_postgrest_error(e: Exception) -> bool:
    """Cloudflare 520 (Supabase edge hiccup), PGRST002 (PostgREST schema
    cache reload after a migration), and 57014 (statement_timeout — often
    transient when planner stats go stale during a bulk load or autovacuum
    is running) are all worth retrying. Real errors reraise."""
    s = str(e)
    return ("'code': 520" in s
            or "'code': '520'" in s
            or "PGRST002" in s
            or "schema cache" in s
            or "57014" in s
            or "statement timeout" in s)


def _supabase_call_with_retry(label: str, fn, max_attempts: int = 5):
    """Wrap a Supabase call with bounded exponential backoff on transient
    edge / cache errors. Reraises everything else."""
    delay = 1.0
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as e:
            if attempt < max_attempts - 1 and _is_transient_postgrest_error(e):
                log.warning("%s transient error (attempt %d/%d): %.120s — sleeping %.1fs",
                            label, attempt + 1, max_attempts, str(e), delay)
                time.sleep(delay)
                delay = min(delay * 2, 30.0)
                continue
            raise


def _next_unembedded(supabase, after_id: str, page_size: int) -> list[dict]:
    """Cursor-paginate document_paragraphs WHERE embedding IS NULL.

    PostgREST caps unbounded SELECTs at 1000; the predicate keeps the cursor
    monotonic so resuming after a restart Just Works."""
    return _supabase_call_with_retry(
        "next_unembedded",
        lambda: (
            supabase.table("document_paragraphs")
            .select("id, text")
            .is_("embedding", "null")
            .gt("id", after_id)
            .order("id")
            .limit(page_size)
            .execute().data
        ),
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=0,
                   help="Stop after this many paragraphs (smoke-test mode). "
                        "0 = no limit.")
    p.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    args = p.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        log.error("OPENAI_API_KEY not set in the environment.")
        return 2
    openai = OpenAI(api_key=api_key)

    supabase = create_client(
        os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    )

    total_done = 0
    last_id = "00000000-0000-0000-0000-000000000000"
    started = time.time()

    while True:
        page = _next_unembedded(supabase, last_id, PAGE_SIZE)
        if not page:
            log.info("Nothing left to embed.")
            break

        for batch in _batched(page, args.batch_size):
            texts = [(r["text"] or "")[:MAX_CHARS] for r in batch]
            # Guard: OpenAI rejects empty strings. Swap blanks for a single space.
            texts = [t if t.strip() else " " for t in texts]
            vecs = _embed(openai, texts)
            if len(vecs) != len(batch):
                log.error("Vector count mismatch: %d vecs vs %d rows",
                          len(vecs), len(batch))
                return 1

            # True bulk UPDATE via fn_set_paragraph_embeddings — PostgREST's
            # upsert validates NOT-NULL on the candidate INSERT tuple before
            # the ON CONFLICT arbitration, so it rejects rows with only
            # {id, embedding}. The RPC does an UPDATE … FROM jsonb on a single
            # round trip.
            payload = [
                {"id": r["id"], "embedding": v}
                for r, v in zip(batch, vecs)
            ]
            try:
                _supabase_call_with_retry(
                    "bulk_update",
                    lambda: supabase.rpc(
                        "fn_set_paragraph_embeddings", {"p_updates": payload}
                    ).execute(),
                )
            except Exception as e:
                log.exception("bulk update failed for batch ending %s: %s",
                              batch[-1]["id"], e)
                # Don't advance last_id; next iteration retries this batch.
                continue

            total_done += len(batch)
            rate = total_done / max(1, time.time() - started)
            log.info("embedded %d rows total (%.1f rows/s)", total_done, rate)
            if args.limit and total_done >= args.limit:
                log.info("hit --limit %d; stopping.", args.limit)
                return 0

        last_id = page[-1]["id"]

    elapsed = time.time() - started
    log.info("Done. %d paragraphs embedded in %.1fs.", total_done, elapsed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
