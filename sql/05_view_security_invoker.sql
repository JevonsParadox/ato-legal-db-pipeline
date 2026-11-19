-- ============================================================================
-- ATO Legal Database — switch all views to security_invoker mode
-- ============================================================================
-- Apply order: this file fifth, after 01_schema, 02_indexes, 03_views,
-- 04_rls_policies.
--
-- Why this exists
-- ---------------
-- Postgres views default to `security_definer` semantics: RLS policies are
-- evaluated as the view's creator (postgres superuser, in our case), which
-- silently bypasses RLS for any caller. Supabase's linter flags this
-- correctly as "Security Definer View" critical alerts.
--
-- With our current `using (true)` anon-read policies (per ADR-0005 the
-- pilot-stage decision) the practical effect is identical to invoker mode.
-- But the moment we tighten RLS — Clerk auth at W5, paid-tier user scoping,
-- per-user saved searches, anything — definer views would let anon read
-- through what the table would deny.
--
-- ALTER VIEW ... SET (security_invoker = on) is a metadata-only change.
-- No row recreation, no data touched. Idempotent — safe to re-run.
-- ============================================================================

alter view v_current_documents          set (security_invoker = on);
alter view v_documents_by_section       set (security_invoker = on);
alter view v_inbound_citations          set (security_invoker = on);
alter view v_decision_threads           set (security_invoker = on);
alter view v_latest_by_type_and_topic   set (security_invoker = on);
