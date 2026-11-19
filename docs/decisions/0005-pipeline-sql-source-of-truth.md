# ADR-0005: Pipeline `sql/` is the source of truth for schema; web app mirrors
Date: 2026-05-19
Status: Accepted

## Context
Two repos share one Supabase project: this pipeline (writes via service-role; defines tables, indexes, views) and the `ato-tax-app` web repo (reads via anon/JWT; needs schema for Supabase CLI preview branches and local dev). Schema must not drift between the two — but co-locating it in a third "shared" repo adds CI complexity disproportionate to the size of the schema (~600 lines of SQL).

## Decision
The canonical schema lives in **this repo** at `sql/01_schema.sql`, `sql/02_indexes.sql`, `sql/03_views.sql`. The web app keeps a downstream mirror at `ato-tax-app/supabase/migrations/00001_init.sql`, `00002_indexes.sql`, `00003_views.sql`. Any schema change is made here first, applied to the Supabase project via the Dashboard SQL Editor, then copied verbatim to the web repo as a new migration file. The web repo's `supabase/migrations/` directory MUST NOT contain DDL that doesn't exist in this repo's `sql/`.

## Consequences
**Wins:** one source of truth, no inter-repo CI dependency, schema review happens in the pipeline repo where the data semantics live. **Cost:** manual two-step copy on every schema change. If drift becomes an issue (forgetting to mirror, accidental web-repo-only edits), revisit with one of: a `make sync-schema` script, a GitHub Actions cross-repo PR bot, or moving schema to a shared submodule. Don't pre-build that infrastructure — wait for the pain. Mirrored in `ato-tax-app/docs/decisions/0005-pipeline-sql-source-of-truth.md` so the web side knows the contract.
