"""
validate_prefixes.py
====================
Before running a multi-hour backfill, validate that every (doc_type → docid)
mapping we have actually resolves to a real ATO document.

For each known-good (doc_type, year, number) tuple, we:
    1. Construct the docid using build_docid()
    2. Fetch the URL via Playwright (Akamai blocks plain httpx)
    3. Check that we got HTTP 200 with actual content (not the 'Not found' page)
    4. Report PASS / FAIL with the URL so we can spot-check failures by hand

Run:
    pip install playwright
    python -m playwright install chromium
    python validate_prefixes.py

Add or remove fixtures by editing FIXTURES below. Aim for at least one known
URL per doc_type. Drafts are checked separately.
"""
from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass

from playwright.async_api import async_playwright

# Import from the sibling enumerator module — adjust path as needed when
# running this from a different directory.
sys.path.insert(0, "..")
from scrapers._01_enumerate_austlii import build_docid, ATO_DOC_URL_TPL  # type: ignore


@dataclass
class Fixture:
    doc_type: str
    year: int
    number: str
    label: str           # human-readable description for output
    is_draft: bool = False
    suffix: str = ""     # for withdrawals etc.


# Curated set of known-good URLs. Each one was either confirmed by DevTools
# inspection or appears in published ATO indices.
FIXTURES: list[Fixture] = [
    # Phase 1 — final/current documents
    Fixture("TR",    2024, "1",  "Taxation Ruling 2024/1"),
    Fixture("TD",    2023, "2",  "Taxation Determination 2023/2"),
    Fixture("GSTR",  2024, "1",  "GST Ruling 2024/1"),
    Fixture("GSTD",  2024, "1",  "GST Determination 2024/1"),
    Fixture("MT",    2008, "3",  "Miscellaneous Taxation Ruling 2008/3"),
    Fixture("LCR",   2023, "1",  "Law Companion Ruling 2023/1"),
    Fixture("PCG",   2018, "9",  "Practical Compliance Guideline 2018/9"),
    Fixture("PS LA", 2025, "2",  "Practice Statement 2025/2"),
    Fixture("CR",    2024, "70", "Class Ruling 2024/70"),
    Fixture("TA",    2026, "1",  "Taxpayer Alert 2026/1"),
    Fixture("SGD",   2017, "1",  "Super Guarantee Determination 2017/1"),
    Fixture("SMSFD", 2013, "1",  "SMSF Determination 2013/1"),
    Fixture("ATO ID", 2010, "4", "ATO Interpretative Decision 2010/4"),
    # Withdrawals
    Fixture("TR",    2022, "1",  "TR 2022/1W (withdrawn)", suffix="W"),
    # Drafts
    Fixture("TR",    2025, "1",  "Draft TR 2025/D1", is_draft=True),
    Fixture("TD",    2025, "3",  "Draft TD 2025/D3", is_draft=True),
    Fixture("LCR",   2026, "1",  "Draft LCR 2026/D1", is_draft=True),
    # TODO: add fixtures for these once you find one example each:
    #   WTR, WTD, ER, ED, FTR, FTD, LCTD, PGBR, SGR, SMSFR, PR
]


async def check_one(page, fixture: Fixture) -> tuple[bool, str, str]:
    docid = build_docid(
        fixture.doc_type, fixture.year, fixture.number,
        is_draft=fixture.is_draft, suffix=fixture.suffix,
    )
    url = ATO_DOC_URL_TPL.format(docid=docid)
    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        status = resp.status if resp else 0
        # The ATO returns 200 even for not-found pages with a "document not found"
        # body. Detect that by looking for the title/heading.
        title = (await page.title()).strip()
        body_text = await page.locator("body").inner_text()
        looks_real = (
            status == 200
            and "not found" not in title.lower()
            and "not found" not in body_text[:500].lower()
            and len(body_text) > 500
        )
        return looks_real, url, f"HTTP {status}, title='{title[:60]}'"
    except Exception as e:
        return False, url, f"EXCEPTION: {e}"


async def main() -> int:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 "
                "(KHTML, like Gecko) Version/17.4 Safari/605.1.15"
            ),
            locale="en-AU",
        )
        page = await ctx.new_page()

        passes = 0
        fails: list[tuple[Fixture, str, str]] = []
        for f in FIXTURES:
            ok, url, note = await check_one(page, f)
            marker = "✓ PASS" if ok else "✗ FAIL"
            print(f"{marker}  {f.label:<45}  {note}")
            if ok:
                passes += 1
            else:
                fails.append((f, url, note))

        await browser.close()

        print()
        print(f"Result: {passes}/{len(FIXTURES)} passed")
        if fails:
            print("\nFailures (open these in a browser to debug):")
            for f, url, note in fails:
                print(f"  {f.label}")
                print(f"    {url}")
                print(f"    {note}")
            return 1
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
