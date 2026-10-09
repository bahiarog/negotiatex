-- Master Data Center Etappe 4 (Rechte, Aufbewahrung, Audit, Kennzahlen). Idempotent;
-- als Superuser (negotiatex) ausfuehren. Teil 1 VOR dem Deploy, Teil 2 NACH dem Deploy
-- (mdc_audit_events wird von create_all angelegt; Teil 2 ueberspringt sich bis dahin).

ALTER TABLE mdc_documents
  ADD COLUMN IF NOT EXISTS usage_purpose VARCHAR(255),
  ADD COLUMN IF NOT EXISTS usage_scope VARCHAR(255),
  ADD COLUMN IF NOT EXISTS rights_valid_until DATE,
  ADD COLUMN IF NOT EXISTS rights_revoked_at TIMESTAMP,
  ADD COLUMN IF NOT EXISTS rights_revoked_by VARCHAR(100),
  ADD COLUMN IF NOT EXISTS rights_revoked_reason TEXT,
  ADD COLUMN IF NOT EXISTS legal_hold BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS legal_hold_reason TEXT;

ALTER TABLE mdc_line_items
  ADD COLUMN IF NOT EXISTS tax_evidence TEXT,
  ADD COLUMN IF NOT EXISTS tax_confirmed BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS hours_evidence TEXT,
  ADD COLUMN IF NOT EXISTS hours_confirmed BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE mdc_document_versions
  ADD COLUMN IF NOT EXISTS extraction_input_tokens INTEGER,
  ADD COLUMN IF NOT EXISTS extraction_output_tokens INTEGER,
  ADD COLUMN IF NOT EXISTS extraction_seconds NUMERIC(8,2);

DO $$
BEGIN
  IF to_regclass('public.mdc_audit_events') IS NOT NULL THEN
    ALTER TABLE mdc_audit_events ENABLE ROW LEVEL SECURITY;
    ALTER TABLE mdc_audit_events FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation ON mdc_audit_events;
    CREATE POLICY tenant_isolation ON mdc_audit_events
      USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
    -- Append-only fuer die Anwendung: kein Aendern, kein Loeschen von Audit-Eintraegen.
    REVOKE UPDATE, DELETE, TRUNCATE ON mdc_audit_events FROM negotiatex_app;
  END IF;
END $$;
