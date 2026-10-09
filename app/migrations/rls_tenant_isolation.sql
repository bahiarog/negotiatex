-- Teil C hardening: restricted app role + Row-Level Security tenant isolation.
-- Idempotent: safe to re-run. Replace __APP_DB_PASSWORD__ before running.
--
-- Why: the app's own code already filters every query by tenant_id
-- (see app/deps.py), but that is only ever as correct as every single
-- endpoint's own WHERE clause. This migration adds a second, independent
-- enforcement layer at the database itself, so a future application bug
-- (a forgotten tenant filter somewhere) cannot leak data across tenants.
--
-- Requires the app's runtime DATABASE_URL to connect as `negotiatex_app`,
-- NOT as the `negotiatex` superuser -- Postgres superusers and BYPASSRLS
-- roles silently ignore every RLS policy, which would make this migration
-- a no-op if left unconnected. See docker-compose.yml's `DATABASE_URL` and
-- .env's `APP_DB_PASSWORD`.

DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'negotiatex_app') THEN
    CREATE ROLE negotiatex_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE PASSWORD '__APP_DB_PASSWORD__';
  ELSE
    ALTER ROLE negotiatex_app WITH NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE PASSWORD '__APP_DB_PASSWORD__';
  END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO negotiatex_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO negotiatex_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO negotiatex_app;
ALTER DEFAULT PRIVILEGES FOR ROLE negotiatex IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO negotiatex_app;
ALTER DEFAULT PRIVILEGES FOR ROLE negotiatex IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO negotiatex_app;

-- Direct tenant_id tables: scoped by app.tenant_id (set in deps.py's
-- get_current_membership, SESSION-scoped via set_config(..., false) since
-- several routers call db.commit() mid-request, which would end a
-- transaction-scoped/LOCAL setting early).
DO $$
DECLARE
  t text;
  tables text[] := ARRAY[
    'case_events','cases','contract_actions','contract_events','contract_templates',
    'contracts','documents','line_items','nda_events','ndas',
    'negotiation_actions','negotiation_email_messages','negotiation_exceptions',
    'negotiation_strategies','outreach_actions','outreach_messages',
    'outreach_reminder_timers','policies','policy_checks','rfq_actions',
    'rfq_invitations','rfq_offers','rfqs','savings_records','sourcing_requests',
    'supplier_candidates','supplier_certificates','suppliers_v2'
  ];
BEGIN
  FOREACH t IN ARRAY tables LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
    EXECUTE format(
      'CREATE POLICY tenant_isolation ON %I USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid)',
      t
    );
  END LOOP;
END
$$;

-- `memberships` is scoped by app.user_id instead of app.tenant_id. It is the
-- one table whose own row is precisely how a session's tenant_id gets
-- discovered in the first place (get_current_membership's lookup) -- a
-- tenant_id-based policy here would be a chicken-and-egg lockout: the
-- lookup needed to learn the tenant_id would itself be blocked for not
-- yet knowing it. app.user_id is set earlier, in get_current_user, which
-- has no such problem (the user's identity comes straight from the JWT).
ALTER TABLE memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE memberships FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON memberships;
CREATE POLICY tenant_isolation ON memberships USING (
  user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
);

-- Join-based tenant_id tables (approval rows reference their parent *_actions row)
ALTER TABLE negotiation_approvals ENABLE ROW LEVEL SECURITY;
ALTER TABLE negotiation_approvals FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON negotiation_approvals;
CREATE POLICY tenant_isolation ON negotiation_approvals USING (
  action_id IN (SELECT id FROM negotiation_actions WHERE tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
);

ALTER TABLE outreach_approvals ENABLE ROW LEVEL SECURITY;
ALTER TABLE outreach_approvals FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON outreach_approvals;
CREATE POLICY tenant_isolation ON outreach_approvals USING (
  action_id IN (SELECT id FROM outreach_actions WHERE tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
);

ALTER TABLE rfq_approvals ENABLE ROW LEVEL SECURITY;
ALTER TABLE rfq_approvals FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON rfq_approvals;
CREATE POLICY tenant_isolation ON rfq_approvals USING (
  action_id IN (SELECT id FROM rfq_actions WHERE tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
);

ALTER TABLE contract_approvals ENABLE ROW LEVEL SECURITY;
ALTER TABLE contract_approvals FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON contract_approvals;
CREATE POLICY tenant_isolation ON contract_approvals USING (
  action_id IN (SELECT id FROM contract_actions WHERE tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
);
