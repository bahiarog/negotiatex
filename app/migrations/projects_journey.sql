-- Vorhaben / Customer Journey. Idempotent; als Superuser (negotiatex) NACH dem Deploy
-- ausfuehren (Tabellen legt create_all an). Mandantentrennung wie alle Fachtabellen.

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['projects', 'project_events', 'offer_reviews'] LOOP
    IF to_regclass('public.' || t) IS NOT NULL THEN
      EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
      EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
      EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
      EXECUTE format('CREATE POLICY tenant_isolation ON %I USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid)', t);
      EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO negotiatex_app', t);
    END IF;
  END LOOP;
  -- Zeitstrahl ist append-only fuer die Anwendung (emailed_at wird beim Anlegen gesetzt).
  IF to_regclass('public.project_events') IS NOT NULL THEN
    REVOKE DELETE, TRUNCATE ON project_events FROM negotiatex_app;
  END IF;
END $$;
