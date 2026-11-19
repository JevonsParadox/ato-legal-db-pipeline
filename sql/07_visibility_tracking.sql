-- ============================================================================
-- ATO Legal Database — ATO-side document visibility tracking
-- ============================================================================
-- Apply order: this file seventh, after 01_schema, 02_indexes, 03_views,
-- 04_rls_policies, 05_view_security_invoker, 06_cases.
--
-- Why this exists
-- ---------------
-- Tax-adviser feedback: the ATO sometimes quietly removes rulings from
-- their public search even when those rulings remain legally relevant for
-- prior periods. The adviser still needs to find them. Our database must:
--   (a) never delete a document, even after ATO removes it from search;
--   (b) detect WHEN ATO has removed a doc from search;
--   (c) detect separately whether the canonical URL still works;
--   (d) remain searchable in our app for retired docs, with a banner.
--
-- (a) is already true — no stage of the pipeline ever DELETEs from
--     `documents`. The catalogue is append-only by docid.
--
-- (b)-(d) need the schema additions below. We store these as separate
-- columns rather than new entries in `document_statuses` because ATO's
-- editorial status (current / withdrawn / archived) and our observed
-- visibility (in search / removed from search / inaccessible) are
-- orthogonal concepts. Codified in ADR-0007.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- Columns on `documents`
-- ----------------------------------------------------------------------------
alter table documents
    add column if not exists last_seen_in_search    timestamptz,
    add column if not exists removed_from_search_at timestamptz,
    add column if not exists ato_url_accessible     boolean not null default true,
    add column if not exists ato_url_last_checked_at timestamptz;

-- One-time backfill: every existing doc was seen in our most recent Stage 1a
-- walk (else it wouldn't be in the catalogue). Treat `updated_at` as the
-- best available proxy for "last time we observed this doc in the ATO API".
update documents
   set last_seen_in_search = updated_at
 where last_seen_in_search is null;

-- ----------------------------------------------------------------------------
-- Indexes
-- ----------------------------------------------------------------------------
-- Used by the monitor's "which docs haven't been seen lately?" query.
create index if not exists documents_last_seen_in_search_idx
    on documents (last_seen_in_search);

-- Filtered index for the web app's "show retired docs differently" filter.
-- Partial index keeps it small — only the (small) set of removed docs.
create index if not exists documents_removed_from_search_idx
    on documents (removed_from_search_at)
    where removed_from_search_at is not null;

-- ----------------------------------------------------------------------------
-- View: v_searchable_documents
-- ----------------------------------------------------------------------------
-- The web app's primary search source. Key difference from
-- v_current_documents: this view INCLUDES retired docs. The UI applies a
-- banner conditional on `is_removed_from_ato_search`.
create or replace view v_searchable_documents with (security_invoker = true) as
select d.*,
       (d.removed_from_search_at is not null) as is_removed_from_ato_search,
       (not d.ato_url_accessible)              as is_ato_url_dead
from documents d;
