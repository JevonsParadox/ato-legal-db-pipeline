# ADR-0007: Track ATO-side visibility separately from ATO editorial status
Date: 2026-05-23
Status: Accepted

## Context
A practitioner-side requirement surfaced: the ATO sometimes quietly removes rulings from their public search even when those rulings remain legally relevant for prior tax periods (a TR withdrawn in 2026 still governs how an entity should have treated a 2022 transaction). Tax advisers still need to find these docs. Our database must:

1. **Never delete** a document, even after ATO removes it from search.
2. **Detect when** ATO has removed a doc from search.
3. **Separately detect** whether the canonical URL still works (the most common ATO pattern is to delist from search but leave the URL live).
4. **Remain searchable** for retired docs in our app, with a clear "removed by ATO on \<date\>" banner.

Requirement (1) is already met — no stage of the pipeline ever DELETEs from `documents`. The catalogue is append-only by docid (Stage 1a's `safe_upsert(..., ignore_duplicates=True)` only inserts new rows; Stages 2 and 3 only update).

Requirements (2)–(3) need new signals on the `documents` row. The design question was whether to model visibility through the existing `document_statuses` lookup (e.g. add a `removed_from_search` status) or through new orthogonal columns.

## Decision
**New columns on `documents`, separate from `status`:**
- `last_seen_in_search timestamptz`
- `removed_from_search_at timestamptz`
- `ato_url_accessible boolean default true`
- `ato_url_last_checked_at timestamptz`

**`document_statuses` stays exactly as it is** — it remains the model of *ATO's editorial declaration* about a doc (current / draft / withdrawn / archived / replaced / consolidated). These are statements the ATO itself makes about its own publication.

**Visibility is our observation** of how ATO is treating the doc through its API surface. It is orthogonal to editorial status: an ATO doc can be `status='current'` and `removed_from_search_at IS NOT NULL` at the same time (ATO has quietly delisted a still-current ruling), or `status='withdrawn'` and `removed_from_search_at IS NULL` (ATO is openly showing a withdrawn ruling in search results). Modelling these as a single column would conflate two different facts.

A new view `v_searchable_documents` (security_invoker=true, mirroring ADR-0005-equivalent) becomes the web app's primary search source. It includes retired docs and exposes two derived booleans (`is_removed_from_ato_search`, `is_ato_url_dead`) so the UI can apply banners without re-implementing the visibility predicate.

The (future) daily monitor — currently parked, see HANDOFF Pending #1 — gets the responsibility of:
- Updating `last_seen_in_search = now()` for every docid observed in each walk (done by Stage 1a itself; the monitor invokes Stage 1a)
- Marking `removed_from_search_at = now()` for any doc whose `last_seen_in_search < now() - interval '3 days'` (3-day debounce defends against transient ATO API hiccups; missing 3 consecutive daily walks is a strong signal)
- Probing the URL of newly-removed docs with a single GET to set `ato_url_accessible` and `ato_url_last_checked_at`
- Emitting `change_events` rows of new event types: `removed_from_search`, `restored_to_search`, `became_inaccessible`, `restored_accessible`

## Consequences

**Wins:**
- Clean separation: ATO's word about its docs vs our word about how ATO is treating them. Future schema changes to either side don't muddy the other.
- The web app can show practitioner-friendly banners (retired-from-search, URL-dead) without complex status mapping.
- The audit trail in `change_events` captures both editorial transitions (existing) and visibility transitions (new), letting the customer-facing app surface them differently.
- Retired docs remain in our corpus, full-text searchable, with all paragraphs/citations/legislation refs preserved. The practitioner value of "find the ruling that governed 2022 even though ATO removed it in 2026" is fully delivered.

**Cost:**
- One extra Supabase round trip per Stage 1a flush (one "touch" upsert that updates only `last_seen_in_search`). Trivial — adds ~1 second per 1000 docs walked.
- The 3-day debounce means a doc that's genuinely removed today won't appear as such until 3 days have passed. Acceptable; the legal-relevance question is "did this exist?", not "is it removed exactly this minute?".

**Future revisit conditions:**
- If we ever discover ATO removes docs with their search-API "soft-deletion" turning into an explicit error response (rather than just an omission), we can add an additional column or event type. Schema is extensible.
- If practitioners want a view of "ATO's view at point in time" we already have `document_versions` for content history; visibility transitions in `change_events` complete that picture.
