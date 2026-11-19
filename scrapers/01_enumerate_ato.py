"""
01_enumerate_ato.py
===================
Stage 1 (v2): Enumerate the legal database via the ATO's own JSON API.

DISCOVERY: The legal database SPA backs onto a public JSON tree-browse API at
    https://www.ato.gov.au/API/v1/law/lawservices/browse-content/

URL patterns:
    Root (top-level nodes):
        GET /API/v1/law/lawservices/browse-content/?category=E
    Children of a node:
        GET /API/v1/law/lawservices/browse-content/?Mode=type&Action=inject&TOC={path}

The TOC path is a colon-separated string like:
    "03:Public rulings:Rulings:#001#Taxation"
Numeric markers like #001# are sort-order codes inserted by the ATO; they're
part of the path string, not separate params.

We walk the tree depth-first. Folder nodes have {"folder": true, "lazy": true,
"data": {"url": "Mode=type&Action=inject&TOC=..."}}. Leaf nodes (the actual
documents) have a different shape — we discover and adapt that shape on first
contact and persist what we learn.

Output rows are upserted to fetch_queue, same as before. The rest of the
pipeline (fetcher, parser) doesn't change.

This module SUPERSEDES 01_enumerate_austlii.py once verified — keep the
AustLII script around as a fallback for cross-checking or for any doc type
the API somehow doesn't surface.

Run:
    python 01_enumerate_ato.py                        # Phase-1 roots (skips
                                                      # SKIP_ROOTS — see below)
    python 01_enumerate_ato.py --root "Practical compliance guidelines"
    python 01_enumerate_ato.py --include-deferred     # walk everything incl. EPA
    python 01_enumerate_ato.py --dry-run              # print, don't write

By default the walk SKIPS the deferred top-level roots in SKIP_ROOTS (notably
"Edited private advice", the ~401k-leaf Phase-2 bulk that blew the monitor
timeout). Cases and every Phase-1 ruling root are walked. See SKIP_ROOTS for
the maintenance rule when ingest scope changes.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import parse_qs, quote, urlencode

import httpx
from supabase import create_client

# Allow `from _db import safe_upsert` regardless of CWD — the script is run
# as `python scrapers/01_enumerate_ato.py` from the repo root.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _db import safe_upsert  # noqa: E402

log = logging.getLogger("enumerate_ato")

API_BASE = "https://www.ato.gov.au/API/v1/law/lawservices/browse-content/"

# Conservative concurrency. The API is friendly but it's still polite to
# stay light.
CONCURRENCY = 4
REQUEST_DELAY_SEC = 0.3
HTTP_TIMEOUT = 30
MAX_RETRIES = 3

# Push rows to Supabase every FLUSH_EVERY leaves rather than only at end of
# walk. A mid-walk crash (network drop, OS hiccup, accidental kill) loses at
# most this many leaves of work. The walk is idempotent (on_conflict=docid +
# ignore_duplicates), so restart re-walks the tree and already-flushed rows
# upsert as no-ops.
FLUSH_EVERY = 1000

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15"
)
# Critical: the API expects a Referer pointing at the SPA, otherwise some
# requests come back empty. Set the standard expected headers.
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-AU,en;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.ato.gov.au/single-page-applications/legaldatabase",
}

# Doc types seeded in sql/01_schema.sql. The ATO Type tree contains many
# leaves whose short_code prefix isn't a recognised doc type (literal "Test"
# items, legislation pointers, archive-only types). Without this guard the
# documents upsert fails with FK constraint violations and the whole batch
# rolls back. Keep this list in sync with the document_types seed in the
# schema; on mismatch the offending leaf is skipped and logged.
VALID_DOC_TYPES: frozenset[str] = frozenset({
    "ATO ID", "CR", "DIS", "EC", "ED", "EPA", "ER", "FACT", "FTD", "FTR",
    "GSTB", "GSTD", "GSTR", "ITR", "LCR", "LCTD", "MT", "OTHER", "PCG",
    "PGBR", "PR", "PS LA", "SCRT", "SGD", "SGN", "SGR", "SMSFD", "SMSFR",
    "SMSFRB", "TA", "TD", "TDP", "TR", "WTD", "WTR",
})
# Multi-word codes need a two-token check (otherwise "ATO ID 2019/1" derives
# to "ATO" which isn't valid). Order doesn't matter — these are checked first
# via startswith(prefix + " ").
MULTI_WORD_DOC_TYPES: tuple[str, ...] = tuple(
    c for c in VALID_DOC_TYPES if " " in c
)

# Top-level ATO tree roots we deliberately DO NOT walk. "Edited private advice"
# alone branches into hundreds of thousands of leaves (Phase-2, deferred) and
# was the sole cause of the ~3h / 401,872-leaf walk that blew the 60-min
# monitor timeout — every EPA leaf is then dropped anyway (no recognised
# doc_type prefix), so walking them is pure waste. "Legislation and supporting
# material" we never ingest (we link out to legislation.gov.au). Both
# contribute ZERO rows to `documents`, so skipping them cannot orphan a corpus
# doc from the visibility sweep.
#
# CASES IS INTENTIONALLY WALKED (it populates the `cases` catalogue, ADR-0006).
#
# !!! MAINTENANCE !!! This is an allow-by-omission filter keyed on exact
# top-level titles. If the ATO adds a NEW top-level root that carries documents
# we ingest, it is walked automatically — good. But if you START ingesting a
# doc type that lives under a root listed here, you MUST remove that root from
# this set or its docs will never be re-seen and the sweep will flag them
# removed. Pass --include-deferred to walk everything (e.g. a one-off EPA
# backfill). Every other small root (Bulletins, Other ATO documents, ATO law
# aids, Archived) is kept on purpose: each feeds a handful of corpus docs
# (e.g. SMSFRB, GSTB) and is cheap to walk.
SKIP_ROOTS: frozenset[str] = frozenset({
    "Edited private advice",
    "Legislation and supporting material",
})


CASES_ROOT = "Cases"

# Long-form court / tribunal names as they appear in the ATO Type tree,
# mapped to the short codes we want to store. Anything not in this table
# is stored verbatim. Updated as new courts surface during enumeration.
COURT_SHORT_CODES: dict[str, str] = {
    "Administrative Appeals Tribunal": "AAT",
    "Administrative Review Tribunal": "ART",
    "Federal Court of Australia": "FCA",
    "Full Federal Court of Australia": "FCAFC",
    "High Court of Australia": "HCA",
    "Family Court": "FamC",
    "Supreme Court": "SC",
    "Privy Council": "PC",
    "Federal Circuit Court": "FCC",
    "Federal Circuit and Family Court of Australia": "FedCFamC",
    "Court of Appeal": "CA",
    "United Kingdom": "UK",
    "New Zealand": "NZ",
    "Miscellaneous - Australian": "Misc-AU",
    "Miscellaneous - Overseas": "Misc-OS",
}

_SLUG_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _slugify(s: str) -> str:
    """Lowercase, collapse non-alphanumerics to single hyphens, strip ends."""
    s = s.lower().strip()
    s = _SLUG_NON_ALNUM_RE.sub("-", s)
    return s.strip("-")


def _normalise_court(name: str) -> str:
    """Return a short code for the court name as it appears in the breadcrumb.
    Falls through verbatim for unmapped courts so we don't lose data."""
    return COURT_SHORT_CODES.get(name.strip(), name.strip())


def derive_case_from_breadcrumb(node: "TreeNode") -> Optional[dict]:
    """Return a `cases`-table row dict if the leaf lives under
    Cases > <Court> > <Year> > <Case name>, else None.

    The leaf itself is usually labelled "Judgment" or similar and carries
    no useful metadata of its own — the case identity comes from the parent
    folders, which are in `node.breadcrumb`. Because `parse_nodes` appends
    the leaf title to breadcrumb at construction time, for a typical case
    leaf the breadcrumb is:

        ['Cases', '<court long-form>', '<year>', '<case name>', '<leaf title>']

    A leaf whose Cases-tree path doesn't follow this 5-level shape is
    skipped (returns None) — caller treats it like any other non-matching
    leaf.
    """
    breadcrumb = node.breadcrumb
    if not breadcrumb or breadcrumb[0] != CASES_ROOT:
        return None
    if len(breadcrumb) < 4:
        return None
    court_long = breadcrumb[1]
    year_str = breadcrumb[2]
    case_name = breadcrumb[3].strip()
    try:
        year = int(year_str)
    except ValueError:
        return None
    court = _normalise_court(court_long)
    slug = _slugify(f"{court}-{year}-{case_name}")
    href = (node.raw.get("a_attr") or {}).get("href") or node.raw.get("href") or ""
    ato_url = (
        f"https://www.ato.gov.au{href}"
        if href.startswith("/")
        else (href or None)
    )
    return {
        "slug": slug,
        "case_name": case_name,
        "court": court,
        "decision_year": year,
        "ato_url": ato_url,
        "ato_path": breadcrumb,
    }


def derive_doc_type(short_code: str) -> Optional[str]:
    """Return the schema-valid doc_type for `short_code`, or None if unknown.

    Tries multi-word codes (e.g. "PS LA", "ATO ID") first, then the single-
    token prefix. Returns None for unrecognised heads — the caller should
    skip the leaf rather than fail the upsert.
    """
    if not short_code:
        return None
    for mw in MULTI_WORD_DOC_TYPES:
        if short_code.startswith(mw + " ") or short_code == mw:
            return mw
    head = short_code.split()[0]
    return head if head in VALID_DOC_TYPES else None


def _touch_last_seen(supabase, docids: list[str], seen_at_iso: str,
                     batch_size: int = 500) -> int:
    """Refresh `last_seen_in_search` for observed docids (ADR-0007).

    A plain batched UPDATE, NOT an upsert. This used to be a partial-column
    upsert of {docid, last_seen_in_search}, which silently refreshed ZERO docs
    on every run: Postgres validates NOT-NULL constraints on the candidate
    INSERT tuple BEFORE `ON CONFLICT` arbitration, so an upsert that omits the
    NOT-NULL `short_code` fails outright — it never reaches DO UPDATE, even for
    a docid that already exists. (Fixed 2026-05-26; see HANDOFF Pending #1.)

    Returns the number of rows actually updated (PostgREST returns the updated
    rows), so a mismatch between observed docids and stored docids shows up as
    updated < len(docids) rather than failing silently.
    """
    updated = 0
    for i in range(0, len(docids), batch_size):
        chunk = docids[i:i + batch_size]
        try:
            resp = (
                supabase.table("documents")
                .update({"last_seen_in_search": seen_at_iso})
                .in_("docid", chunk)
                .execute()
            )
            updated += len(resp.data or [])
        except Exception as e:  # noqa: BLE001 — log + continue, never fail the walk
            log.error("last_seen touch failed for %d docids (%s…): %s",
                      len(chunk), chunk[0] if chunk else "", e)
    return updated


# ----------------------------------------------------------------------------
# Tree walking
# ----------------------------------------------------------------------------
@dataclass
class TreeNode:
    title: str
    is_folder: bool
    toc_path: Optional[str]               # the "Mode=type&Action=inject&TOC=..." string from data.url, or None for leaves
    raw: dict[str, Any] = field(default_factory=dict)
    breadcrumb: list[str] = field(default_factory=list)


def _params_from_data_url(data_url: str) -> dict[str, str]:
    """Convert "Mode=type&Action=inject&TOC=..." into a dict suitable for
    httpx params. The TOC value contains colons and # which must be properly
    URL-encoded."""
    return dict(parse_qs(data_url))


async def fetch_node(client: httpx.AsyncClient, data_url: Optional[str]) -> list[dict]:
    """Fetch a tree node's children. data_url is None → root."""
    for attempt in range(MAX_RETRIES):
        try:
            if data_url is None:
                # Root call — uses category=E to fetch top-level structure
                resp = await client.get(API_BASE, params={"category": "E"})
            else:
                # Pass through the parameters from data.url verbatim
                params = _params_from_data_url(data_url)
                # Flatten: parse_qs gives lists, httpx accepts lists too
                resp = await client.get(API_BASE, params=params)
            resp.raise_for_status()
            await asyncio.sleep(REQUEST_DELAY_SEC)
            return resp.json()
        except (httpx.HTTPError, json.JSONDecodeError) as e:
            if attempt == MAX_RETRIES - 1:
                log.error("Failed after %d attempts: %s — %s", MAX_RETRIES, data_url, e)
                return []
            await asyncio.sleep(2 ** attempt)
    return []


def parse_nodes(raw_list: list[dict], breadcrumb: list[str]) -> list[TreeNode]:
    """Convert raw API output into TreeNode objects. The API uses Fancytree
    conventions — folder/lazy/data/children."""
    out: list[TreeNode] = []
    for item in raw_list:
        title = item.get("title", "")
        is_folder = bool(item.get("folder"))
        data_url = (item.get("data") or {}).get("url")
        out.append(TreeNode(
            title=title,
            is_folder=is_folder,
            toc_path=data_url,
            raw=item,
            breadcrumb=breadcrumb + [title],
        ))
        # If children are inlined (server pre-expanded some nodes), include
        # them too — recursion handles this naturally if we treat inlined
        # children as already-known.
    return out


# ----------------------------------------------------------------------------
# Leaf detection — turning a document leaf into a fetch_queue row
# ----------------------------------------------------------------------------
# CONFIRMED leaf shape from the live API (no `folder` flag, has `a_attr.href`):
#
#   {
#     "title": "TR 2024/1 - Income tax: composite items - identifying the relevant
#               depreciating asset for capital allowances",
#     "a_attr": {
#       "href": "/law/view/document?docid=TXR/TR20241/NAT/ATO/00001"
#     }
#   }
#
# We extract:
#   - docid    via the `docid=` query parameter in href
#   - short_code via the portion of title before the first " - "
#   - api_title  the portion AFTER the first " - " (the descriptive title)
#   - ato_url  by prepending the domain to the relative href

_DOCID_FROM_HREF_RE = re.compile(r"docid=([^&]+)")


def is_leaf(raw: dict) -> bool:
    """A node is a leaf if it isn't a folder. Folders always set folder=true."""
    return not bool(raw.get("folder"))


def leaf_to_queue_row(node: TreeNode) -> Optional[dict]:
    raw = node.raw
    href = (raw.get("a_attr") or {}).get("href") or raw.get("href") or ""

    m = _DOCID_FROM_HREF_RE.search(href)
    if not m:
        log.warning("Leaf missing docid in href: title=%r href=%r", node.title, href)
        return None
    docid = m.group(1)

    # Title format: "TR 2024/1 - Income tax: composite items - identifying..."
    # Split on the FIRST " - " only (descriptive title may contain more " - ").
    parts = node.title.split(" - ", 1)
    short_code = parts[0].strip()
    api_title = parts[1].strip() if len(parts) > 1 else ""

    # Build absolute URL. The API gives us a relative path; prepend the domain.
    ato_url = (
        f"https://www.ato.gov.au{href}"
        if href.startswith("/")
        else f"https://www.ato.gov.au/law/view/document?docid={docid}"
    )

    return {
        "docid": docid,
        "ato_url": ato_url,
        "short_code": short_code,
        "api_title": api_title,             # consumed by enumerator; not a column
        "topic_path": node.breadcrumb,      # full TOC path for downstream context
        "priority": 100,
        "status": "pending",
    }


# ----------------------------------------------------------------------------
# Recursive walk
# ----------------------------------------------------------------------------
async def walk(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    breadcrumb: list[str],
    data_url: Optional[str],
    on_leaf,
) -> int:
    """Depth-first tree walk. Returns the count of leaves found."""
    async with sem:
        raw = await fetch_node(client, data_url)

    if not raw:
        return 0

    nodes = parse_nodes(raw, breadcrumb)
    leaf_count = 0

    # Process folders concurrently, leaves serially.
    folder_tasks = []
    for node in nodes:
        if node.is_folder and node.toc_path:
            folder_tasks.append(walk(client, sem, node.breadcrumb, node.toc_path, on_leaf))
        elif node.is_folder and not node.toc_path:
            # Folder with no data.url — usually means children were inlined
            # in `node.raw["children"]`. Process those inline.
            for child in node.raw.get("children", []) or []:
                child_node = parse_nodes([child], node.breadcrumb)[0]
                if child_node.is_folder and child_node.toc_path:
                    folder_tasks.append(walk(client, sem, child_node.breadcrumb, child_node.toc_path, on_leaf))
                elif not child_node.is_folder:
                    on_leaf(child_node)
                    leaf_count += 1
        else:
            on_leaf(node)
            leaf_count += 1

    results = await asyncio.gather(*folder_tasks, return_exceptions=True)
    for r in results:
        if isinstance(r, int):
            leaf_count += r
        elif isinstance(r, Exception):
            log.error("Walk subtask failed: %s", r)

    log.info("[%s] %d leaves below", " > ".join(breadcrumb) or "ROOT", leaf_count)
    return leaf_count


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------
async def main_async(args: argparse.Namespace) -> int:
    supabase = None
    if not args.dry_run:
        supabase = create_client(
            os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"]
        )

    rows_buffer: list[dict] = []
    docs_buffer: list[dict] = []
    cases_buffer: list[dict] = []
    skipped_by_head: Counter[str] = Counter()
    seen_docids: set[str] = set()
    seen_case_slugs: set[str] = set()
    duplicate_count = 0
    cum_docs_ok = 0
    cum_docs_bad = 0
    cum_queue_ok = 0
    cum_queue_bad = 0
    cum_cases_ok = 0
    cum_cases_bad = 0

    def flush_buffers() -> None:
        """Push accumulated rows to Supabase and clear local buffers.

        Idempotent and safe to call with empty buffers. Safe to call repeatedly
        during the walk — `safe_upsert` handles partial-batch failure
        gracefully. The asyncio walk is cooperative (no preemption), and
        `safe_upsert` never awaits, so no lock is needed even though multiple
        coroutines feed `on_leaf`.
        """
        nonlocal cum_docs_ok, cum_docs_bad, cum_queue_ok, cum_queue_bad
        nonlocal cum_cases_ok, cum_cases_bad
        if args.dry_run or not supabase:
            return
        if docs_buffer:
            # First pass: catalogue inserts only. ignore_duplicates=True
            # prevents overwriting existing fields like status / title that
            # the fetcher and parser own (the bug we fixed on 2026-05-21).
            ok, bad = safe_upsert(
                supabase, "documents", docs_buffer,
                on_conflict="docid", ignore_duplicates=True,
            )
            cum_docs_ok += ok
            cum_docs_bad += bad

            # NB: the last_seen_in_search "touch" (ADR-0007) is NOT done here.
            # It's a single batched UPDATE after the walk (see main_async) —
            # running it per-flush makes it contend with these concurrent
            # ON CONFLICT DO NOTHING inserts for row locks and time out (57014).
            docs_buffer.clear()
        if rows_buffer:
            # ignore_duplicates=True is critical here. The enumerator's role is
            # to announce "I saw this docid" — it should NEVER touch state that
            # the fetcher owns (status, last_error, attempt_count,
            # last_attempt_at). Without this flag, re-walking flips every
            # existing row's status back to "pending" because the enumerator
            # payload includes status="pending" by default. That happened once
            # on 2026-05-21 and required a one-shot SQL restore from ground
            # truth (document_versions / preserved last_error). Don't repeat
            # the mistake.
            ok, bad = safe_upsert(
                supabase, "fetch_queue", rows_buffer,
                on_conflict="docid", ignore_duplicates=True,
            )
            cum_queue_ok += ok
            cum_queue_bad += bad
            rows_buffer.clear()
        if cases_buffer:
            # `last_seen_at` defaults to now() on insert but isn't refreshed on
            # upsert by Postgres alone. The pipeline accepts this — `last_seen_at`
            # is initialised on first sight and never updated. If we want refresh
            # semantics later, change to `ignore_duplicates=False` plus an
            # explicit `last_seen_at: now()` field in the payload.
            ok, bad = safe_upsert(
                supabase, "cases", cases_buffer,
                on_conflict="slug", ignore_duplicates=True,
            )
            cum_cases_ok += ok
            cum_cases_bad += bad
            cases_buffer.clear()
        log.info(
            "  [flush] cumulative: documents %d ok / %d skipped, "
            "fetch_queue %d ok / %d skipped, "
            "cases %d ok / %d skipped",
            cum_docs_ok, cum_docs_bad, cum_queue_ok, cum_queue_bad,
            cum_cases_ok, cum_cases_bad,
        )

    def on_leaf(node: TreeNode) -> None:
        nonlocal duplicate_count
        # Case-tree leaves come first — they don't follow the doc-leaf shape
        # (no docid in href, leaf title is generic like "Judgment") and we
        # want them in `cases`, not `documents` / `fetch_queue`. Per
        # ADR-0006: metadata + ATO classification + deep-link only, no text
        # fetch. The dedup-by-slug elides multi-leaf cases naturally.
        case_row = derive_case_from_breadcrumb(node)
        if case_row is not None:
            slug = case_row["slug"]
            if slug in seen_case_slugs:
                return
            seen_case_slugs.add(slug)
            cases_buffer.append(case_row)
            if not args.dry_run and supabase and len(cases_buffer) >= FLUSH_EVERY:
                flush_buffers()
            return

        row = leaf_to_queue_row(node)
        if not row:
            return

        # Docs can appear under multiple Type-tree folders (cross-categorised
        # rulings, etc.). Without this dedup, the upsert batches eventually
        # contain two rows with the same docid and Postgres errors with code
        # 21000 — "ON CONFLICT DO UPDATE command cannot affect row a second
        # time". safe_upsert would recover via per-row fallback, but skipping
        # duplicates here is both faster and more deterministic about which
        # version "wins" (first-seen).
        docid = row["docid"]
        if docid in seen_docids:
            duplicate_count += 1
            return
        seen_docids.add(docid)

        # Bootstrap the documents row from API data — gives us a complete
        # catalogue with titles before we've fetched anything. The fetcher
        # and parser will fill in body content, dates, etc.
        short_code = row["short_code"]
        head = derive_doc_type(short_code)
        if head is None:
            raw_head = short_code.split()[0] if short_code else "<empty>"
            skipped_by_head[raw_head] += 1
            return

        # Derive year/number from short_code (e.g. "TR 2024/1" → 2024, "1")
        year, number = None, None
        m = re.search(r"(\d{4})/(D?\d+[A-Z]*)", short_code)
        if m:
            year, number = int(m.group(1)), m.group(2)

        docs_buffer.append({
            "docid": row["docid"],
            "short_code": short_code,
            "doc_type": head,
            "year": year,
            "number": number,
            "is_draft": (number or "").startswith("D"),
            "title": row["api_title"] or short_code,
            "status": "current",  # placeholder — parser corrects later
            "ato_url": row["ato_url"],
        })

        # Strip transient fields the queue table doesn't have.
        queue_row = {k: v for k, v in row.items() if k not in ("api_title", "topic_path")}
        rows_buffer.append(queue_row)

        if args.dry_run and len(rows_buffer) <= 5:
            print(f"  LEAF: {short_code:<15} {row['docid']}  —  {row['api_title'][:60]}")

        # Periodic durability flush. Crash before next flush only loses up to
        # `FLUSH_EVERY` leaves' worth of work, not the whole walk.
        if len(rows_buffer) >= FLUSH_EVERY:
            flush_buffers()

    sem = asyncio.Semaphore(CONCURRENCY)
    total = 0
    elapsed = 0.0
    try:
        async with httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=HTTP_TIMEOUT) as client:
            start = time.time()
            # Fetch the top-level roots first so we can filter which subtrees to
            # walk (SKIP_ROOTS / --root / --include-deferred). This is what keeps
            # the walk off the ~401k-leaf "Edited private advice" subtree.
            root_nodes = parse_nodes(await fetch_node(client, None), [])
            available = [n.title for n in root_nodes]
            if args.root:
                selected = [n for n in root_nodes if n.title == args.root]
                if not selected:
                    log.error("Root %r not found. Available roots: %s",
                              args.root, available)
                    return 1
            elif args.include_deferred:
                selected = list(root_nodes)
            else:
                selected = [n for n in root_nodes if n.title not in SKIP_ROOTS]
                for n in root_nodes:
                    if n.title in SKIP_ROOTS:
                        log.info("Skipping deferred root: %s "
                                 "(pass --include-deferred to walk it)", n.title)

            walk_tasks = [
                walk(client, sem, n.breadcrumb, n.toc_path, on_leaf)
                for n in selected
                if n.is_folder and n.toc_path
            ]
            # A top-level node that is itself a leaf (rare) still counts.
            for n in selected:
                if not n.is_folder:
                    on_leaf(n)
            results = await asyncio.gather(*walk_tasks, return_exceptions=True)
            total = sum(r for r in results if isinstance(r, int))
            for r in results:
                if isinstance(r, Exception):
                    log.error("Top-level walk failed: %s", r)
            elapsed = time.time() - start
    finally:
        # Last-mile durability: flush any residual buffer on clean exit,
        # Ctrl-C, or unexpected exception. Re-running picks up where this
        # left off because already-flushed docids upsert as no-ops.
        flush_buffers()

    # Visibility "touch" (ADR-0007), once, AFTER the walk — when no concurrent
    # inserts are holding row locks (the per-flush version timed out, 57014).
    # Refreshes last_seen_in_search for every docid observed this walk so the
    # daily monitor's sweep can tell what's genuinely gone from ATO search.
    if not args.dry_run and supabase and seen_docids:
        seen_at_iso = datetime.now(timezone.utc).isoformat()
        touched = _touch_last_seen(supabase, list(seen_docids), seen_at_iso,
                                   batch_size=200)
        log.info("last_seen_in_search refreshed for %d/%d observed docs",
                 touched, len(seen_docids))

    log.info("Walk complete: %d leaves discovered in %.1fs", total, elapsed)
    log.info("Unique docids: %d (skipped %d duplicate appearances)", len(seen_docids), duplicate_count)
    log.info("Unique cases: %d (Cases-tree slugs deduped)", len(seen_case_slugs))
    if skipped_by_head:
        total_skipped = sum(skipped_by_head.values())
        top = skipped_by_head.most_common(10)
        log.info(
            "Skipped %d leaves with unrecognised doc_type (top 10 prefixes): %s",
            total_skipped,
            ", ".join(f"{h}={n}" for h, n in top),
        )
    if not args.dry_run and supabase:
        log.info(
            "Final totals: documents %d ok / %d skipped, "
            "fetch_queue %d ok / %d skipped, "
            "cases %d ok / %d skipped",
            cum_docs_ok, cum_docs_bad, cum_queue_ok, cum_queue_bad,
            cum_cases_ok, cum_cases_bad,
        )
        log.info("Done.")

    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--root", help="Restrict walk to a single top-level node title "
                                   "(overrides SKIP_ROOTS; useful for testing/ops)")
    p.add_argument("--include-deferred", action="store_true",
                   help="Walk every top-level root, including the deferred ones in "
                        "SKIP_ROOTS (e.g. a one-off Edited-private-advice backfill).")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
