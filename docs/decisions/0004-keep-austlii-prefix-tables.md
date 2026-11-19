# ADR-0004: Retain `01_enumerate_austlii.py` for its docid prefix tables
Date: 2026-05-19
Status: Accepted

## Context
`scrapers/01_enumerate_austlii.py` is the original Stage 1 implementation, superseded by `01_enumerate_ato.py` once we discovered the ATO API (see ADR-0001). It's tempting to delete superseded code, but the AustLII enumerator contains canonical lookup tables — `DOC_TYPE_PREFIX`, `CATEGORY_PREFIX_FINAL`, `CATEGORY_PREFIX_DRAFT`, `REF_CODE`, plus `build_docid()` and `build_dis_docid()` — that the parser uses to **reverse-resolve docids found inside document body text** (e.g., a TR citing "TR 2010/5" needs to become the canonical docid `TXR/TR20105/NAT/ATO/00001` for the citation graph).

## Decision
Keep `scrapers/01_enumerate_austlii.py` in the tree as the canonical home for these prefix tables. `parsers/03_parse_document.py` imports the mapping dictionaries from it. The enumeration entry point at the bottom of the file remains runnable as a fallback if the ATO API ever changes shape.

## Consequences
**Wins:** zero risk of losing the prefix-mapping intelligence; second-source fallback if the ATO API breaks. **Cost:** a file labelled "legacy" sits permanently in `scrapers/`. Anyone tempted to delete it must first migrate the prefix tables to a neutral location (`scrapers/_docid_prefixes.py`) and update the parser import. Until that migration is done deliberately, **do not delete this file**. Codified in HANDOFF.md §8.1.
