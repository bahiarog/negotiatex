-- Digitales Team: Arbeitsprotokoll der Agenten. NACH dem Deploy ausfuehren (Tabelle legt create_all an).
DO $$
BEGIN
  IF to_regclass('public.agent_runs') IS NOT NULL THEN
    ALTER TABLE agent_runs ENABLE ROW LEVEL SECURITY;
    ALTER TABLE agent_runs FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation ON agent_runs;
    CREATE POLICY tenant_isolation ON agent_runs
      USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
    GRANT SELECT ON agent_runs TO negotiatex_app;
    -- Geschrieben wird nur ueber die Admin-Verbindung (services/agent_log.py); die Anwendung liest nur.
    REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON agent_runs FROM negotiatex_app;
  END IF;
END $$;
