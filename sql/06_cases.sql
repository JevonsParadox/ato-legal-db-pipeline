-- ============================================================================
-- ATO Legal Database — cases master table (judgments + tribunal decisions)
-- ============================================================================
-- Apply order: this file sixth, after 01_schema, 02_indexes, 03_views,
-- 04_rls_policies, 05_view_security_invoker.
--
-- Why this exists
-- ---------------
-- Stage 1a's enumeration walks the full ATO Type tree. The "Cases" subtree
-- (Cases > <Court> > <Year> > <Case name>) contains ~18,800 leaves the
-- pipeline was originally filtering out (HANDOFF §3.9 "no case-law text
-- ingestion"). That decision is right for case TEXT — AustLII does that
-- well, the commercial-use terms are complex, and we'd be a redundant
-- mirror. It's wrong for case METADATA: the ATO has already curated and
-- classified which cases are tax-relevant; we're throwing that data away.
--
-- This migration adds a `cases` master table to capture each unique
-- court / tribunal decision the ATO indexes. We store name, court, year,
-- ATO's URL for the case, and a slug for stable lookups. The actual
-- judgment text is NOT ingested — practitioners deep-link out to AustLII
-- for that.
--
-- Cross-links existing `case_references` (per-doc citation occurrences)
-- to this master table via a nullable `case_id` foreign key, enabling
-- "every ATO doc citing FCT v Pike" as a one-hop lookup.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- cases: one row per unique court/tribunal decision indexed by the ATO
-- ----------------------------------------------------------------------------
create table if not exists cases (
    id              uuid primary key default uuid_generate_v4(),
    -- Stable slug used as the dedup key by the enumerator. Format:
    -- "<court>-<year>-<normalised-case-name>". Lowercase, alphanumerics +
    -- hyphens only.
    slug            text not null unique,
    -- Display name as the ATO has it. May contain " - (date)" suffix.
    case_name       text not null,
    -- Normalised short code: AAT, FCA, FCAFC, HCA, ART, FamC, etc.
    -- Unmapped courts fall through verbatim.
    court           text not null,
    decision_year   int,
    decision_date   date,
    -- Citation populated later by parser cross-reference when the ATO
    -- doesn't carry the bracket-format citation in the tree itself.
    citation        text,
    -- ATO's own URL for the case (data.url from the Type-tree leaf).
    ato_url         text,
    -- Full breadcrumb path for traceability and topic-style filtering.
    ato_path        text[],
    -- Set by parser when an AustLII deep link can be resolved from a
    -- citation match.
    austlii_url     text,
    first_seen_at   timestamptz not null default now(),
    last_seen_at    timestamptz not null default now()
);

-- ----------------------------------------------------------------------------
-- RLS — match the existing content-table pattern (anon read during pilot)
-- ----------------------------------------------------------------------------
alter table cases enable row level security;

drop policy if exists "anon read" on cases;
create policy "anon read" on cases
    for select to anon, authenticated using (true);

-- ----------------------------------------------------------------------------
-- Cross-link: case_references → cases
-- ----------------------------------------------------------------------------
-- Nullable: some citations in ATO doc text won't match a Cases-tree leaf
-- (foreign jurisdiction, pre-classification, unusual formatting). The FK
-- is set later by parsers/03_parse_document.py via name+year matching.
do $$
begin
    if not exists (
        select 1 from information_schema.columns
        where table_name = 'case_references' and column_name = 'case_id'
    ) then
        alter table case_references add column case_id uuid references cases(id);
    end if;
end $$;

create index if not exists case_references_case_id_idx on case_references(case_id);

-- Helpful index on (court, year) since that's how the new view filters.
create index if not exists cases_court_year_idx on cases(court, decision_year);
-- Full-text on case_name for fuzzy lookup in the future web app.
create index if not exists cases_case_name_trgm_idx on cases using gin (case_name gin_trgm_ops);

-- ----------------------------------------------------------------------------
-- View: "every ATO doc that cites case X" — the practitioner's killer query.
-- ----------------------------------------------------------------------------
create or replace view v_documents_by_case with (security_invoker = true) as
select
    c.id            as case_id,
    c.slug,
    c.case_name,
    c.court,
    c.decision_year,
    c.ato_url,
    c.austlii_url,
    d.id            as document_id,
    d.short_code,
    d.title,
    d.doc_type,
    d.status,
    d.date_issued,
    cr.source_paragraph,
    cr.context_snippet
from cases c
join case_references cr on cr.case_id = c.id
join documents d on d.id = cr.document_id
order by
    case when d.status = 'current' then 0 else 1 end,
    d.date_issued desc nulls last;
