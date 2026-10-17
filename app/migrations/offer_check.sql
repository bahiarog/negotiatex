-- Angebots-Check: Referenzpreis und Sofort-Ergebnis. VOR dem Deploy ausfuehren.
ALTER TABLE projects
  ADD COLUMN IF NOT EXISTS reference_price NUMERIC(18,2),
  ADD COLUMN IF NOT EXISTS check_json JSON;
