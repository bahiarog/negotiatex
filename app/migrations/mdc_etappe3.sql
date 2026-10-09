-- Master Data Center Etappe 3. Idempotent; als Superuser (negotiatex) ausfuehren.
-- 1. VOR dem Deploy: neue Spalte an bestehender Tabelle (create_all ergaenzt keine Spalten).
-- 2. NACH dem Deploy (Tabellen von create_all angelegt): Volltextindex + RLS.
--    Teil 2 ueberspringt sich selbst, solange die Tabellen noch fehlen.

ALTER TABLE mdc_documents ADD COLUMN IF NOT EXISTS rfq_offer_id UUID REFERENCES rfq_offers(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS ix_mdc_documents_rfq_offer_id ON mdc_documents (rfq_offer_id);

DO $$
DECLARE t text;
BEGIN
  IF to_regclass('public.mdc_retrieval_chunks') IS NOT NULL THEN
    EXECUTE 'CREATE INDEX IF NOT EXISTS ix_mdc_chunks_fts ON mdc_retrieval_chunks USING GIN (to_tsvector(''german'', text))';
  END IF;
  FOREACH t IN ARRAY ARRAY['mdc_retrieval_chunks','mdc_analysis_snapshots'] LOOP
    IF to_regclass('public.' || t) IS NOT NULL THEN
      EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
      EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
      EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
      EXECUTE format('CREATE POLICY tenant_isolation ON %I USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid)', t);
    END IF;
  END LOOP;
END $$;
