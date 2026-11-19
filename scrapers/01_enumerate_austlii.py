"""
01_enumerate_austlii.py
=======================
Stage 1: Build a complete index of every ATO document that exists.

Strategy:
    AustLII (austlii.edu.au) maintains a public, scraper-friendly mirror of
    the ATO content. Their per-type index pages list every document in the
    series with title, year, and number. We crawl ONE index page per document
    type, parse the entries, then construct the ATO canonical URL for each.

    We do NOT fetch document content from AustLII — only the index. The full
    content is then fetched from the ATO canonical URL by 02_fetch_ato.py,
    so we're using the source the ATO has explicitly made reusable under its
    permissive copyright notice.

    Output rows are written to the `fetch_queue` table in Supabase, ready
    for the fetcher to consume.

The AustLII index URL pattern is:
    https://www.austlii.edu.au/cgi-bin/viewdb/au/other/rulings/ato/{DB_CODE}/

    DB_CODE values are stored in the `document_types.austlii_db_code` column.

Run:
    python 01_enumerate_austlii.py            # full pass over all types
    python 01_enumerate_austlii.py --type TR  # one type only
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from supabase import create_client  # pip install supabase

log = logging.getLogger("enumerate")

AUSTLII_BASE = "https://www.austlii.edu.au"
AUSTLII_INDEX_TPL = AUSTLII_BASE + "/cgi-bin/viewdb/au/other/rulings/ato/{db}/"

# ----------------------------------------------------------------------------
# Docid construction
# ----------------------------------------------------------------------------
# Confirmed mappings from real URLs (DevTools inspection):
#
#   FINAL/CURRENT documents:
#     TXR / TR20241    → TR 2024/1        (Taxation Ruling)
#     TXD / TD20232    → TD 2023/2        (Taxation Determination — DISTINCT from TR)
#     TXR / TR20221W   → TR 2022/1W       (Withdrawn — "W" suffix in ref)
#     GST / GSTR…      → GST Rulings
#     GSD / GSTD20241  → GSTD 2024/1
#     MXR / MT20083    → MT 2008/3
#     COG / LCR20231   → LCR 2023/1       (Law Companion Ruling)
#     COG / PCG20189   → PCG 2018/9       (Practical Compliance Guideline — shares COG)
#     PSR / PS20252    → PS LA 2025/2     (ref uses "PS" NOT "PSLA")
#     SGD / SGD20171   → SGD 2017/1
#     SFD / SMSFD20131 → SMSFD 2013/1
#     AID / AID20104   → ATO ID 2010/4    (3-segment docid; ref uses "AID")
#     TPA / TA20261    → TA 2026/1
#     CGR / GSTR2025EC1 → GSTR 2025/1EC   (Compendium — own prefix, parent in ref)
#
#   DRAFTS:
#     COD / LCR2026D1  → LCR 2026/D1      (Draft Law Companion Ruling)
#     ↑ Drafts go into a separate "draft" category prefix. Pattern observed:
#       COG (final guidance) → COD (draft guidance) — covers both LCR and PCG
#     Other draft prefixes (TR, TD, GSTR, etc.) NOT YET CONFIRMED.
#
#   DISs:  LIT/ICD/{court}{file_no}of{year}/00001  — built from court file
#          numbers, not year/number sequences. Handled separately.
#
# CATEGORY_PREFIX maps (doc_type, is_draft) tuples to the category prefix.
# Final/current uses the standard prefix; drafts may use a different one.
CATEGORY_PREFIX_FINAL: dict[str, str] = {
    "TR":     "TXR",
    "TD":     "TXD",
    "GSTR":   "GST",
    "GSTD":   "GSD",
    "MT":     "MXR",
    "WTR":    "WTR",
    "WTD":    "WTD",   # provisional — Wine Equalisation Tax Determination
    "ER":     "EXR",
    "ED":     "EXD",   # provisional — Excise Determination
    "FTR":    "RFT",
    "FTD":    "FTD",   # provisional — Fuel Tax Determination
    "LCTD":   "LCD",   # provisional — Luxury Car Tax Determination
    "PGBR":   "PGR",
    "SGR":    "SGR",
    "SGD":    "SGD",
    "SMSFR":  "SFR",
    "SMSFD":  "SFD",
    "CR":     "CLR",
    "PR":     "PRR",
    "PCG":    "COG",
    "LCR":    "COG",
    "PS LA":  "PSR",
    "TA":     "TPA",
}

# For drafts: each doc type has its own draft prefix in the ATO's system.
# Confirmed mappings:
#   DTR / TR2025D1   → TR  2025/D1  (Draft Taxation Ruling)
#   DXT / TD2025D3   → TD  2025/D3  (Draft Taxation Determination)
#   COD / LCR2026D1  → LCR 2026/D1  (Draft Law Companion Ruling)
# Hypothesis: PCG drafts → COD (same bucket as LCR drafts). Unconfirmed.
# Other doc types: draft prefixes unknown until we observe one.
CATEGORY_PREFIX_DRAFT: dict[str, str] = {
    "TR":     "DTR",
    "TD":     "DXT",
    "LCR":    "COD",
    "PCG":    "COD",   # hypothesis — confirm with any current PCG draft URL
    # Unconfirmed for: GSTR, GSTD, MT, WTR, WTD, ER, ED, FTR, FTD, LCTD,
    # PGBR, SGR, SGD, SMSFR, SMSFD, CR, PR, PS LA, TA. When we hit a draft
    # of any of these during enumeration, the fetcher will fail and we
    # patch this map.
}

# Ref-code overrides — what appears in the ref segment for each doc_type.
# Default is doc_type code with spaces stripped; these are the exceptions.
REF_CODE: dict[str, str] = {
    "PS LA":  "PS",    # not "PSLA"
    "ATO ID": "AID",   # not "ATOID"
}


def _ref_code(doc_type: str) -> str:
    return REF_CODE.get(doc_type, doc_type.replace(" ", ""))


ATO_DOC_URL_TPL = "https://www.ato.gov.au/law/view/document?docid={docid}"


def build_docid(
    doc_type: str,
    year: int,
    number: str,
    is_draft: bool = False,
    suffix: str = "",
    version: str = "00001",
) -> str:
    """Construct the canonical ATO docid for a (doc_type, year, number) tuple.

    is_draft: routes to the draft category prefix where one is known.
    suffix:   modifiers appended to the ref ("W" for withdrawals;
              parent doc-type code for compendiums).
    """
    # ATO ID — 3-segment docid, no NAT/ATO.
    if doc_type == "ATO ID":
        ref = f"AID{year}{number}{suffix}"
        return f"AID/{ref}/{version}"

    # DIS — court-file-based, can't construct from (year, number).
    if doc_type == "DIS":
        raise ValueError(
            "DIS docids are built from court file numbers, not year/number. "
            "Use build_dis_docid(court_registry, file_no, file_year) instead."
        )

    # Compendiums — dedicated CGR prefix, parent type embedded in ref.
    if doc_type == "EC":
        if not suffix:
            raise ValueError("Compendiums require the parent doc_type in suffix")
        parent_type = suffix
        ref = f"{parent_type}{year}EC{number}"
        return f"CGR/{ref}/NAT/ATO/{version}"

    # Standard 5-segment pattern.
    prefix_map = CATEGORY_PREFIX_DRAFT if is_draft else CATEGORY_PREFIX_FINAL
    prefix = prefix_map.get(doc_type) or CATEGORY_PREFIX_FINAL.get(doc_type)
    if prefix is None:
        raise ValueError(f"No prefix mapping for doc_type {doc_type!r}")

    code = _ref_code(doc_type)
    # Drafts put a "D" before the number; e.g. LCR 2026/D1 → ref LCR2026D1
    draft_marker = "D" if is_draft else ""
    ref = f"{code}{year}{draft_marker}{number}{suffix}"
    return f"{prefix}/{ref}/NAT/ATO/{version}"


def build_dis_docid(
    court_registry: str, file_no: int, file_year: int, version: str = "00001"
) -> str:
    """DIS docid builder.

    Observed examples:
        LIT/ICD/vid779of2025/00001    (Victoria registry, file 779 of 2025)
        LIT/ICD/WAD112of2025/00001    (Western Australia registry, file 112)

    Case of the registry code matters — preserve what the source provides.
    """
    ref = f"{court_registry}{file_no}of{file_year}"
    return f"LIT/ICD/{ref}/{version}"

HTTP_TIMEOUT = 30
POLITE_DELAY_SEC = 1.0   # be a good citizen on AustLII
USER_AGENT = (
    "ATO-LegalDB-Mirror/0.1 "
    "(research; will be replaced with contact email before production runs)"
)


@dataclass
class IndexEntry:
    """A single document discovered on an AustLII index page."""
    doc_type: str             # "TR", "GSTR", etc.
    short_code: str           # "TR 2024/1"
    year: int
    number: str               # "1", "1A", "D1"
    title: str
    is_draft: bool
    austlii_url: str
    ato_docid: str            # constructed canonical docid
    ato_url: str              # constructed canonical URL


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
_SHORTCODE_RE = re.compile(
    r"^\s*([A-Z][A-Z\s]*?)\s*(\d{2,4})\s*/\s*(D?\d+[A-Z]?)\s*$"
)


def _parse_short_code(text: str) -> Optional[tuple[str, int, str, bool]]:
    """Parse 'TR 2024/1' or 'GSTR 2024/D2'."""
    m = _SHORTCODE_RE.match(text)
    if not m:
        return None
    doc_type = m.group(1).strip()
    year_raw = m.group(2)
    number = m.group(3)
    year = int(year_raw) if len(year_raw) == 4 else 1900 + int(year_raw) if int(year_raw) > 50 else 2000 + int(year_raw)
    is_draft = number.startswith("D")
    return doc_type, year, number, is_draft


def _build_ato_url(doc_type: str, year: int, number: str, is_draft: bool) -> tuple[str, str]:
    """Construct the canonical ATO docid and URL using the dispatcher.

    AustLII renders draft numbers as "D1", "D2" etc. The builder takes the
    number WITHOUT the D prefix and handles the marker itself based on
    is_draft, so we strip it here if present.
    """
    clean_number = number.lstrip("D") if is_draft else number

    if doc_type == "DIS":
        raise ValueError("DIS enumeration uses a separate path")

    docid = build_docid(doc_type, year, clean_number, is_draft=is_draft)
    url = ATO_DOC_URL_TPL.format(docid=docid)
    return docid, url


def parse_austlii_index(doc_type: str, html: str) -> list[IndexEntry]:
    """Parse one AustLII index page into IndexEntry rows.

    AustLII index pages have the structure:
        <li><a href="2024/1.html">TR 2024/1 - Title here...</a></li>
    The link text always starts with the short_code.
    """
    # html.parser (stdlib) instead of lxml — see comment in
    # parsers/03_parse_document.py. This file is the retired AustLII
    # fallback (ADR-0004) and isn't on the production path, but keeping
    # the parser choice consistent prevents future divergence.
    soup = BeautifulSoup(html, "html.parser")
    entries: list[IndexEntry] = []

    for a in soup.select("a[href]"):
        href = a.get("href", "")
        text = a.get_text(" ", strip=True)
        if not text.startswith(doc_type):
            continue
        if not re.search(r"\.html?$", href):
            continue

        # Split on " - " to get short_code and title.
        parts = re.split(r"\s+-\s+", text, maxsplit=1)
        short_part = parts[0].strip()
        title = parts[1].strip() if len(parts) > 1 else ""

        parsed = _parse_short_code(short_part)
        if not parsed:
            continue
        _doc_type, year, number, is_draft = parsed

        try:
            docid, ato_url = _build_ato_url(doc_type, year, number, is_draft)
        except ValueError as e:
            log.warning("Skipping %s: %s", short_part, e)
            continue

        entries.append(IndexEntry(
            doc_type=doc_type,
            short_code=short_part,
            year=year,
            number=number,
            title=title,
            is_draft=is_draft,
            austlii_url=urljoin(AUSTLII_BASE, href),
            ato_docid=docid,
            ato_url=ato_url,
        ))

    return entries


# ---------------------------------------------------------------------------
# Crawl
# ---------------------------------------------------------------------------
def fetch_austlii_index(client: httpx.Client, db_code: str) -> str:
    url = AUSTLII_INDEX_TPL.format(db=db_code)
    log.info("Fetching index: %s", url)
    resp = client.get(url, follow_redirects=True)
    resp.raise_for_status()
    time.sleep(POLITE_DELAY_SEC)
    return resp.text


def enumerate_type(
    client: httpx.Client, doc_type: str, db_code: str
) -> list[IndexEntry]:
    html = fetch_austlii_index(client, db_code)
    entries = parse_austlii_index(doc_type, html)
    log.info("Discovered %d entries for %s", len(entries), doc_type)
    return entries


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def upsert_to_queue(supabase, entries: Iterable[IndexEntry]) -> None:
    """Insert discovered entries into the fetch_queue (idempotent via unique
    docid constraint)."""
    rows = [
        {
            "docid": e.ato_docid,
            "ato_url": e.ato_url,
            "austlii_url": e.austlii_url,
            "short_code": e.short_code,
            "priority": 100,
            "status": "pending",
        }
        for e in entries
    ]
    if not rows:
        return
    # Supabase upsert on the docid unique key
    for batch_start in range(0, len(rows), 500):
        batch = rows[batch_start : batch_start + 500]
        supabase.table("fetch_queue").upsert(batch, on_conflict="docid").execute()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
def get_db_codes(supabase, only_type: Optional[str]) -> list[tuple[str, str]]:
    q = supabase.table("document_types").select("code,austlii_db_code")
    if only_type:
        q = q.eq("code", only_type)
    rows = q.execute().data
    return [(r["code"], r["austlii_db_code"]) for r in rows if r["austlii_db_code"]]


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--type", help="Restrict to a single doc_type code, e.g. TR")
    p.add_argument("--dry-run", action="store_true", help="Print results, don't write")
    args = p.parse_args()

    supabase = create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SERVICE_ROLE_KEY"],
    )

    types = get_db_codes(supabase, args.type)
    if not types:
        log.error("No document types found.")
        return 1

    headers = {"User-Agent": USER_AGENT, "Accept": "text/html"}
    total = 0
    with httpx.Client(headers=headers, timeout=HTTP_TIMEOUT) as client:
        for doc_type, db_code in types:
            try:
                entries = enumerate_type(client, doc_type, db_code)
            except httpx.HTTPError as e:
                log.error("Failed %s (%s): %s", doc_type, db_code, e)
                continue

            if args.dry_run:
                for e in entries[:5]:
                    print(e)
            else:
                upsert_to_queue(supabase, entries)
            total += len(entries)

    log.info("Done. Total entries: %d", total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
