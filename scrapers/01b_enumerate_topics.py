"""
01b_enumerate_topics.py — Stage 1b: Topic tree walk.

The ATO Legal Database exposes two browse axes: Type and Topic. The Type tree
gives us doc_type/year/number (TR 2024/1, GSTR 2025/3, etc.) and we walked it
in 01_enumerate_ato.py. The Topic tree gives us subject classifications —
"Income tax > Deductions > Personal services income", "GST > Supplies > Food",
and so on. Each document can appear under multiple topic paths.

Endpoint shape is identical to the Type tree:

    GET /API/v1/law/lawservices/browse-content/?Mode=topic            # root
    GET /API/v1/law/lawservices/browse-content/?{node.data.url}       # children

Folder nodes have `folder: true, lazy: true`. Leaves have `a_attr.href`
containing a docid as a query parameter — same docids the Type tree produced.

Side effects:
  - Upserts every folder node as a row in `topics`, keyed by the full path[].
  - Records every (docid, topic_path) leaf and bulk-inserts the resulting
    (document_id, topic_id) pairs into `document_topics`.
  - Does NOT touch `documents` or `fetch_queue`. Assumes the Type enumerator
    has already populated those. If a Topic-tree leaf references a docid we
    haven't seen, we log it and skip rather than fabricate a documents row
    (the Type walk is canonical).

Run order: 01_enumerate_ato.py first (Type → documents + fetch_queue),
           then 01b_enumerate_topics.py (Topic → topics + document_topics).
"""

import argparse
import asyncio
import logging
import os
import re
import sys
import time
from typing import Optional

import httpx
from supabase import create_client

# Shared resilient-upsert helper (see scrapers/_db.py)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _db import safe_upsert  # noqa: E402

# ----------------------------------------------------------------------------
# Config — mirrors 01_enumerate_ato.py for consistency
# ----------------------------------------------------------------------------
API_ROOT = "https://www.ato.gov.au/API/v1/law/lawservices/browse-content/"
TOPIC_SEED = "Mode=topic"
CONCURRENCY = 4
REQUEST_DELAY_S = 0.3
HTTP_TIMEOUT = 30.0

# Match 01_enumerate_ato.py exactly. The ATO API filters on User-Agent —
# the prior `ATOLegalIndexer/1.0` UA gets HTTP 403 on `?Mode=topic` even
# though `?Mode=type` worked because 01a presents a Safari UA. Keep these
# two enumerators' headers in lockstep until / unless we have a reason to
# diverge.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15"
)
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-AU,en;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.ato.gov.au/single-page-applications/legaldatabase",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("enumerate_topics")

_DOCID_FROM_HREF_RE = re.compile(r"docid=([^&]+)")


# ----------------------------------------------------------------------------
# Tree walk — same shape as the Type enumerator
# ----------------------------------------------------------------------------

class TopicNode:
    """An in-flight node during the walk: tracks raw payload + breadcrumb."""
    __slots__ = ("raw", "breadcrumb")

    def __init__(self, raw: dict, breadcrumb: list[str]):
        self.raw = raw
        self.breadcrumb = breadcrumb         # ["Income tax", "Deductions", ...]

    @property
    def title(self) -> str:
        return self.raw.get("title", "")

    @property
    def is_folder(self) -> bool:
        return bool(self.raw.get("folder"))

    @property
    def child_url_param(self) -> Optional[str]:
        return (self.raw.get("data") or {}).get("url")


async def fetch_children(client, sem, url_param: Optional[str]) -> list[dict]:
    """One GET against browse-content/. None means the seed (Mode=topic)."""
    async with sem:
        await asyncio.sleep(REQUEST_DELAY_S)
        url = API_ROOT + ("?" + (url_param or TOPIC_SEED))
        r = await client.get(url)
        r.raise_for_status()
        return r.json()


async def walk(client, sem, parent_breadcrumb, url_param,
               on_folder, on_leaf) -> int:
    """Depth-first traversal. Returns total leaves visited."""
    children = await fetch_children(client, sem, url_param)
    total = 0

    folder_tasks = []
    for raw in children:
        node = TopicNode(raw, parent_breadcrumb + [raw.get("title", "")])
        if node.is_folder:
            on_folder(node)
            folder_tasks.append(
                walk(client, sem, node.breadcrumb, node.child_url_param,
                     on_folder, on_leaf)
            )
        else:
            on_leaf(node)
            total += 1

    if folder_tasks:
        results = await asyncio.gather(*folder_tasks)
        total += sum(results)
    return total


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

async def main_async(args: argparse.Namespace) -> int:
    supabase = None
    if not args.dry_run:
        supabase = create_client(
            os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"]
        )

    # Collected as we walk:
    #   topics_seen: path tuple -> {name, parent_path, display_order}
    #   leaf_pairs:  list of (docid, topic_path_tuple)
    topics_seen: dict[tuple, dict] = {}
    leaf_pairs: list[tuple[str, tuple]] = []

    def on_folder(node: TopicNode) -> None:
        path = tuple(node.breadcrumb)
        if path in topics_seen:
            return
        topics_seen[path] = {
            "name": node.title,
            "parent_path": tuple(node.breadcrumb[:-1]) if len(path) > 1 else None,
            "display_order": len(topics_seen),
        }
        if args.dry_run and len(topics_seen) <= 10:
            indent = "  " * (len(path) - 1)
            print(f"  TOPIC{indent} {' > '.join(path)}")

    def on_leaf(node: TopicNode) -> None:
        href = (node.raw.get("a_attr") or {}).get("href") or ""
        m = _DOCID_FROM_HREF_RE.search(href)
        if not m:
            log.warning("Leaf missing docid: %r", node.title)
            return
        docid = m.group(1)
        # Parent breadcrumb is the topic path; node.title itself is just the doc.
        topic_path = tuple(node.breadcrumb[:-1])
        leaf_pairs.append((docid, topic_path))

    sem = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=HTTP_TIMEOUT) as client:
        start = time.time()
        total_leaves = await walk(client, sem, [], None, on_folder, on_leaf)
        elapsed = time.time() - start

    log.info(
        "Topic walk complete: %d topics, %d leaf occurrences in %.1fs",
        len(topics_seen), total_leaves, elapsed,
    )

    if args.dry_run or not supabase:
        return 0

    # ----- Persist topics with parent linkage (two passes for FK integrity) -----
    # First pass: insert all topics without parent_id (parent_id may not exist yet
    # because we don't sort by depth). We'll set parent_id in pass 2.
    log.info("Upserting %d topic rows...", len(topics_seen))
    topic_rows = [
        {"name": meta["name"], "path": list(path), "display_order": meta["display_order"]}
        for path, meta in topics_seen.items()
    ]
    ok, bad = safe_upsert(supabase, "topics", topic_rows, on_conflict="path")
    log.info("  topics: %d succeeded, %d skipped", ok, bad)

    # Pass 2: resolve parent_id for each topic with a non-empty parent_path.
    log.info("Linking topic parents...")
    # Fetch back the (path -> id) map. PostgREST caps unbounded SELECTs at
    # 1000 rows, so we cursor-paginate by `id` to be safe for any topic
    # tree size. A naive `.select("id,path").execute()` truncated to 1000
    # rows on the first run with 1115 topics and KeyError'd on the missing
    # 115 in this same loop.
    path_to_id: dict[tuple, str] = {}
    last_id = "00000000-0000-0000-0000-000000000000"
    while True:
        page = (supabase.table("topics")
                .select("id,path").gt("id", last_id)
                .order("id").limit(1000).execute().data)
        if not page:
            break
        for r in page:
            path_to_id[tuple(r["path"])] = r["id"]
        last_id = page[-1]["id"]
    log.info("  loaded %d topic paths for parent-link resolution", len(path_to_id))
    parent_updates = []
    for path, meta in topics_seen.items():
        if meta["parent_path"] is None:
            continue
        parent_id = path_to_id.get(meta["parent_path"])
        if not parent_id:
            log.warning("Parent path not found for %s", path)
            continue
        topic_id = path_to_id.get(path)
        if not topic_id:
            log.warning("Topic path not found after upsert: %s", path)
            continue
        # Include the full row (not just id+parent_id) so safe_upsert can use
        # on_conflict="id" cleanly — UPSERT with partial rows fails the NOT
        # NULL constraint on `name` (which is what the prior `{id, parent_id}`
        # payload tripped silently via safe_upsert's per-row fallback).
        parent_updates.append({
            "id": topic_id,
            "name": meta["name"],
            "path": list(path),
            "display_order": meta["display_order"],
            "parent_id": parent_id,
        })
    if parent_updates:
        ok, bad = safe_upsert(
            supabase, "topics", parent_updates, on_conflict="id",
        )
        log.info("  topic parent links: %d succeeded, %d skipped", ok, bad)

    # ----- Join leaf docids to topic ids and persist document_topics -----
    # Load the full docid → id map via cursor pagination. The earlier
    # implementation chunked unique docids into 1000-element IN-clauses; each
    # `.in_("docid", chunk)` produced a ~30 KB query string that exceeded
    # PostgREST's request-line limit and returned 400 Bad Request. Cursor-
    # paginating over the documents table sidesteps URL-length entirely.
    log.info("Loading docid → id map for %d leaf occurrences...", len(leaf_pairs))
    docid_to_id: dict[str, str] = {}
    last_id = "00000000-0000-0000-0000-000000000000"
    while True:
        page = (supabase.table("documents")
                .select("id,docid").gt("id", last_id)
                .order("id").limit(1000).execute().data)
        if not page:
            break
        for row in page:
            docid_to_id[row["docid"]] = row["id"]
        last_id = page[-1]["id"]
    log.info("  loaded %d docids", len(docid_to_id))

    missing = sum(1 for d, _ in leaf_pairs if d not in docid_to_id)
    if missing:
        log.warning(
            "%d topic-tree leaves reference docids not in `documents`. "
            "Run 01_enumerate_ato.py first to populate the canonical catalogue.",
            missing,
        )

    join_rows = []
    seen_pairs = set()
    for docid, topic_path in leaf_pairs:
        doc_id = docid_to_id.get(docid)
        topic_id = path_to_id.get(topic_path)
        if not (doc_id and topic_id):
            continue
        key = (doc_id, topic_id)
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        join_rows.append({"document_id": doc_id, "topic_id": topic_id})

    log.info("Inserting %d document_topics rows...", len(join_rows))
    ok, bad = safe_upsert(
        supabase, "document_topics", join_rows,
        on_conflict="document_id,topic_id", ignore_duplicates=True,
    )
    log.info("  document_topics: %d succeeded, %d skipped", ok, bad)

    log.info("Done.")
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true",
                   help="Walk the tree and print structure but write nothing.")
    args = p.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
