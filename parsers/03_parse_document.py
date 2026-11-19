"""
03_parse_document.py
====================
Stage 3: Parse raw HTML snapshots into structured data.

This is where the "magic" gets built. For each newly fetched document we:

    1. Extract document-level metadata
        - title, short_title, issued/effect/withdrawn dates, status
        - addenda, withdrawal notices

    2. Extract paragraph-level content
        - rulings are numbered: "1.", "2.", "2A.", "23(b)" — preserve these
        - section/heading hierarchy

    3. Extract the citation graph (document → document)
        - "This Ruling replaces TR 2018/5" → relationship_type=replaces
        - "Refer to PCG 2018/9" → relationship_type=cites
        - DIS-specific: "Considers the decision in FCT v Pike [2020]…" → considers

    4. Extract legislation references
        - "section 8-1 of the ITAA 1997" → legislation_references row
        - subsections and paragraphs: "subsection 8-1(2)(a)"

    5. Extract case-law references
        - "FCT v Pike [2020] FCAFC 158" → case_references row

    6. Extract topic mappings from the ATO TOC (we attach these from the
       enumeration stage; see enrich step at the end)

Design philosophy:
    - The ATO uses CONSISTENT HTML templates per document type. Each parser
      file targets one type. Adding a new type is a single-file change.
    - Parsers should be IDEMPOTENT — running twice over the same version
      produces the same row set. We use ON CONFLICT DO UPDATE everywhere.
    - Heuristics here are explicit. When a heuristic is uncertain, attach a
      `parser_confidence` field rather than silently mislabelling.

Run:
    python 03_parse_document.py                       # parse everything unparsed
    python 03_parse_document.py --docid TXR/TR20241/NAT/ATO/00001
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Optional

from bs4 import BeautifulSoup, Tag
from supabase import create_client

log = logging.getLogger("parse")

# ---------------------------------------------------------------------------
# Regex library — citations & references
# ---------------------------------------------------------------------------

# Document short-codes inside body text: "TR 2024/1", "GSTR 2024/D2", "PS LA 2025/2"
DOC_CITATION_RE = re.compile(
    r"\b(TR|TD|LCR|GSTR|GSTD|MT|SGR|SGD|CR|PR|FBTR|PCG|PS\s*LA|ATO\s*ID|DIS|TA|SMSFR|SMSFD|ITR)\s+"
    r"(\d{2,4})\s*/\s*(D?\d+[A-Z]?)\b"
)

# Legislation refs — sections, divisions, subdivisions
# Examples:
#   "section 8-1 of the ITAA 1997"
#   "subsection 6(1)"
#   "Division 974 of the Income Tax Assessment Act 1997"
#   "paragraph 8-1(2)(a)"
LEGISLATION_RE = re.compile(
    r"\b(?:(section|sections|subsection|subsections|paragraph|paragraphs|Division|Divisions|Subdivision|Subdivisions|Part|Parts|Schedule)s?)\s+"
    r"([\dA-Z][\d\-A-Z()\s,]*?)"
    r"(?:\s+of\s+(?:the\s+)?(?:" 
    r"(Income\s+Tax\s+Assessment\s+Act\s+(?:1997|1936)|"
    r"ITAA\s*(?:1997|97|1936|36)|"
    r"A\s+New\s+Tax\s+System\s+\(Goods\s+and\s+Services\s+Tax\)\s+Act\s+1999|"
    r"GST\s+Act|"
    r"Taxation\s+Administration\s+Act\s+1953|"
    r"TAA\s*1953|"
    r"Fringe\s+Benefits\s+Tax\s+Assessment\s+Act\s+1986|"
    r"FBT\s+Act|"
    r"Superannuation\s+Guarantee\s+\(Administration\)\s+Act\s+1992|"
    r"SG\s+Act"
    r")))",
    re.IGNORECASE,
)

# Case citations — Australian style: "Name v Name [YEAR] COURT NUMBER"
# Courts: HCA, FCAFC, FCA, FedCFamC1A/G, AAT, ART, NSWSC, VSC, etc.
CASE_CITATION_RE = re.compile(
    r"([A-Z][\w&'’.\- ]+?\s+v[s]?\s+[A-Z][\w&'’.\- ]+?)\s+"
    r"\[(\d{4})\]\s+([A-Z]{2,8})\s+(\d+)"
)


_CASE_NAME_NORMALISE_RE = re.compile(r"[^a-z0-9]+")


def _normalise_case_name(name: str) -> str:
    """Lower-case, strip common case prefixes ("Re ", "FCT v "), collapse
    non-alphanumerics. Used to match parser-extracted case citations to
    `cases` master rows populated by the Stage 1a enumerator. Match rate
    won't be 100% — historical / foreign citations may stay unlinked, which
    is fine (case_references.case_id is nullable).
    """
    s = (name or "").lower().strip()
    for prefix in ("re ", "fct v ", "commissioner of taxation v ", "the commissioner of taxation v "):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    return _CASE_NAME_NORMALISE_RE.sub("-", s).strip("-")

# Cue phrases for relationship types
RELATIONSHIP_CUES = [
    (re.compile(r"\b(?:this (?:Ruling|Determination|Guideline|Statement)\s+)?replaces?\b", re.I), "replaces"),
    (re.compile(r"\bsupersedes?\b", re.I), "replaces"),
    (re.compile(r"\bwithdrawn? and replaced by\b", re.I), "replaced_by"),
    (re.compile(r"\bamends?\b", re.I), "amends"),
    (re.compile(r"\bconsolidates?\b", re.I), "consolidates"),
    (re.compile(r"\bwithdraws?\b", re.I), "withdraws"),
    (re.compile(r"\bconsider(?:s|ed)?\b", re.I), "considers"),  # DIS-specific
]


# ---------------------------------------------------------------------------
# Data carriers
# ---------------------------------------------------------------------------
@dataclass
class ParsedParagraph:
    paragraph_number: str
    ordinal: int
    heading_path: list[str]
    text: str
    html: str


@dataclass
class ParsedDocument:
    docid: str
    short_code: str
    doc_type: str
    title: str
    short_title: Optional[str]
    body_html: str
    body_text: str
    status: Optional[str]  # None when the HTML status panel wasn't parseable
    date_issued: Optional[str]
    date_effect_from: Optional[str]
    date_effect_to: Optional[str]
    date_withdrawn: Optional[str]
    date_last_modified: Optional[str]
    paragraphs: list[ParsedParagraph] = field(default_factory=list)
    citations: list[dict] = field(default_factory=list)
    legislation: list[dict] = field(default_factory=list)
    cases: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Core parsing
# ---------------------------------------------------------------------------
def parse_dates(soup: BeautifulSoup) -> dict:
    """Extract dates from the ATO 'Status' panel that appears on every doc.

    The ATO template includes a table or sidebar with:
        Date of effect:   1 July 2024
        Date of issue:    15 May 2024
        Status:           Current
        Date of withdrawal: (if applicable)
    """
    out: dict[str, Optional[str]] = {
        "date_issued": None,
        "date_effect_from": None,
        "date_effect_to": None,
        "date_withdrawn": None,
        "date_last_modified": None,
        # None (not "current") so persist() only writes status when the HTML
        # status panel actually matched — a default would clobber a correct
        # status (e.g. the short_code backfill's 'withdrawn') on re-parse.
        "status": None,
    }

    # The status panel is usually identified by class names like
    # "documentInformation", "statusPanel", or a dt/dd list with role headers.
    # Try a few selectors with graceful fallback.
    text_blocks = soup.find_all(["dl", "table", "aside", "div"], class_=re.compile(r"status|information|metadata", re.I))
    candidate_text = " \n".join(b.get_text(" ", strip=True) for b in text_blocks)

    def grab(label: str) -> Optional[str]:
        m = re.search(rf"{label}\s*:?\s*([0-9]{{1,2}}\s+\w+\s+\d{{4}}|\d{{4}}-\d{{2}}-\d{{2}})", candidate_text, re.I)
        return m.group(1) if m else None

    out["date_issued"] = grab(r"Date of (?:issue|publication)")
    out["date_effect_from"] = grab(r"Date of effect")
    out["date_withdrawn"] = grab(r"Date of withdrawal|Withdrawn on")
    out["date_last_modified"] = grab(r"Last (?:modified|updated)")

    status_match = re.search(r"Status\s*:?\s*(Current|Draft|Withdrawn|Replaced|Archived|Consolidated)", candidate_text, re.I)
    if status_match:
        out["status"] = status_match.group(1).lower()

    return out


def parse_title(soup: BeautifulSoup) -> str:
    h1 = soup.find("h1")
    if h1:
        return h1.get_text(" ", strip=True)
    return soup.title.get_text(strip=True) if soup.title else "(untitled)"


_PARA_NUM_RE = re.compile(r"^\s*(\d+[A-Z]?(?:\([a-z0-9]+\))?)\.\s+(.*)$", re.S)

# The ATO renders a "document history" widget — a small table whose header row
# is some subset of Date / Version / Change (plus a "You are here" marker) — on
# essentially every doc. It's metadata, never body content, so the unnumbered
# fallback must skip it (otherwise ATO IDs and old TDs pick it up as paragraphs).
_HISTORY_HEADERS = {"", "date", "version", "change", "you are here"}


def _is_history_table(table: Tag) -> bool:
    first = table.find("tr")
    if not first:
        return False
    cells = [c.get_text(" ", strip=True).rstrip(":").lower()
             for c in first.find_all(["td", "th"])]
    cells = [c for c in cells if c]
    return bool(cells) and all(c in _HISTORY_HEADERS for c in cells)


def parse_paragraphs(soup: BeautifulSoup) -> list[ParsedParagraph]:
    """Extract paragraph-level content, tracking section-heading depth.

    Two passes. The numbered pass handles the dominant ruling template
    (TR/GSTR/TD/PR/CR/…), where paragraphs begin "1.", "2.", "2A." inside
    <p> tags. If — and only if — the numbered pass finds nothing, an
    unnumbered fallback runs for the other ATO templates:

      - ATO IDs and older / draft rulings: prose in <p class="indentlevelN">
        under <h3> section headings (Issue / Decision / Facts / Reasons),
        with no leading numbers.
      - Compendiums (the "EC" docs): an Issue / ATO-response grid where the
        body lives in <table> rows, not <p> tags.

    The fallback assigns synthetic sequential paragraph_numbers ("1", "2", …)
    since these templates carry none and the column is NOT NULL + unique per
    document. Because the fallback only runs when the numbered pass is empty,
    those synthetic numbers can never collide with real ones, and no
    already-numbered document changes.
    """
    body = soup.find("article") or soup.find("main") or soup.body or soup
    numbered = _parse_numbered(body)
    if numbered:
        return numbered
    return _parse_unnumbered(body)


def _parse_numbered(body: Tag) -> list[ParsedParagraph]:
    out: list[ParsedParagraph] = []
    heading_stack: list[str] = []
    ordinal = 0
    for el in body.descendants:
        if not isinstance(el, Tag):
            continue
        if el.name in ("h1", "h2", "h3", "h4"):
            depth = int(el.name[1]) - 2
            heading_stack = heading_stack[:depth] + [el.get_text(" ", strip=True)]
            continue
        if el.name == "p":
            text = el.get_text(" ", strip=True)
            m = _PARA_NUM_RE.match(text)
            if m:
                ordinal += 1
                out.append(ParsedParagraph(
                    paragraph_number=m.group(1),
                    ordinal=ordinal,
                    heading_path=list(heading_stack),
                    text=m.group(2),
                    html=str(el),
                ))
    return out


def _parse_unnumbered(body: Tag) -> list[ParsedParagraph]:
    """Fallback: indentlevel prose paragraphs + content-table rows, in document
    order, for templates that don't number their paragraphs."""
    out: list[ParsedParagraph] = []
    heading_stack: list[str] = []
    ordinal = 0
    for el in body.descendants:
        if not isinstance(el, Tag):
            continue
        if el.name in ("h1", "h2", "h3", "h4"):
            depth = int(el.name[1]) - 2
            heading_stack = heading_stack[:depth] + [el.get_text(" ", strip=True)]
            continue
        if el.name == "p":
            # <p>s inside a table are handled when we reach the <table> node.
            if el.find_parent("table") is not None:
                continue
            if not any(c.startswith("indentlevel") for c in (el.get("class") or [])):
                continue  # skip metadata / chrome paragraphs (references, ISSN, CTA)
            text = el.get_text(" ", strip=True)
            if not text:
                continue
            ordinal += 1
            out.append(ParsedParagraph(
                paragraph_number=str(ordinal),
                ordinal=ordinal,
                heading_path=list(heading_stack),
                text=text,
                html=str(el),
            ))
        elif el.name == "table":
            if el.find_parent("table") is not None:
                continue  # nested table — already covered by the outer table's rows
            if _is_history_table(el):
                continue  # document-history widget, not body content
            for tr in el.find_all("tr"):
                cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
                row_text = " | ".join(c for c in cells if c)
                if not row_text:
                    continue
                ordinal += 1
                out.append(ParsedParagraph(
                    paragraph_number=str(ordinal),
                    ordinal=ordinal,
                    heading_path=list(heading_stack),
                    text=row_text,
                    html=str(tr),
                ))
    if out:
        return out

    # Tier B (only if nothing else matched): some withdrawn / duplicate ATO IDs
    # carry their entire content as an <div class="alert ..."> notice
    # ("This Interpretative Decision is a duplicate of … and is therefore
    # withdrawn."). Capture those so the doc isn't left structureless. Runs
    # last so docs with real body content never pick up these notice boxes.
    ordinal = 0
    for div in body.find_all("div", class_="alert"):
        if div.find_parent("div", class_="alert") is not None:
            continue  # nested alert — covered by the outer one
        text = div.get_text(" ", strip=True)
        if not text:
            continue
        ordinal += 1
        out.append(ParsedParagraph(
            paragraph_number=str(ordinal),
            ordinal=ordinal,
            heading_path=[],
            text=text,
            html=str(div),
        ))
    return out


def extract_citations(text: str) -> list[dict]:
    """Find document short-codes and infer relationship type from cue words."""
    out = []
    for m in DOC_CITATION_RE.finditer(text):
        doc_type = re.sub(r"\s+", " ", m.group(1)).strip().upper()
        # Normalise PSLA / PS LA
        if doc_type == "PSLA":
            doc_type = "PS LA"
        year = m.group(2)
        number = m.group(3)
        if len(year) == 2:
            year = ("19" + year) if int(year) > 50 else ("20" + year)
        short_code = f"{doc_type} {year}/{number}"

        # Look back 80 chars for a relationship cue.
        start = max(0, m.start() - 80)
        prefix = text[start : m.start()]
        rel = "cites"
        for pattern, candidate in RELATIONSHIP_CUES:
            if pattern.search(prefix):
                rel = candidate
                break

        out.append({
            "target_docid_raw": short_code,
            "relationship_type": rel,
            "context_snippet": text[max(0, m.start() - 60) : m.end() + 60],
        })
    return out


def extract_legislation(text: str) -> list[dict]:
    out = []
    for m in LEGISLATION_RE.finditer(text):
        kind = m.group(1)
        section_raw = re.sub(r"\s+", " ", m.group(2)).strip()
        act_text = m.group(3) or ""
        # Normalise to short_name.
        act_short = _normalise_act(act_text)
        out.append({
            "act_short_name": act_short,
            "section_raw": section_raw,
            "kind": kind.lower(),
            "context_snippet": text[max(0, m.start() - 60) : m.end() + 60],
        })
    return out


def _normalise_act(s: str) -> str:
    s_low = s.lower()
    if "1997" in s_low or "itaa 1997" in s_low or "itaa97" in s_low:
        return "ITAA 1997"
    if "1936" in s_low or "itaa 1936" in s_low or "itaa36" in s_low:
        return "ITAA 1936"
    if "gst" in s_low:
        return "GST Act"
    if "taa" in s_low or "administration act 1953" in s_low:
        return "TAA 1953"
    if "fringe" in s_low or "fbt" in s_low:
        return "FBT Act"
    if "superannuation guarantee" in s_low or "sg act" in s_low:
        return "Super Guarantee Act"
    return s.strip() or "UNKNOWN"


def extract_cases(text: str) -> list[dict]:
    out = []
    for m in CASE_CITATION_RE.finditer(text):
        out.append({
            "case_name": m.group(1).strip(),
            "decision_year": int(m.group(2)),
            "court": m.group(3),
            "citation": f"[{m.group(2)}] {m.group(3)} {m.group(4)}",
            "context_snippet": text[max(0, m.start() - 60) : m.end() + 60],
        })
    return out


# ---------------------------------------------------------------------------
# Compendium (EC) parsing — structured "issues raised / ATO response" grid
#
# A compendium (short_code suffix "EC", e.g. TD 2017/20EC) publishes the ATO's
# responses to comments on a draft ruling as an HTML table. Stage 3's normal
# paragraph pass already captures that table as pipe-delimited prose; this path
# captures it STRUCTURED — one row per issue, with a heuristic accept/reject
# outcome — into compendium_submissions. Re-parses the source HTML <table>
# (multi-line cells survive, unlike the flattened pipe text). Run via the
# --compendiums mode; the normal parse path is untouched.
# ---------------------------------------------------------------------------

# The "EC" suffix follows the parent's number (a digit), optionally with a
# re-issue round ("EC", "EC2", "EC-2"). The digit lookbehind avoids matching a
# short_code that merely ends in the letters "EC".
COMPENDIUM_SC_RE = re.compile(r"(?<=\d)EC(?:-?\d+)?$")

# Outcome classifier, checked in priority order so "change accommodated in
# part" → partial and "no change made" → rejected (not accepted). Conservative:
# a substantive response with no clear signal stays 'unknown', and the raw
# ato_response is always stored so a mis-label is visible/recoverable in the UI.
_OUTCOME_PARTIAL = re.compile(
    r"\b(in part|partly|partially|in principle|accommodated in part"
    r"|agreed in part|to some extent)\b", re.I)
# Negations first so "does not accept" / "no change" never read as accepted.
_OUTCOME_REJECTED = re.compile(
    r"\b(no change|not agreed|does not agree|do not agree|not accepted"
    r"|does not accept|do not accept|cannot accept|rejected|not adopted"
    r"|no amendment|declined|disagrees?|not supported)\b", re.I)
_OUTCOME_ACCEPTED = re.compile(
    r"\b(agrees?|agreed|accepts?|accepted|amend(?:ed|ment|ments)?|accommodat(?:ed|es)"
    r"|inserted|added|adjusted|incorporat(?:e|es|ed|ing)|change made"
    r"|clarif(?:ied|y)|resolved)\b", re.I)
_OUTCOME_NOTED = re.compile(r"\b(noted|no response (?:is )?required)\b", re.I)


def classify_outcome(response: Optional[str]) -> str:
    t = response or ""
    if _OUTCOME_PARTIAL.search(t):
        return "partial"
    if _OUTCOME_REJECTED.search(t):
        return "rejected"
    if _OUTCOME_ACCEPTED.search(t):
        return "accepted"
    if _OUTCOME_NOTED.search(t):
        return "noted"
    return "unknown"


_ISSUE_HDR_RE = re.compile(r"issue", re.I)
_RESP_HDR_RE = re.compile(r"response|action|tax office", re.I)


def find_issues_table(soup: BeautifulSoup) -> Optional[tuple[Tag, list[str], int]]:
    """Return (table, header_cells, header_row_index) for the compendium's
    issues grid — the column-header row reads like 'Issue … | … | Response/
    Action'. The header is usually row 0, but older docs (e.g. GSTR 2001/8EC)
    put a single-cell 'Summary of issues raised and responses' title row first
    and the real column headers in row 1, so scan rows for the first one with
    ≥2 cells matching both cues. Skips the 'Relying on this Compendium'
    disclaimer table. If several tables match, picks the one with the most
    rows (the real grid)."""
    best: Optional[tuple[Tag, list[str], int]] = None
    best_rows = -1
    for t in soup.find_all("table"):
        trs = t.find_all("tr")
        for hi, tr in enumerate(trs[:3]):  # header is at or near the top
            hdr = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            joined = " ".join(hdr).lower()
            if len(hdr) >= 2 and _ISSUE_HDR_RE.search(joined) and _RESP_HDR_RE.search(joined):
                nrows = len(trs)
                if nrows > best_rows:
                    best, best_rows = (t, hdr, hi), nrows
                break
    return best


def parse_compendium(soup: BeautifulSoup) -> list[dict]:
    """Extract structured submission rows from a compendium's HTML.

    The ATO has used two templates over time:

    1. **Table format** (pre-2026): one <table> with rows per issue. Columns
       are mapped POSITIONALLY (robust to header-text drift — 'Issue No.'/
       'Issue number'/'Issue(s) raised', 'ATO Response'/'Tax Office Response/
       Action taken', …): issue_no = first column, ato_response = last column,
       issue_raised = the column(s) between, entities = column 1 only in the
       older 4-column format (Issue No. | Entity/ies commenting | …).

    2. **h3-anchored format** (2026+): per-issue <h3>Issue N – …</h3> followed
       by label-anchored <p>s ("Paragraph or section of draft product",
       "Issue raised", "ATO response", "Outcome"). The explicit Outcome label
       — values like "No change" / "Clarified" / "Position amended" — gives a
       far more reliable classification than the keyword heuristic; we use it
       when present.

    Try table format first; if it yields zero rows, fall back to h3 format."""
    rows = _parse_compendium_table(soup)
    if rows:
        return rows
    return _parse_compendium_h3(soup)


def _parse_compendium_table(soup: BeautifulSoup) -> list[dict]:
    found = find_issues_table(soup)
    if not found:
        return []
    table, hdr, header_idx = found
    has_entities = len(hdr) >= 4 and bool(
        re.search(r"entit|commenting", " ".join(hdr), re.I))
    out: list[dict] = []
    for tr in table.find_all("tr")[header_idx + 1:]:  # rows after the header
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if len(cells) < 2 or not any(c.strip() for c in cells):
            continue
        issue_no = cells[0].strip().rstrip(".") or None
        ato_response = cells[-1].strip() or None
        if has_entities and len(cells) >= 4:
            entities = cells[1].strip() or None
            issue_raised = " ".join(c.strip() for c in cells[2:-1] if c.strip()) or None
        else:
            entities = None
            issue_raised = " ".join(c.strip() for c in cells[1:-1] if c.strip()) or None
        out.append({
            "issue_no": issue_no,
            "entities": entities,
            "issue_raised": issue_raised,
            "ato_response": ato_response,
            "outcome": classify_outcome(ato_response),
        })
    return out


# The 2026+ h3-anchored layout. The summary section starts at an h2/h3/h4
# titled "Summary of issues raised and responses"; each issue is a sibling
# <h3>Issue N – …</h3> followed by label-anchored <p> tags.
_SUMMARY_HEADING_RE = re.compile(r"summary of issues raised and responses", re.I)
_ISSUE_HEADING_RE = re.compile(r"^\s*Issue\s*(\d+)\b", re.I)


# Map an ATO Outcome label (the 2026 format's explicit value) → our enum.
def _classify_outcome_label(label: str) -> str:
    s = (label or "").strip().lower()
    if any(k in s for k in (
        "partly agreed", "agreed in part", "partly amended", "in part",
        "partial",
    )):
        return "partial"
    if any(k in s for k in (
        "no change", "not agreed", "does not agree", "do not agree",
        "not accepted", "rejected", "not adopted", "no amendment",
        "declined", "disagree",
    )):
        return "rejected"
    if any(k in s for k in (
        "clarified", "position amended", "amended", "agreed", "accepted",
        "added", "inserted", "incorporated", "change made", "adjusted",
        "resolved",
    )):
        return "accepted"
    if "noted" in s or "no response" in s:
        return "noted"
    return "unknown"


def _parse_compendium_h3(soup: BeautifulSoup) -> list[dict]:
    anchor: Optional[Tag] = None
    for h in soup.find_all(["h2", "h3", "h4"]):
        if _SUMMARY_HEADING_RE.search(h.get_text(" ", strip=True)):
            anchor = h
            break
    if anchor is None:
        return []

    out: list[dict] = []
    current_no: Optional[str] = None
    current_section: Optional[str] = None
    buckets: dict[str, list[str]] = {}

    def flush():
        nonlocal current_no, buckets
        if current_no is None:
            return
        ref = " ".join(buckets.get("ref", [])).strip()
        issue = " ".join(buckets.get("issue", [])).strip()
        response = " ".join(buckets.get("response", [])).strip()
        outcome_label = " ".join(buckets.get("outcome", [])).strip()
        outcome = (
            _classify_outcome_label(outcome_label)
            if outcome_label
            else classify_outcome(response)
        )
        # If we have an explicit outcome label, surface it in ato_response so
        # the UI shows it verbatim regardless of how we classified.
        if outcome_label:
            response = (
                f"{response} [Outcome: {outcome_label}]"
                if response else f"Outcome: {outcome_label}"
            )
        out.append({
            "issue_no": current_no,
            "entities": None,
            "issue_raised": (
                f"({ref}) {issue}" if ref and issue
                else issue or ref or None
            ),
            "ato_response": response or None,
            "outcome": outcome,
        })
        current_no = None
        buckets = {}

    sib = anchor.find_next_sibling()
    while sib is not None:
        if sib.name == "h2":
            break  # left the Summary section
        if sib.name == "h3":
            text = sib.get_text(" ", strip=True)
            m = _ISSUE_HEADING_RE.match(text)
            if m:
                flush()
                current_no = m.group(1)
                current_section = None
                buckets = {}
            else:
                break  # an h3 that isn't "Issue N — …" → end of section
        elif sib.name == "p" and current_no is not None:
            text = sib.get_text(" ", strip=True)
            text_lc = text.lower().strip()
            if text_lc.startswith("paragraph or section of draft product"):
                current_section = "ref"; buckets["ref"] = []
            elif text_lc.startswith("issue raised"):
                current_section = "issue"; buckets["issue"] = []
            elif (text_lc.startswith("ato response")
                  or text_lc.startswith("tax office response")):
                current_section = "response"; buckets["response"] = []
            elif text_lc == "outcome" or text_lc.startswith("outcome:"):
                current_section = "outcome"; buckets["outcome"] = []
            elif current_section and text:
                buckets[current_section].append(text)
        sib = sib.find_next_sibling()
    flush()
    return out


def resolve_parent(short_code: str, doc_lookup: Optional[dict[str, str]]) -> tuple[Optional[str], Optional[str]]:
    """Parent ruling = the compendium short_code with its EC suffix stripped.
    Combined compendiums ('TD 2010/14EC; TD 2010/15EC') are ';'-split; the first
    resolvable parent wins. Returns (parent_short_code, parent_id|None)."""
    segments = [s.strip() for s in short_code.split(";") if s.strip()] or [short_code]
    for seg in segments:
        parent_sc = COMPENDIUM_SC_RE.sub("", seg).strip()
        pid = doc_lookup.get(parent_sc) if doc_lookup else None
        if pid:
            return parent_sc, pid
    return (COMPENDIUM_SC_RE.sub("", segments[0]).strip() or None), None


def persist_compendium(
    supabase,
    compendium_doc_id: str,
    compendium_short_code: str,
    parent_id: Optional[str],
    submissions: list[dict],
) -> None:
    """Write structured submissions + the parent's responded_to_by edge.
    Idempotent: clears this compendium's existing rows/edge before re-inserting,
    so re-runs are safe."""
    supabase.table("compendium_submissions").delete().eq(
        "compendium_document_id", compendium_doc_id).execute()
    rows = [
        {
            "compendium_document_id": compendium_doc_id,
            "parent_document_id": parent_id,
            "ordinal": i,
            "issue_no": s["issue_no"],
            "entities": s["entities"],
            "issue_raised": s["issue_raised"],
            "ato_response": s["ato_response"],
            "outcome": s["outcome"],
        }
        for i, s in enumerate(submissions, start=1)
    ]
    _bulk_insert(supabase, "compendium_submissions", rows)

    # responded_to_by: the parent ruling IS responded to by this compendium.
    # Resolves §6 item 6 of the HANDOFF (EC docs weren't auto-linked). Cleared
    # by the compendium's raw short_code first to stay idempotent.
    if parent_id:
        supabase.table("document_relationships").delete().eq(
            "relationship_type", "responded_to_by").eq(
            "target_docid_raw", compendium_short_code).execute()
        try:
            supabase.table("document_relationships").insert(_strip_nulls({
                "source_document_id": parent_id,
                "target_document_id": compendium_doc_id,
                "target_docid_raw": compendium_short_code,
                "relationship_type": "responded_to_by",
            })).execute()
        except Exception as e:
            log.warning("responded_to_by edge for %s failed: %s", compendium_short_code, e)


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------
def parse_html(docid: str, short_code: str, doc_type: str, html: str) -> ParsedDocument:
    # BS4's lxml backend crashes on real ATO HTML under lxml 6.x with
    # "ValueError: not enough values to unpack" in its SAX builder
    # (known BS4 4.12 × lxml 6 incompatibility). html.parser is Python's
    # stdlib HTML parser — slightly slower on huge docs, but bullet-proof
    # for our corpus shape. Removing lxml from this path also closes the
    # last reason we needed lxml in requirements.txt.
    soup = BeautifulSoup(html, "html.parser")
    title = parse_title(soup)
    dates = parse_dates(soup)
    paragraphs = parse_paragraphs(soup)
    body_text = soup.get_text(" \n", strip=True)

    citations = extract_citations(body_text)
    legislation = extract_legislation(body_text)
    cases = extract_cases(body_text)

    return ParsedDocument(
        docid=docid,
        short_code=short_code,
        doc_type=doc_type,
        title=title,
        short_title=None,
        body_html=str(soup.find("article") or soup.body or soup),
        body_text=body_text,
        paragraphs=paragraphs,
        citations=citations,
        legislation=legislation,
        cases=cases,
        **dates,
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def load_case_lookup(supabase) -> dict[tuple[str, int], str]:
    """Build an in-memory (normalised_name, year) → case.id map from the
    `cases` master table populated by Stage 1a. Run once at parser startup
    so each citation lookup is O(1) instead of a Supabase round-trip.

    Paginates via id cursor to handle the full ~18k row set (PostgREST
    caps unbounded SELECTs at 1000 rows).
    """
    lookup: dict[tuple[str, int], str] = {}
    last_id = "00000000-0000-0000-0000-000000000000"
    while True:
        page = (
            supabase.table("cases")
            .select("id,case_name,decision_year")
            .gt("id", last_id).order("id").limit(1000)
            .execute().data
        )
        if not page:
            break
        for c in page:
            if c.get("decision_year") is None:
                continue
            key = (_normalise_case_name(c["case_name"]), c["decision_year"])
            lookup[key] = c["id"]
        last_id = page[-1]["id"]
    log.info("Loaded %d cases for citation cross-link", len(lookup))
    return lookup


def _bulk_insert(supabase, table: str, rows: list[dict]) -> tuple[int, int]:
    """Insert all `rows` in one POST. On failure, fall back to per-row inserts
    so a single malformed row only drops itself, not the whole doc.

    Returns (succeeded, failed). Never raises for a single bad row — the
    parser's per-doc exception isolation handles systemic failures
    separately.

    Replaces the previous per-row insert loops in persist(). For a typical
    TR doc this cuts ~50 round trips down to 1 for paragraphs alone.
    """
    if not rows:
        return (0, 0)
    rows = _strip_nulls(rows)
    try:
        supabase.table(table).insert(rows).execute()
        return (len(rows), 0)
    except Exception as e:
        log.warning(
            "Batch insert to %s failed (%d rows): %s. Falling back to per-row.",
            table, len(rows), e,
        )
        ok, bad = 0, 0
        for r in rows:
            try:
                supabase.table(table).insert(r).execute()
                ok += 1
            except Exception as e2:
                bad += 1
                # Best-effort label; persist() callers know which doc this is.
                label = r.get("docid") or r.get("document_id") or "row"
                log.error("Skipped bad %s row [%s]: %s", table, label, e2)
        return (ok, bad)


def load_act_lookup(supabase) -> dict[str, str]:
    """Pre-load acts.short_name → id (~6 rows). Saves a round trip per
    legislation_reference insert."""
    out: dict[str, str] = {}
    for r in supabase.table("acts").select("id,short_name").execute().data or []:
        out[r["short_name"]] = r["id"]
    log.info("Loaded %d acts for legislation cross-link", len(out))
    return out


def load_doc_lookup(supabase) -> dict[str, str]:
    """Pre-load documents.short_code → id (~15k rows). Saves a round trip
    per citation target lookup in persist(). Cursor-paginated. Where the
    same short_code matches multiple rows (e.g. draft TR + its compendium),
    first-seen wins — deterministic by id order."""
    out: dict[str, str] = {}
    last_id = "00000000-0000-0000-0000-000000000000"
    while True:
        page = (
            supabase.table("documents")
            .select("id,short_code")
            .gt("id", last_id).order("id").limit(1000)
            .execute().data
        )
        if not page:
            break
        for r in page:
            sc = r["short_code"]
            if sc and sc not in out:   # first-seen wins
                out[sc] = r["id"]
        last_id = page[-1]["id"]
    log.info("Loaded %d unique short_codes for citation cross-link", len(out))
    return out


def _strip_nulls(obj):
    """Recursively strip NULL bytes (\\u0000) from any string field.

    Postgres `text` columns reject NULL bytes with error 22P05
    "unsupported Unicode escape sequence". Some ATO HTML contains stray
    NULL bytes (PDF-embedded text, encoding artefacts) which the parser
    surfaces as-is in title / body_text / paragraph text. Sanitising at
    the write boundary is the cheapest fix — applies to every persist()
    payload uniformly.
    """
    if isinstance(obj, str):
        return obj.replace("\x00", "")
    if isinstance(obj, dict):
        return {k: _strip_nulls(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_strip_nulls(v) for v in obj]
    return obj


def persist(
    supabase,
    p: ParsedDocument,
    case_lookup: Optional[dict[tuple[str, int], str]] = None,
    doc_lookup: Optional[dict[str, str]] = None,
    act_lookup: Optional[dict[str, str]] = None,
) -> None:
    """Write parsed output to Supabase.

    Round-trip budget per doc (post-2026-05-23 batching refactor):
      1 documents.update + 1 documents.select(id) +
      4 deletes (paragraphs, relationships, legislation_refs, case_refs) +
      4 batched inserts (one per table) = 10 calls/doc baseline
    Down from ~38 calls/doc in the per-row implementation, which was the
    dominant cost in the original ~10s/doc parser pace.

    Ownership split (per ADR-equivalent in commit ede76da; status updated
    2026-05-26):
      - Enumerator (Stage 1a) OWNS: title, short_code, doc_type, year,
        number, is_draft, ato_url. Higher-quality from the ATO API than what
        the parser can extract from rendered HTML. It seeds status='current'
        as a placeholder only.
      - Parser (Stage 3) OWNS: body_html, body_text, date_* fields, **status
        (when the HTML status panel is parseable — the ATO HTML is the ground
        truth; otherwise left untouched)**, and downstream relational tables.
        (The short_code backfill, scripts/backfill_status_from_shortcode.py,
        seeded status for the existing corpus; this keeps it correct going
        forward.)

    Per-row error isolation comes from _bulk_insert: a malformed row only
    drops itself, the rest of the batch lands. Per-doc isolation lives one
    level up in main()'s loop.
    """
    # 1. documents.update — body + dates only, never overwrite enumerator
    #    metadata. Skip date_* fields that are None so we don't clobber
    #    prior values.
    upd: dict = {
        "body_html": p.body_html,
        "body_text": p.body_text,
    }
    for date_field in ("date_issued", "date_effect_from", "date_effect_to",
                       "date_withdrawn", "date_last_modified"):
        v = getattr(p, date_field, None)
        if v is not None:
            upd[date_field] = v
    # status only when the HTML status panel matched (p.status is None
    # otherwise) — never overwrite a correct status with a parser default.
    if p.status is not None:
        upd["status"] = p.status
    supabase.table("documents").update(_strip_nulls(upd)).eq("docid", p.docid).execute()

    # documents.id lookup. Could be eliminated with doc_id pre-loading too,
    # but the parser already iterates docs serially with their id in hand —
    # accept the one extra round trip for now.
    doc_id = (
        supabase.table("documents")
        .select("id").eq("docid", p.docid).single().execute().data["id"]
    )

    # 2. Paragraphs — wipe and re-insert.
    supabase.table("document_paragraphs").delete().eq("document_id", doc_id).execute()
    para_rows = [
        {
            "document_id": doc_id,
            "paragraph_number": pp.paragraph_number,
            "ordinal": pp.ordinal,
            "heading_path": pp.heading_path,
            "text": pp.text,
            "html": pp.html,
        } for pp in p.paragraphs
    ]
    _bulk_insert(supabase, "document_paragraphs", para_rows)

    # 3. Citations — link to targets via pre-loaded doc_lookup.
    supabase.table("document_relationships").delete().eq("source_document_id", doc_id).execute()
    rel_rows = []
    for c in p.citations:
        tgt_id = doc_lookup.get(c["target_docid_raw"]) if doc_lookup else None
        rel_rows.append({
            "source_document_id": doc_id,
            "target_document_id": tgt_id,
            "target_docid_raw": c["target_docid_raw"],
            "relationship_type": c["relationship_type"],
            "context_snippet": c["context_snippet"],
        })
    _bulk_insert(supabase, "document_relationships", rel_rows)

    # 4. Legislation — link to acts via pre-loaded act_lookup.
    supabase.table("legislation_references").delete().eq("document_id", doc_id).execute()
    leg_rows = []
    for lr in p.legislation:
        act_id = act_lookup.get(lr["act_short_name"]) if act_lookup else None
        leg_rows.append({
            "document_id": doc_id,
            "act_id": act_id,
            "act_short_name": lr["act_short_name"],
            "section_raw": lr["section_raw"],
            "context_snippet": lr["context_snippet"],
        })
    _bulk_insert(supabase, "legislation_references", leg_rows)

    # 5. Cases — link to master cases table via pre-loaded case_lookup.
    supabase.table("case_references").delete().eq("document_id", doc_id).execute()
    case_rows = []
    for cs in p.cases:
        case_id = None
        if case_lookup is not None and cs.get("decision_year") is not None:
            case_id = case_lookup.get(
                (_normalise_case_name(cs["case_name"]), cs["decision_year"])
            )
        case_rows.append({
            "document_id": doc_id,
            "case_name": cs["case_name"],
            "citation": cs["citation"],
            "court": cs["court"],
            "decision_year": cs["decision_year"],
            "context_snippet": cs["context_snippet"],
            "case_id": case_id,
        })
    _bulk_insert(supabase, "case_references", case_rows)


def run_compendiums(supabase, rows: list[dict], doc_lookup: dict[str, str]) -> int:
    """Parse the EC compendium docs among `rows` into compendium_submissions
    (and write each parent's responded_to_by edge). Logs per-doc issue counts
    and an outcome tally for the validation gate."""
    targets = [r for r in rows if COMPENDIUM_SC_RE.search(r.get("short_code") or "")]
    log.info("Compendium mode: %d EC docs of %d candidate rows", len(targets), len(rows))
    tally: Counter = Counter()
    docs_ok = docs_empty = docs_fail = unresolved = 0
    total_subs = 0
    for row in targets:
        try:
            version = (
                supabase.table("document_versions")
                .select("raw_html_path")
                .eq("document_id", row["id"])
                .order("version_number", desc=True)
                .limit(1)
                .execute().data
            )
            if not version:
                log.warning("No version for %s", row["docid"])
                docs_fail += 1
                continue
            html = supabase.storage.from_("ato-raw-html").download(
                version[0]["raw_html_path"]).decode("utf-8")
            soup = BeautifulSoup(html, "html.parser")
            subs = parse_compendium(soup)
            parent_sc, parent_id = resolve_parent(row["short_code"], doc_lookup)
            persist_compendium(supabase, row["id"], row["short_code"], parent_id, subs)
            for s in subs:
                tally[s["outcome"]] += 1
            total_subs += len(subs)
            if subs:
                docs_ok += 1
            else:
                docs_empty += 1
            if not parent_id:
                unresolved += 1
            log.info("Compendium %s: %d issues, parent=%s%s",
                     row["short_code"], len(subs), parent_sc,
                     "" if parent_id else " (UNRESOLVED)")
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log.exception("Skipped compendium %s: %s", row.get("docid"), str(e)[:200])
            docs_fail += 1
    log.info(
        "Compendium run complete: %d ok, %d empty, %d failed, %d parent-unresolved; "
        "%d submissions; outcomes=%s",
        docs_ok, docs_empty, docs_fail, unresolved, total_subs, dict(tally))
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--docid", help="Parse a single docid")
    p.add_argument("--docids-file",
                   help="Parse only the docids listed in this file (one per line). "
                        "Used to re-parse a targeted set, e.g. the Stage-3 residual.")
    p.add_argument("--compendiums", action="store_true",
                   help="Compendium mode: parse EC docs (short_code suffix 'EC') "
                        "into the structured compendium_submissions table instead "
                        "of the normal paragraph/citation parse. Respects --docid "
                        "/ --docids-file for a targeted set; otherwise all EC docs.")
    args = p.parse_args()

    supabase = create_client(
        os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    )

    # Pre-load the cases master table so each parsed citation can be linked
    # to its `cases.id` in O(1). Populated by Stage 1a's Cases-tree walk
    # (see scrapers/01_enumerate_ato.py::derive_case_from_breadcrumb and
    # ADR-0006). Safe if the table is empty — case_id just stays NULL.
    # Compendium mode only needs doc_lookup (for parent resolution), so skip the
    # heavy case/act loads there.
    doc_lookup = load_doc_lookup(supabase)
    case_lookup = None if args.compendiums else load_case_lookup(supabase)
    act_lookup = None if args.compendiums else load_act_lookup(supabase)

    # Discover documents that need parsing.
    #
    # Original design (HANDOFF) used `title='(pending parse)'` as the signal
    # that Stage 2 had fetched but Stage 3 hadn't parsed. In practice neither
    # Stage 1a nor Stage 2 sets that title — the enumerator sets the API title
    # at creation time and the fetcher leaves it alone. So that filter
    # matched zero rows.
    #
    # New signal: `documents.source_html_hash IS NOT NULL` means Stage 2 ran
    # successfully and there's a raw HTML in Storage to parse. Idempotency
    # comes from persist(): it deletes existing paragraphs / citations /
    # legislation_refs / case_refs for the doc before inserting fresh ones.
    # So re-runs are safe (just wasteful — see TODO for incremental).
    #
    # PostgREST caps unbounded SELECTs at 1000 rows; cursor-paginate by `id`
    # to handle the full ~15k-doc corpus.
    rows: list[dict] = []
    if args.docid:
        rows = (supabase.table("documents")
                .select("id,docid,short_code,doc_type,source_html_hash")
                .eq("docid", args.docid).execute().data)
    elif args.docids_file:
        with open(args.docids_file) as fh:
            wanted = [ln.strip() for ln in fh if ln.strip()]
        # PostgREST `in` filter has a practical URL-length cap; chunk the lookups.
        for i in range(0, len(wanted), 100):
            chunk = wanted[i:i + 100]
            rows.extend(supabase.table("documents")
                        .select("id,docid,short_code,doc_type,source_html_hash")
                        .in_("docid", chunk).execute().data)
        log.info("Loaded %d/%d requested docids", len(rows), len(wanted))
    else:
        last_id = "00000000-0000-0000-0000-000000000000"
        while True:
            page = (supabase.table("documents")
                    .select("id,docid,short_code,doc_type,source_html_hash")
                    .not_.is_("source_html_hash", "null")
                    .gt("id", last_id).order("id").limit(1000)
                    .execute().data)
            if not page:
                break
            rows.extend(page)
            last_id = page[-1]["id"]

    if args.compendiums:
        return run_compendiums(supabase, rows, doc_lookup)

    log.info("Parsing %d documents", len(rows))

    # TODO (incremental parsing): add a `documents.parsed_html_hash` column,
    # set it after persist(), and filter for
    # `source_html_hash != parsed_html_hash OR parsed_html_hash IS NULL` so
    # re-runs only touch new / changed docs. Out of scope for the first run.

    ok_count = 0
    fail_count = 0
    for row in rows:
        # Per-doc exception isolation: any error in download / parse /
        # persist for one doc must NOT take down the whole 15k-doc run.
        # Same principle as the Stage 2 fetcher post-Storage-crash fix.
        try:
            # Pull the latest version's raw HTML from Storage.
            version = (
                supabase.table("document_versions")
                .select("raw_html_path")
                .eq("document_id", row["id"])
                .order("version_number", desc=True)
                .limit(1)
                .execute().data
            )
            if not version:
                log.warning("No version for %s", row["docid"])
                continue
            path = version[0]["raw_html_path"]
            html = supabase.storage.from_("ato-raw-html").download(path).decode("utf-8")
            parsed = parse_html(row["docid"], row["short_code"], row["doc_type"], html)
            persist(supabase, parsed,
                    case_lookup=case_lookup,
                    doc_lookup=doc_lookup,
                    act_lookup=act_lookup)
            log.info("Parsed %s: %d paragraphs, %d citations, %d leg refs, %d cases",
                     row["short_code"], len(parsed.paragraphs), len(parsed.citations),
                     len(parsed.legislation), len(parsed.cases))
            ok_count += 1
        except KeyboardInterrupt:
            raise
        except Exception as e:
            err = f"{type(e).__name__}: {e}"[:300]
            log.exception("Skipped %s: %s", row.get("docid"), err)
            fail_count += 1

    log.info("Parser run complete: %d ok, %d failed (out of %d)",
             ok_count, fail_count, len(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
