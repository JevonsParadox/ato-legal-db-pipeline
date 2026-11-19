-- ============================================================================
-- ATO Legal Database — RLS read policies for the practitioner pilot
-- ============================================================================
-- Apply order: this file fourth, after 01_schema, 02_indexes, 03_views.
--
-- Adds permissive SELECT policies (`using (true)`) for content + lookup
-- tables, so the W1–W4 web app can read via the anon key. Pipeline-internal
-- tables (`fetch_queue`, `change_events`, `crawl_runs`) intentionally get
-- NO policies — they stay locked to service-role only.
--
-- Revisit at W5 when Clerk auth lands: tighten to user-scoped policies for
-- any per-user state (subscriptions, saved searches, etc.).
--
-- Idempotent: drops the policy first if it exists, then recreates. Safe to
-- re-run.
-- ============================================================================

-- Enable RLS on every public-schema table. `enable row level security` is
-- idempotent — re-running on an already-enabled table is a no-op. This
-- block exists so a fresh install via psql / Supabase CLI works without
-- relying on the dashboard's "Enable RLS" warning prompt.
alter table document_types         enable row level security;
alter table document_statuses      enable row level security;
alter table topics                 enable row level security;
alter table documents              enable row level security;
alter table document_versions      enable row level security;
alter table document_paragraphs    enable row level security;
alter table document_relationships enable row level security;
alter table legislation_references enable row level security;
alter table case_references        enable row level security;
alter table acts                   enable row level security;
alter table act_sections           enable row level security;
alter table document_topics        enable row level security;
alter table fetch_queue            enable row level security;
alter table change_events          enable row level security;
alter table crawl_runs             enable row level security;

-- Lookup tables
drop policy if exists "anon read" on document_types;
create policy "anon read" on document_types
    for select to anon, authenticated using (true);

drop policy if exists "anon read" on document_statuses;
create policy "anon read" on document_statuses
    for select to anon, authenticated using (true);

drop policy if exists "anon read" on acts;
create policy "anon read" on acts
    for select to anon, authenticated using (true);

drop policy if exists "anon read" on act_sections;
create policy "anon read" on act_sections
    for select to anon, authenticated using (true);

-- Taxonomy
drop policy if exists "anon read" on topics;
create policy "anon read" on topics
    for select to anon, authenticated using (true);

drop policy if exists "anon read" on document_topics;
create policy "anon read" on document_topics
    for select to anon, authenticated using (true);

-- Content
drop policy if exists "anon read" on documents;
create policy "anon read" on documents
    for select to anon, authenticated using (true);

drop policy if exists "anon read" on document_versions;
create policy "anon read" on document_versions
    for select to anon, authenticated using (true);

drop policy if exists "anon read" on document_paragraphs;
create policy "anon read" on document_paragraphs
    for select to anon, authenticated using (true);

-- Graphs
drop policy if exists "anon read" on document_relationships;
create policy "anon read" on document_relationships
    for select to anon, authenticated using (true);

drop policy if exists "anon read" on legislation_references;
create policy "anon read" on legislation_references
    for select to anon, authenticated using (true);

drop policy if exists "anon read" on case_references;
create policy "anon read" on case_references
    for select to anon, authenticated using (true);

-- ============================================================================
-- Intentionally NOT given any policy:
--   fetch_queue, change_events, crawl_runs
-- These have RLS enabled but zero policies, so anon/authenticated reads
-- return empty arrays. Pipeline (service-role) bypasses RLS and works.
-- ============================================================================
