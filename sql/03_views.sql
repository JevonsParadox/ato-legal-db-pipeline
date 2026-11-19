-- ============================================================================
-- Views and helper functions — the "magic" queries for tax practitioners
-- ============================================================================
-- These are the queries that will make this product feel like magic. They
-- pre-compute the joins that practitioners want answered immediately:
--   • What's the ATO's current view on section X?
--   • Trace the lineage of a ruling (replaces / replaced_by chain).
--   • Show every document that touches a specific section AND its status today.
--   • Show me the DIS chain for a court decision.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- v_current_documents: only documents that are currently in force
-- ----------------------------------------------------------------------------
create or replace view v_current_documents with (security_invoker = true) as
select d.*
from documents d
join document_statuses s on s.code = d.status
where s.is_active = true;

-- ----------------------------------------------------------------------------
-- v_documents_by_section
--   For each (act, section) pair, list every document that cites it,
--   ordered so the most recent CURRENT documents come first. This is the
--   single most useful query a tax lawyer can run.
-- ----------------------------------------------------------------------------
create or replace view v_documents_by_section with (security_invoker = true) as
select
    lr.act_short_name,
    lr.section_raw,
    d.id              as document_id,
    d.short_code,
    d.title,
    d.doc_type,
    d.status,
    d.date_issued,
    d.date_last_modified,
    d.ato_url,
    lr.source_paragraph,
    lr.context_snippet
from legislation_references lr
join documents d on d.id = lr.document_id
order by
    case when d.status = 'current' then 0 else 1 end,
    d.date_issued desc nulls last;

-- ----------------------------------------------------------------------------
-- v_document_lineage
--   For each document, walk the chain of what replaced it / what it replaces.
--   Returns the FULL ancestry as an array of short_codes.
-- ----------------------------------------------------------------------------
-- v2 (2026-05-27): normalise ALL supersession-shaped edges (not just
-- 'replaces') into one directed "newer supersedes older" graph and return the
-- relationship_type + date per entry, so the app's lineage tab can label each
-- step (replaced / amended / withdrawn). v1 only followed 'replaces' and so
-- returned the self row alone for most docs. KEEP IN SYNC with the app mirror
-- ato-app/supabase/migrations/0007_fn_document_lineage_v2.sql (applied there
-- via the app's supabase-link, which is the DDL-application path in use).
drop function if exists fn_document_lineage(uuid);
create or replace function fn_document_lineage(p_document_id uuid)
returns table (
    generation int,
    document_id uuid,
    short_code text,
    title text,
    status text,
    date_issued date,
    direction text,            -- 'self' | 'predecessor' | 'successor'
    relationship_type text     -- the edge linking this node toward the queried doc
) language sql stable as $$
    with recursive
    edges as (
        select source_document_id as newer, target_document_id as older, relationship_type as rel
        from document_relationships
        where relationship_type in ('replaces','supersedes','amends','withdraws')
        union all
        select target_document_id as newer, source_document_id as older, relationship_type as rel
        from document_relationships
        where relationship_type in ('replaced_by','superseded_by','amended_by','withdrawn_by')
    ),
    ancestors as (
        select 0 as gen, p_document_id as id, null::text as rel
        union all
        select a.gen - 1, e.older, e.rel
        from ancestors a join edges e on e.newer = a.id
        where a.gen > -8
    ),
    descendants as (
        select 0 as gen, p_document_id as id, null::text as rel
        union all
        select d.gen + 1, e.newer, e.rel
        from descendants d join edges e on e.older = d.id
        where d.gen < 8
    ),
    combined as (
        select gen, id, rel from ancestors where gen < 0
        union all
        select 0, p_document_id, null
        union all
        select gen, id, rel from descendants where gen > 0
    ),
    ranked as (
        select distinct on (id) id, gen, rel from combined order by id, abs(gen)
    )
    select r.gen, r.id, d.short_code, d.title, d.status, d.date_issued,
           case when r.gen = 0 then 'self' when r.gen < 0 then 'predecessor' else 'successor' end,
           r.rel
    from ranked r join documents d on d.id = r.id
    order by r.gen;
$$;

-- ----------------------------------------------------------------------------
-- v_inbound_citations
--   For each document, every other document that cites it. Inverts the
--   citation graph for the "what relies on this ruling?" question.
-- ----------------------------------------------------------------------------
create or replace view v_inbound_citations with (security_invoker = true) as
select
    r.target_document_id      as document_id,
    r.target_docid_raw,
    s.id                      as citing_document_id,
    s.short_code              as citing_short_code,
    s.title                   as citing_title,
    s.status                  as citing_status,
    r.relationship_type,
    r.source_paragraph,
    r.context_snippet
from document_relationships r
join documents s on s.id = r.source_document_id
where r.target_document_id is not null;

-- ----------------------------------------------------------------------------
-- v_decision_threads
--   Joins a court/tribunal case with: the DIS that responds to it, AND any
--   rulings the DIS or related docs reference. Lets practitioners follow
--   the full thread from case to current ATO position.
-- ----------------------------------------------------------------------------
create or replace view v_decision_threads with (security_invoker = true) as
select
    cr.citation,
    cr.case_name,
    cr.court,
    cr.decision_year,
    cr.austlii_url,
    dis.id            as dis_document_id,
    dis.short_code    as dis_short_code,
    dis.title         as dis_title,
    dis.status        as dis_status,
    dis.date_issued   as dis_issued
from case_references cr
left join documents dis
    on dis.doc_type = 'DIS'
   and dis.id = cr.document_id;

-- ----------------------------------------------------------------------------
-- Quick helper: most recent current document of each type per topic
-- ----------------------------------------------------------------------------
create or replace view v_latest_by_type_and_topic with (security_invoker = true) as
select distinct on (t.path, d.doc_type)
    t.path                as topic_path,
    d.doc_type,
    d.short_code,
    d.title,
    d.date_issued,
    d.ato_url
from documents d
join document_topics dt on dt.document_id = d.id
join topics t on t.id = dt.topic_id
where d.status = 'current'
order by t.path, d.doc_type, d.date_issued desc;

-- ----------------------------------------------------------------------------
-- mv_document_authority — composite authority score (Week 6, app feature)
--   Multi-component authority per doc: normalised citation / recency /
--   type-weighted-citation / status components + a weighted composite + a 1–5
--   strength bucket (percentile WITHIN doc_type). Component weights live in
--   authority_weights (tunable without code). Refreshed nightly by the monitor
--   via fn_refresh_authority(). See the formula notes in the app mirror
--   ato-app/supabase/migrations/0009_authority_score.sql — KEEP IN SYNC
--   (applied to the live DB via the app's supabase-link, the DDL path in use).
-- ----------------------------------------------------------------------------
drop materialized view if exists mv_document_authority;
create materialized view mv_document_authority as
with
weights as (
  select
    coalesce(max(weight) filter (where component = 'citation'), 0)      as w_citation,
    coalesce(max(weight) filter (where component = 'recency'), 0)       as w_recency,
    coalesce(max(weight) filter (where component = 'type_weighted'), 0) as w_type,
    coalesce(max(weight) filter (where component = 'status'), 0)        as w_status,
    coalesce(max(weight) filter (where component = 'practitioner'), 0)  as w_pract
  from authority_weights
),
inbound as (
  select dr.target_document_id as doc_id,
         count(*) as cc,
         sum(case when dt.is_binding then 1.0
                  when dt.is_public_ruling then 0.8
                  else 0.4 end)::numeric as twcc
  from document_relationships dr
  join documents src on src.id = dr.source_document_id
  join document_types dt on dt.code = src.doc_type
  where dr.target_document_id is not null
  group by dr.target_document_id
),
base as (
  select d.id, d.doc_type, d.status, d.date_issued,
         coalesce(i.cc, 0) as cc,
         coalesce(i.twcc, 0) as twcc,
         coalesce(i.cc, 0)::numeric
           / greatest(1, date_part('year', age(now(), coalesce(d.date_issued, now()))) + 1)
           as cite_rate,
         (case d.status
            when 'current'      then 1.0 when 'consolidated' then 0.9
            when 'draft'        then 0.6 when 'replaced'     then 0.5
            when 'archived'     then 0.4 when 'withdrawn'    then 0.2
            else 0.5 end)::numeric as status_weight
  from documents d
  left join inbound i on i.doc_id = d.id
),
norm as (
  select b.*,
    case when max(ln(1 + b.cc)) over w = 0 then 0
         else ln(1 + b.cc) / max(ln(1 + b.cc)) over w end as cite_norm,
    case when max(ln(1 + b.twcc)) over w = 0 then 0
         else ln(1 + b.twcc) / max(ln(1 + b.twcc)) over w end as type_weighted_norm,
    case when max(b.cite_rate) over w = 0 then 0
         else b.cite_rate / max(b.cite_rate) over w end as recency_norm
  from base b
  window w as (partition by b.doc_type)
)
select
  n.id   as document_id,
  n.doc_type,
  n.cc   as citation_count,
  round(n.cite_norm::numeric, 4)          as citation_norm,
  round(n.recency_norm::numeric, 4)       as recency_norm,
  round(n.type_weighted_norm::numeric, 4) as type_weighted_norm,
  round(n.status_weight::numeric, 4)      as status_weight,
  0::numeric                              as practitioner_norm,
  round((w.w_citation * n.cite_norm + w.w_recency * n.recency_norm
        + w.w_type * n.type_weighted_norm + w.w_status * n.status_weight)::numeric, 4) as composite,
  ntile(5) over (
    partition by n.doc_type
    order by (w.w_citation * n.cite_norm + w.w_recency * n.recency_norm
              + w.w_type * n.type_weighted_norm + w.w_status * n.status_weight)
  ) as strength
from norm n cross join weights w;

create unique index if not exists idx_mv_authority_doc
  on mv_document_authority (document_id);

-- Nightly refresh entry point (monitor calls this as service role).
create or replace function fn_refresh_authority() returns void
  language plpgsql security definer set search_path = public as $$
begin
  refresh materialized view concurrently mv_document_authority;
end $$;

grant select on authority_weights, mv_document_authority to anon, authenticated, service_role;
grant execute on function fn_refresh_authority() to service_role;
