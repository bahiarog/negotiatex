-- NX-033: Unveraenderlichkeit des Pruefpfads. Idempotent; als Superuser (negotiatex) NACH dem Deploy
-- ausfuehren (die neue Tabelle action_rejections legt create_all beim Start des Backends an).
--
-- Fuer die Anwendungsrolle negotiatex_app gilt danach:
--   * Freigaben (*_approvals) und Ablehnungen (action_rejections): nur lesen und anlegen.
--   * Zeitstrahl (project_events): nur lesen und anlegen; einzige Ausnahme ist das Nachtragen von
--     emailed_at (Versandzeit der Meilenstein-Mail), per Spaltenrecht.
-- Loeschungen ueber Fremdschluessel-Kaskaden (z. B. Mandant geloescht) laufen mit den Rechten des
-- Tabelleneigentuemers und sind davon nicht betroffen.
-- requisition_approvals bleibt aenderbar: dort ist die Zeile selbst der Arbeitsschritt (Status wird gesetzt).

DO $$
DECLARE t text;
BEGIN
  -- Ablehnungen: Mandantentrennung wie alle Fachtabellen
  IF to_regclass('public.action_rejections') IS NOT NULL THEN
    ALTER TABLE action_rejections ENABLE ROW LEVEL SECURITY;
    ALTER TABLE action_rejections FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation ON action_rejections;
    CREATE POLICY tenant_isolation ON action_rejections
      USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
  END IF;

  FOREACH t IN ARRAY ARRAY['outreach_approvals', 'rfq_approvals', 'negotiation_approvals',
                           'contract_approvals', 'action_rejections'] LOOP
    IF to_regclass('public.' || t) IS NOT NULL THEN
      EXECUTE format('REVOKE UPDATE, DELETE, TRUNCATE ON %I FROM negotiatex_app', t);
      EXECUTE format('GRANT SELECT, INSERT ON %I TO negotiatex_app', t);
    END IF;
  END LOOP;

  IF to_regclass('public.project_events') IS NOT NULL THEN
    REVOKE UPDATE, DELETE, TRUNCATE ON project_events FROM negotiatex_app;
    GRANT SELECT, INSERT ON project_events TO negotiatex_app;
    GRANT UPDATE (emailed_at) ON project_events TO negotiatex_app;
  END IF;
END $$;

-- Pruefung (Ausgabe soll fuer jede Tabelle update=f, delete=f zeigen; project_events.emailed_at update=t):
SELECT t AS tabelle,
       has_table_privilege('negotiatex_app', t, 'UPDATE') AS "update",
       has_table_privilege('negotiatex_app', t, 'DELETE') AS "delete",
       has_table_privilege('negotiatex_app', t, 'INSERT') AS "insert"
FROM unnest(ARRAY['outreach_approvals', 'rfq_approvals', 'negotiation_approvals', 'contract_approvals',
                  'action_rejections', 'project_events']) AS t
WHERE to_regclass('public.' || t) IS NOT NULL;
SELECT has_column_privilege('negotiatex_app', 'project_events', 'emailed_at', 'UPDATE') AS emailed_at_update;
