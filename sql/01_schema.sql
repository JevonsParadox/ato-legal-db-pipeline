-- ============================================================================
-- ATO Legal Database — Supabase / Postgres schema
-- ============================================================================
-- Design goals:
--   1. Lossless capture of every ATO document (rulings, determinations, PCGs,
--      PS LAs, ATO IDs, DISs, taxpayer alerts, class & product rulings, etc.).
--   2. Versioning — keep every historical snapshot so we can show "as at" views.
--   3. Citation graph — first-class relationships between documents.
--   4. Legislation graph — map every reference to ITAA 1997, ITAA 1936, GST Act,
--      TAA 1953, FBT Act, etc. so a user can ask "show me everything that
--      currently touches s8-1 ITAA 1997".
--   5. Case-law graph — link to AAT/FCA/FCAFC/HCA decisions so decisions and
--      the ATO's response (DISs) are joined.
--   6. Paragraph-level addressing — lawyers cite "TR 2024/1 para 47", we
--      should be able to return exactly that.
--   7. Full-text + semantic search ready (tsvector + pgvector).
--   8. Audit trail of every change for daily monitoring + notifications.
--
-- Run order: this file first, then 02_indexes.sql, then 03_views.sql.
-- ============================================================================

create extension if not exists "uuid-ossp";
create extension if not exists "pgcrypto";
create extension if not exists "vector";       -- pgvector for semantic search
create extension if not exists "pg_trgm";      -- fuzzy text matching for citations
create extension if not exists "unaccent";

-- ----------------------------------------------------------------------------
-- Reference data: document types and statuses
-- ----------------------------------------------------------------------------
create table document_types (
    code            text primary key,            -- "TR", "TD", "GSTR", "GSTD", "MT", "SGR", "CR", "PR", "PCG", "PS LA", "ATO ID", "DIS", "TA", "LCR", "FBTR", ...
    name            text not null,
    description     text,
    is_binding      boolean not null default false,
    is_public_ruling boolean not null default false,
    austlii_db_code text,                        -- e.g. "ATOTR", "ATOCR", "ATOPR" for the AustLII index URLs
    sort_order      int
);

-- Phase markers:
--   1 = the "spine" (build first, prove the pipeline)
--   2 = edited private advice (huge volume, build second)
--   3 = DIS (special handling needed)
--   4 = bulletins, ATO documents, etc. (lower priority)
insert into document_types (code, name, is_binding, is_public_ruling, austlii_db_code, sort_order) values
    -- Phase 1: the rulings spine
    ('TR',     'Taxation Ruling',                          true,  true,  'ATOTR',    10),
    ('TD',     'Taxation Determination',                   true,  true,  'ATOTD',    20),
    ('LCR',    'Law Companion Ruling',                     true,  true,  'ATOLCR',   30),
    ('GSTR',   'GST Ruling',                               true,  true,  'ATOGSTR',  40),
    ('GSTD',   'GST Determination',                        true,  true,  'ATOGSTD',  50),
    ('MT',     'Miscellaneous Taxation Ruling',            true,  true,  'ATOMT',    60),
    ('WTR',    'Wine Equalisation Tax Ruling',             true,  true,  'ATOWTR',   65),
    ('WTD',    'Wine Equalisation Tax Determination',      true,  true,  'ATOWTD',   66),
    ('ER',     'Excise Ruling',                            true,  true,  'ATOER',    67),
    ('ED',     'Excise Determination',                     true,  true,  'ATOED',    68),
    ('FTR',    'Fuel Tax Ruling',                          true,  true,  'ATOFTR',   69),
    ('FTD',    'Fuel Tax Determination',                   true,  true,  'ATOFTD',   70),
    ('LCTD',   'Luxury Car Tax Determination',             true,  true,  null,       71),
    ('PGBR',   'Product Grants and Benefits Ruling',       true,  true,  'ATOPGBR',  72),
    ('SGR',    'Superannuation Guarantee Ruling',          true,  true,  'ATOSGR',   80),
    ('SGD',    'Superannuation Guarantee Determination',   true,  true,  'ATOSGD',   90),
    ('SMSFR',  'Self Managed Superannuation Funds Ruling', true,  true,  'ATOSMSFR',100),
    ('SMSFD',  'Self Managed Superannuation Funds Determination', true, true, 'ATOSMSFD', 110),
    ('CR',     'Class Ruling',                             true,  true,  'ATOCR',   120),
    ('PR',     'Product Ruling',                           true,  true,  'ATOPR',   130),
    ('PCG',    'Practical Compliance Guideline',           false, false, 'ATOPCG',  140),
    ('PS LA',  'Law Administration Practice Statement',    false, false, 'ATOPSLA', 150),
    ('ATO ID', 'ATO Interpretative Decision',              false, false, 'ATOID',   160),
    ('TA',     'Taxpayer Alert',                           false, false, 'ATOTA',   170),
    ('ITR',    'Income Tax Ruling (historic)',             true,  true,  'ATOITR',  180),
    -- Phase 2: edited private advice — likely the largest single category by row count
    ('EPA',    'Edited Private Advice',                    false, false, null,      200),
    -- Phase 3: DIS — special handling required (court-file-number docids)
    ('DIS',    'Decision Impact Statement',                false, false, 'ATODIS',  210),
    -- Phase 4: bulletins and other ATO documents
    ('GSTB',   'GST Bulletin',                             false, false, null,      220),
    ('SMSFRB', 'SMSF Regulator''s Bulletin',               false, false, null,      230),
    ('SCRT',   'Super CRT Alert',                          false, false, null,      240),
    ('SGN',    'Super Guidance Note',                      false, false, null,      241),
    ('TDP',    'Technical Discussion Paper',               false, false, null,      242),
    ('FACT',   'ATO Fact Sheet',                           false, false, null,      243),
    ('OTHER',  'Other ATO Document',                       false, false, null,      244),
    -- Phase 5: compendiums (linked back to parent rulings via document_relationships)
    ('EC',     'Erratum and Compendium',                   false, false, null,      300);

-- Note: FBT is a TOPIC, not a doc_type. FBT-related rulings are TRs tagged
-- with the FBT topic in the topics taxonomy. Same goes for CGT, R&D, etc.
-- The "By topic" view of the ATO TOC drives the topics table population.

-- Note: Legislation, regulations, and court cases are NOT ingested into this
-- system. We link to the canonical authoritative sources:
--   • legislation.gov.au for Commonwealth Acts and instruments
--   • austlii.edu.au / jade.io for case-law text
-- Our value-add is the relationship graph, not duplicate hosting.

create table document_statuses (
    code            text primary key,
    name            text not null,
    is_active       boolean not null default true,  -- still legally relevant
    description     text
);

insert into document_statuses (code, name, is_active, description) values
    ('current',     'Current',     true,  'In force and binding/relevant.'),
    ('draft',       'Draft',       true,  'Released for consultation; not yet final.'),
    ('replaced',    'Replaced',    false, 'Superseded by a later document.'),
    ('withdrawn',   'Withdrawn',   false, 'Formally withdrawn by the ATO.'),
    ('archived',    'Archived',    false, 'No longer applies but retained for history.'),
    ('consolidated','Consolidated',true,  'Combined with amendments into a single text.');

-- ----------------------------------------------------------------------------
-- Acts and sections (the legislation we cite into)
-- ----------------------------------------------------------------------------
create table acts (
    id                  uuid primary key default uuid_generate_v4(),
    short_name          text unique not null,          -- "ITAA 1997"
    full_name           text not null,                 -- "Income Tax Assessment Act 1997"
    jurisdiction        text not null default 'Cth',
    legislation_url     text,                          -- legislation.gov.au canonical URL
    aliases             text[],                        -- ["ITAA97", "1997 Act"] etc., for matching
    created_at          timestamptz not null default now()
);

insert into acts (short_name, full_name, aliases, legislation_url) values
    ('ITAA 1997',  'Income Tax Assessment Act 1997',          array['ITAA97','1997 Act'],
        'https://www.legislation.gov.au/C2004A05138/latest/text'),
    ('ITAA 1936',  'Income Tax Assessment Act 1936',          array['ITAA36','1936 Act'],
        'https://www.legislation.gov.au/C1936A00027/latest/text'),
    ('GST Act',    'A New Tax System (Goods and Services Tax) Act 1999', array['GSTA','GST 1999'],
        'https://www.legislation.gov.au/C2004A00446/latest/text'),
    ('TAA 1953',   'Taxation Administration Act 1953',        array['TAA','Tax Admin Act'],
        'https://www.legislation.gov.au/C1953A00001/latest/text'),
    ('FBT Act',    'Fringe Benefits Tax Assessment Act 1986', array['FBTAA','FBT 1986'],
        'https://www.legislation.gov.au/C2004A03280/latest/text'),
    ('Super Guarantee Act', 'Superannuation Guarantee (Administration) Act 1992', array['SGAA','SG Act'],
        'https://www.legislation.gov.au/C2004A04472/latest/text');

create table act_sections (
    id                  uuid primary key default uuid_generate_v4(),
    act_id              uuid not null references acts(id) on delete cascade,
    section_number      text not null,                  -- "8-1", "974-15", "Div 152", "Subdiv 124-M"
    section_kind        text,                           -- "section" | "division" | "subdivision" | "schedule"
    title               text,
    legislation_url     text,
    -- We don't store legislation text here — defer to legislation.gov.au.
    unique (act_id, section_number)
);

-- ----------------------------------------------------------------------------
-- Topics — the ATO's own taxonomy (built from the TOC "by topic" view)
-- ----------------------------------------------------------------------------
create table topics (
    id              uuid primary key default uuid_generate_v4(),
    parent_id       uuid references topics(id) on delete cascade,
    name            text not null,
    path            text[] not null unique,             -- ["GST","Supplies","Food"] — full hierarchical path
    display_order   int,
    created_at      timestamptz not null default now()
);

-- ----------------------------------------------------------------------------
-- Documents — the core table
-- ----------------------------------------------------------------------------
create table documents (
    id                      uuid primary key default uuid_generate_v4(),

    -- Identity
    docid                   text unique not null,          -- ATO's stable identifier, e.g. "TXR/TR20241/NAT/ATO/00001"
    short_code              text not null,                  -- "TR 2024/1" — what tax practitioners actually use
    doc_type                text not null references document_types(code),
    year                    int,
    number                  text,                           -- "1", "1A", "D1"
    is_draft                boolean not null default false,
    is_addendum             boolean not null default false,
    is_withdrawal_notice    boolean not null default false,

    -- Titles & content
    title                   text not null,                  -- full title as published
    short_title             text,                           -- snappier title, if available
    body_html               text,                           -- cleaned HTML body
    body_text               text,                           -- plain text for search
    summary_ai              text,                           -- LLM-generated executive summary
    summary_human           text,                           -- ATO's own preamble/abstract if extracted

    -- Status & dates
    status                  text not null references document_statuses(code),
    date_issued             date,                           -- when first published
    date_effect_from        date,                           -- start of application period
    date_effect_to          date,                           -- end of application period (if any)
    date_withdrawn          date,
    date_last_modified      date,                           -- ATO's "last modified" stamp

    -- URLs
    ato_url                 text not null,                  -- canonical URL on ato.gov.au (the docid URL)
    austlii_url             text,                           -- mirror at AustLII for cross-reference
    pdf_storage_path        text,                           -- path in Supabase Storage if we've captured the PDF

    -- Provenance & change detection
    source_html_hash        text,                           -- sha256 of last-fetched HTML — change detection
    last_fetched_at         timestamptz,

    -- Search
    fts                     tsvector generated always as (
        setweight(to_tsvector('english', coalesce(short_code,'')), 'A') ||
        setweight(to_tsvector('english', coalesce(title,'')), 'A') ||
        setweight(to_tsvector('english', coalesce(short_title,'')), 'B') ||
        setweight(to_tsvector('english', coalesce(body_text,'')), 'C')
    ) stored,
    embedding               vector(1536),                   -- whole-document embedding for semantic search

    -- Audit
    created_at              timestamptz not null default now(),
    updated_at              timestamptz not null default now()
);

-- ----------------------------------------------------------------------------
-- Document versions — every time the content changes meaningfully
-- ----------------------------------------------------------------------------
create table document_versions (
    id                  uuid primary key default uuid_generate_v4(),
    document_id         uuid not null references documents(id) on delete cascade,
    version_number      int not null,
    fetched_at          timestamptz not null default now(),
    source_html_hash    text not null,
    raw_html_path       text,                               -- Supabase Storage path to the raw HTML snapshot
    body_text           text,
    body_html           text,
    status_at_version   text,
    change_type         text not null,                      -- "initial" | "content" | "status" | "withdrawn" | "addendum"
    diff_summary        text,                               -- human-readable summary of what changed
    diff_ai_summary     text,                               -- LLM-generated explanation of significance
    unique (document_id, version_number)
);

-- ----------------------------------------------------------------------------
-- Paragraph-level addressing
--   Tax practitioners cite "TR 2024/1, para 47". We index every paragraph so we
--   can (a) return precise citations and (b) embed each paragraph separately for
--   high-quality semantic retrieval.
-- ----------------------------------------------------------------------------
create table document_paragraphs (
    id                  uuid primary key default uuid_generate_v4(),
    document_id         uuid not null references documents(id) on delete cascade,
    paragraph_number    text not null,                      -- "1", "1A", "23(b)"
    ordinal             int not null,                       -- for ordering (1, 2, 3, ...)
    heading_path        text[],                             -- ["Background","Application"] — section hierarchy
    text                text not null,
    html                text,
    is_footnote         boolean not null default false,
    fts                 tsvector generated always as (to_tsvector('english', coalesce(text,''))) stored,
    embedding           vector(1536),
    unique (document_id, paragraph_number)
);

-- ----------------------------------------------------------------------------
-- Citation graph: document → document
-- ----------------------------------------------------------------------------
create table document_relationships (
    id                      uuid primary key default uuid_generate_v4(),
    source_document_id      uuid not null references documents(id) on delete cascade,
    target_document_id      uuid references documents(id) on delete set null, -- may be null if not yet ingested
    target_docid_raw        text not null,                  -- e.g. "TR 2018/5", "PCG 2018/9" — useful when target_document_id is null
    relationship_type       text not null,                  -- see check constraint below
    source_paragraph        text,                           -- which paragraph in the source cites the target
    context_snippet         text,                           -- surrounding text for context
    created_at              timestamptz not null default now(),

    constraint relationship_type_valid check (relationship_type in (
        'replaces',        'replaced_by',
        'amends',          'amended_by',
        'withdraws',       'withdrawn_by',
        'consolidates',    'consolidated_by',
        'cites',           'cited_by',
        'considers',       'considered_by',   -- for Decision Impact Statements
        'responds_to',     'responded_to_by', -- compendiums responding to parent rulings
        'archives',        'archived_by',     -- only if archived items get distinct docids
        'related_to'
    )),
    unique (source_document_id, target_docid_raw, relationship_type, source_paragraph)
);

-- ----------------------------------------------------------------------------
-- Authority weights — tunable component weights for the composite authority
--   score (mv_document_authority, defined in sql/03_views.sql). Config table;
--   read by the materialised view via a join so weights can be retuned without
--   code. Mirrored in the app migration 0009 — KEEP IN SYNC.
-- ----------------------------------------------------------------------------
create table authority_weights (
    component   text primary key check (component in
                  ('citation','recency','type_weighted','status','practitioner')),
    weight      numeric not null check (weight >= 0),
    description text
);

insert into authority_weights (component, weight, description) values
    ('citation',     0.40, 'Log-scaled inbound citation count, normalised within doc_type'),
    ('recency',      0.20, 'Inbound citations per year since issue, normalised within doc_type'),
    ('type_weighted',0.20, 'Inbound citations weighted by the citing document''s standing'),
    ('status',       0.10, 'Editorial status weight (current > superseded > withdrawn)'),
    ('practitioner', 0.10, 'Practitioner feedback signal — 0 in the MVP, captured for later tuning');

-- ----------------------------------------------------------------------------
-- Compendium submissions — structured EC (Erratum & Compendium) content
--   A compendium ("EC" suffix, e.g. TD 2017/20EC) is the ATO's published table
--   of responses to comments received on a draft ruling. Stage 3's normal
--   paragraph pass captures the comment table as pipe-delimited prose; this
--   table holds it STRUCTURED — one row per issue — so the app can show a
--   breakdown (accepted/rejected/partial) and a per-issue accept/reject label.
--   Populated by parsers/03_parse_document.py --compendiums (re-parses the
--   source HTML <table>). Parent link resolved from short_code (strip "EC").
--   Corpus table — read via service-role like the rest of the corpus, no RLS.
-- ----------------------------------------------------------------------------
create table compendium_submissions (
    id                      uuid primary key default uuid_generate_v4(),
    compendium_document_id  uuid not null references documents(id) on delete cascade, -- the EC doc
    parent_document_id      uuid references documents(id) on delete set null,         -- the ruling it responds to (null if unresolved)
    ordinal                 int not null,                   -- row order within the compendium
    issue_no                text,                           -- "1", "1." — as published (may restart/skip)
    entities                text,                           -- "Entity/ies commenting" col (only the older 4-col format)
    issue_raised            text,                           -- the comment/submission text
    ato_response            text,                           -- the ATO's response/action taken (verbatim)
    outcome                 text not null default 'unknown',-- accepted | rejected | partial | noted | unknown
    created_at              timestamptz not null default now(),
    constraint compendium_outcome_valid check (outcome in
        ('accepted','rejected','partial','noted','unknown')),
    unique (compendium_document_id, ordinal)
);

-- ----------------------------------------------------------------------------
-- Legislation references — every section of an Act cited by a document
-- ----------------------------------------------------------------------------
create table legislation_references (
    id                  uuid primary key default uuid_generate_v4(),
    document_id         uuid not null references documents(id) on delete cascade,
    act_id              uuid references acts(id),
    act_short_name      text not null,                      -- "ITAA 1997" — preserved even if act_id is null
    section_id          uuid references act_sections(id),
    section_raw         text,                               -- "8-1", "subsection 8-1(2)(a)"
    subsection          text,
    paragraph_in_act    text,
    source_paragraph    text,                               -- which paragraph in our document cites this
    context_snippet     text,
    created_at          timestamptz not null default now()
);

-- ----------------------------------------------------------------------------
-- Case-law references — court & tribunal cases cited
-- ----------------------------------------------------------------------------
create table case_references (
    id                  uuid primary key default uuid_generate_v4(),
    document_id         uuid not null references documents(id) on delete cascade,
    case_name           text not null,                      -- "FCT v Pike"
    citation            text not null,                      -- "[2020] FCAFC 158"
    court              text,                               -- "FCAFC", "HCA", "FCA", "AAT", "ART"
    decision_year       int,
    source_paragraph    text,
    context_snippet     text,
    austlii_url         text,                               -- canonical link on AustLII if resolvable
    jade_url            text,
    -- A document may also have a DIS responding to a decision — link via document_relationships(considers/considered_by).
    created_at          timestamptz not null default now()
);

-- ----------------------------------------------------------------------------
-- Topic mapping — many-to-many
-- ----------------------------------------------------------------------------
create table document_topics (
    document_id     uuid not null references documents(id) on delete cascade,
    topic_id        uuid not null references topics(id) on delete cascade,
    is_primary      boolean not null default false,
    primary key (document_id, topic_id)
);

-- ----------------------------------------------------------------------------
-- Change events — audit log feeding the daily monitor & customer alerts
-- ----------------------------------------------------------------------------
create table change_events (
    id              uuid primary key default uuid_generate_v4(),
    document_id     uuid references documents(id) on delete set null,
    docid           text,                                   -- preserved even if document is deleted
    detected_at     timestamptz not null default now(),
    event_type      text not null,                          -- "new" | "modified" | "withdrawn" | "status_change" | "addendum" | "replaced"
    details         jsonb,
    ai_summary      text,
    notified_at     timestamptz
);

-- ----------------------------------------------------------------------------
-- Crawl runs — when did we run, what did we find, what failed
-- ----------------------------------------------------------------------------
create table crawl_runs (
    id                      uuid primary key default uuid_generate_v4(),
    started_at              timestamptz not null default now(),
    finished_at             timestamptz,
    source                  text not null,                  -- "ato" | "austlii"
    mode                    text not null,                  -- "enumerate" | "backfill" | "monitor"
    documents_discovered    int not null default 0,
    documents_new           int not null default 0,
    documents_changed       int not null default 0,
    documents_failed        int not null default 0,
    notes                   text,
    failure_log             jsonb
);

-- ----------------------------------------------------------------------------
-- Fetch queue — work distribution for the scraper
-- ----------------------------------------------------------------------------
create table fetch_queue (
    id              uuid primary key default uuid_generate_v4(),
    docid           text not null,
    ato_url         text not null,
    austlii_url     text,
    short_code      text,
    priority        int not null default 100,               -- lower = more urgent
    status          text not null default 'pending',        -- pending | in_progress | done | failed | skipped
    attempt_count   int not null default 0,
    last_attempt_at timestamptz,
    last_error      text,
    scheduled_for   timestamptz not null default now(),
    unique (docid)
);

-- ----------------------------------------------------------------------------
-- Update triggers
-- ----------------------------------------------------------------------------
create or replace function set_updated_at() returns trigger as $$
begin
    new.updated_at := now();
    return new;
end;
$$ language plpgsql;

create trigger trg_documents_updated_at
    before update on documents
    for each row execute procedure set_updated_at();
