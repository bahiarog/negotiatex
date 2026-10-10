-- Rollen, Bereiche und Kunden-Ergebnisse (CTO-Vorgabe Navigation 10.10.2026).
-- Idempotent; als Superuser (negotiatex) VOR dem Deploy ausfuehren.

ALTER TABLE memberships
  ADD COLUMN IF NOT EXISTS roles_json JSON,
  ADD COLUMN IF NOT EXISTS can_decide BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE tenants ADD COLUMN IF NOT EXISTS success_fee_pct NUMERIC(5,2);

ALTER TABLE projects
  ADD COLUMN IF NOT EXISTS responsible_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS shared_with_tenant BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS mandate_confirmed_at TIMESTAMP,
  ADD COLUMN IF NOT EXISTS mandate_confirmed_by VARCHAR(100),
  ADD COLUMN IF NOT EXISTS offer_document_id UUID REFERENCES mdc_documents(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS incumbent_company VARCHAR(255),
  ADD COLUMN IF NOT EXISTS incumbent_email VARCHAR(255),
  ADD COLUMN IF NOT EXISTS incumbent_contact VARCHAR(255),
  ADD COLUMN IF NOT EXISTS result_json JSON,
  ADD COLUMN IF NOT EXISTS invoiced_amount NUMERIC(18,2),
  ADD COLUMN IF NOT EXISTS invoiced_at TIMESTAMP,
  ADD COLUMN IF NOT EXISTS invoiced_by VARCHAR(100),
  ADD COLUMN IF NOT EXISTS archived_at TIMESTAMP;

ALTER TABLE project_events ADD COLUMN IF NOT EXISTS customer_visible BOOLEAN NOT NULL DEFAULT true;

-- Mitgliedschaften: eigene Zeile (zum Ermitteln des Mandanten) ODER Zeilen des
-- eigenen Mandanten (Administration: Nutzerliste, Zustaendigkeit). Schreiben
-- nur innerhalb des eigenen Mandanten bzw. fuer die eigene Zeile.
DROP POLICY IF EXISTS tenant_isolation ON memberships;
CREATE POLICY tenant_isolation ON memberships
  USING (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
         OR tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
              OR tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE, DELETE ON memberships TO negotiatex_app;

-- Bestehende Eigentuemer: Kunde + Admin + Entscheidungsbefugnis (wie bei Neuanlage).
UPDATE memberships SET roles_json = '["admin","customer"]'::json, can_decide = true
 WHERE role = 'owner' AND roles_json IS NULL;
-- Rafael (Testmandant): zusaetzlich Procurement-Experte, um beide Bereiche zu pruefen.
UPDATE memberships SET roles_json = '["admin","customer","procurement"]'::json, can_decide = true
 WHERE user_id = (SELECT id FROM users WHERE email = 'bahiarog@me.com');
