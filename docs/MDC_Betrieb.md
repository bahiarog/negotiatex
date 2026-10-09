# Master Data Center – Betriebsanleitung

Stand 09.10.2026 · gilt für negotiatex.ai (Server 187.124.10.46, Verzeichnis `/root/negotiatex`)

## 1. Überblick

| Teil | Wo | Hinweis |
|---|---|---|
| API | Container `negotiatex-backend`, `/api/v1/mdc/...` | 2 uvicorn-Worker |
| Datenbank | Container `negotiatex-db`, Image `negotiatex-postgres:16-pgvector0.8.0` (`db/Dockerfile`) | PostgreSQL 16.12 + pgvector 0.8.0, RLS je Mandant |
| Originaldokumente | Docker-Volume `negotiatex_uploads`, Pfad `/app/uploads/mdc/<mandant>/<sha256>.<ext>` | nur über authentifizierte Download-Route |
| Embedding-Modell | im Backend-Image unter `/opt/models` (`jinaai/jina-embeddings-v2-base-de`) | lokal, `HF_HUB_OFFLINE=1`, kein externer Dienst |
| Preisextraktion | Anthropic API (`ANTHROPIC_API_KEY` in `.env`) | einziger externer Aufruf; KI schlägt nur vor |
| Oberfläche | `https://negotiatex.ai/data-center` | Inbox, Review-Queue, Angebotsvergleich, Belegsuche, Kennzahlen |
| Monitoring | Prometheus/Grafana (`/root/monitoring`), Kennzahlen-Tab | `/api/metrics` ist öffentlich gesperrt |

## 2. Rollen

- **Owner** entscheidet: Positionen freigeben/ablehnen, Kategorien, Nutzungsrechte, Widerruf, Aufbewahrungssperre, Löschen, Audit einsehen.
- **Member** arbeitet zu: hochladen, extrahieren, korrigieren, Prüfanfragen, Analysen, Suche.
- Durchgesetzt in `services/mdc_governance.py`; die Oberfläche blendet nur aus.

## 3. Automatische Abläufe

| Wann | Was | Ergebnis prüfen |
|---|---|---|
| täglich 03:15 | `ops/backup.sh`, danach `ops/restore_test.sh` (`/etc/cron.d/negotiatex-backup`) | `tail /var/log/negotiatex-backup.log` – erwartet je Tag `backup ok` und `restore-test ok` |
| täglich 02:30 | Rechteablauf: abgelaufene Nutzungsrechte aus Suche/Index nehmen (Scheduler im Backend) | Audit-Aktion `rights_expired_deindexed`; Kennzahl „Suchabschnitte gesperrter Dok.“ muss 0 sein |

Backups liegen 14 Tage in `/root/negotiatex/backups/daily` (Rechte 600/700). **Achtung:** Sie liegen auf demselben Server – ein Serverausfall trifft Daten und Backup gleichzeitig (siehe Abschnitt 9).

## 4. Wiederherstellung im Ernstfall

1. Neuestes funktionierendes Backup wählen: `grep "restore-test ok" /var/log/negotiatex-backup.log | tail -1` (Zeitstempel = `backup=<TS>`).
2. Backend stoppen: `cd /root/negotiatex && docker compose stop backend`.
3. Datenbank ersetzen:
   ```sh
   docker exec negotiatex-db psql -U negotiatex -d postgres -c "DROP DATABASE negotiatex WITH (FORCE)"
   docker exec -i negotiatex-db pg_restore -U negotiatex -C -d postgres < backups/daily/negotiatex_<TS>.dump
   ```
   Rollen bestehen weiter; nur bei neuem leeren DB-Container zuerst `globals_<TS>.sql` einspielen.
4. Uploads ersetzen: `docker run --rm -v negotiatex_uploads:/data -v $PWD/backups/daily:/b alpine sh -c "rm -rf /data/* && tar xzf /b/uploads_<TS>.tar.gz -C /data"`.
5. Backend starten, prüfen: Kennzahlen-Tab → Zustand „ok“, `GET /api/v1/mdc/health`.

Der Ablauf wird täglich durch `restore_test.sh` in einem Wegwerf-Container geprobt (Zeilen je Tabelle, Policies, App-Rolle ohne BYPASSRLS, Vektoren, Dateizahl).

## 5. Rechte, Widerruf, Löschung

- **Widerruf** (Dokumentansicht → „Nutzung widerrufen“): wirkt sofort auf Vergleich, Suche und Belegzugriff; Daten bleiben bis zur Löschentscheidung.
- **Aufbewahrungssperre**: verhindert Löschen (z. B. HGB/AO-Fristen). Grund ist Pflicht und steht im Audit.
- **Löschen**: entfernt Original, Versionen, Positionen, Suchabschnitte und Vektoren. In bestehenden Analyse-Akten werden Titel, Lieferant, Zitat und Namen im Erklärungstext geschwärzt; die damals berechneten Zahlen bleiben als Entscheidungsnachweis. **Dieser Umfang ist mit Legal/Datenschutz zu bestätigen.**
- Löschungen erreichen die täglichen Backups erst nach Ablauf der 14-Tage-Aufbewahrung.

## 6. Regeländerungen, Neuindexierung

- Prüfregeln: `services/mdc_extractor.py`, Version `NORMALIZATION_VERSION` (aktuell v2: Steuerbasis und Stundenzahl nur mit im Dokument nachgewiesenem Zitat oder menschlicher Bestätigung). Nach jeder Regeländerung:
  `docker exec -w /app -e PYTHONPATH=/app negotiatex-backend python maintenance/recheck_mdc_rules.py` – Wertänderungen landen im Audit (`rules_recheck`).
- Suchindex (Zuschnitt `services/mdc_search.py`, Modell `services/mdc_embeddings.py`). Nach Änderung von Zuschnitt oder Modell:
  `... python maintenance/reindex_mdc_search.py`. Vektoren verschiedener Modelle werden nie gemischt; die Suche nutzt nur Abschnitte des aktuellen Modells.
- Vergleichsregeln (Aktualität 90 Tage, Benchmark ab 10 Projekten/5 Lieferanten): `services/mdc_analytics.py`, Policy `RATECARD-PILOT-v1`. Eine Änderung braucht eine neue Policy-Version, damit alte Akten nachvollziehbar bleiben.
- Schemaänderungen: Migrationen in `app/migrations/` (idempotent, Teil 1 vor, Teil 2 nach dem Deploy). `create_all` legt neue Tabellen an, ergänzt aber keine Spalten.

## 7. Abnahmetests

```sh
cd /app   # im Container: docker exec -w /app -e PYTHONPATH=/app negotiatex-backend python <test>
python tests/mdc_normalize_acceptance.py      # Prüfregeln (26 Fälle)
python tests/mdc_analytics_acceptance.py      # Vergleich/Statistik (27 Fälle)
python tests/mdc_permissions_acceptance.py    # Mandanten-/Rollentrennung über API und RLS (71 Fälle)
python tests/gold/evaluate_extraction.py      # Fachabnahme Gold-Datensatz (24 Dok., 106 Positionen, ruft die KI auf)
python tests/gold/mutation_check.py           # Gegenprobe: erkennt die Auswertung eingebaute Fehler? (ohne KI)
```
Vor jedem Release mindestens die ersten drei und die Gegenprobe; den Gold-Lauf nach Änderungen an Prompt, Modell oder Prüfregeln.

## 8. Worauf achten (Kennzahlen-Tab)

| Kennzahl | Normal | Handeln wenn |
|---|---|---|
| Zustand | ok | „eingeschränkt“: Vektoren fehlen oder fremdes Modell → `reindex_mdc_search.py` |
| Suchabschnitte gesperrter Dokumente | 0 | > 0: Rechteablauf-Job prüfen |
| Belege im Original gefunden | 100 % | < 100 %: Extraktion/Belegstellen prüfen |
| offene Pflichtfelder | klein, sinkend | wächst: Review-Queue abarbeiten |
| Datenbreite je Kategorie | – | „zu schmal“: weitere Referenzen erheben; Vergleiche bleiben deskriptiv |
| Extraktion p95 | < 30 s | deutlich höher: Anthropic-Status, Dokumentgröße |

Kosten je Dokument erscheinen erst, wenn `MDC_PRICE_INPUT_PER_MTOK` und `MDC_PRICE_OUTPUT_PER_MTOK` in `.env` gesetzt sind.

## 9. Bekannte Grenzen und offene Entscheidungen

- **Backups liegen nur auf dem Server selbst.** Für echte Ausfallsicherheit wird ein externes Ziel benötigt (z. B. Storage Box / S3, verschlüsselt). Entscheidung und Zugangsdaten offen.
- **Keine Verschlüsselung ruhender Daten** (Volume, Backups) über die Dateirechte hinaus.
- **Gold-Datensatz ist synthetisch.** Er belegt Pipeline und Regeln, nicht die Qualität bei echten, unsauberen Dokumenten (Scans, OCR). Für die fachliche Freigabe 20–30 echte, freigegebene Dokumente mit manuell geprüften Positionen nachziehen.
- **Rolle „Procurement Data Steward“** existiert noch nicht; Owner übernehmen die Freigabe.
- **Abkürzungen** (z. B. „DoP“) versteht die semantische Suche nicht – künftig über Synonyme in der Taxonomie.
- **Stunden-Beleg aus Tabellenkopf**: steht „Std./Tag“ nur im Kopf, gilt die Stundenzahl, wenn dieselbe Zahl als eigener Wert in der Positionszeile steht. Eine zufällig gleiche Zahl in einer anderen Spalte (z. B. Menge 8) würde ebenfalls akzeptiert – jede Position wird dennoch von einem Menschen freigegeben.
- **Hintergrund-Jobs laufen in beiden Workern.** Doppelte E-Mails verhindern eindeutige Indizes; der MDC-Job nutzt eine Datenbanksperre.
