# NegotiateX Backlog

Stand 23.11.2026. Ziel: zahlende Kunden für den Einkauf von Dienstleistungen.

**Status:** `vorgeschlagen` → `freigegeben` (nur Rafael) → `in Arbeit` → `QA` → `erledigt` (deployt und geprüft).
Ein Agent arbeitet nur an Tickets mit Status `freigegeben`, immer genau eines, im Branch `agent/<ID>`.
Deploy nur mit `ops/deploy.py --apply` nach ausdrücklicher Freigabe durch Rafael.

Aufwand: S = bis 2 h, M = halber bis ganzer Tag, L = mehrere Tage.

## Jetzt

### NX-001 Pauschalangebote für Stückmengen richtig vergleichen · `erledigt` (deployt 24.11.2026, e6c6213) · M
**Problem:** Beide Angebote für „20 höhenverstellbare Schreibtische“ wurden als „Menge 1 (Pauschale)“ erfasst. Der Vergleich mit der erwarteten Menge 20 markiert sie deshalb als „eingeschränkt vergleichbar“ (`services/rfq_classifier.py:_comparability`), und die Auswertung nennt kein günstigstes Angebot – obwohl beide denselben Umfang anbieten.
**Abnahme:**
- Ein Gesamtpreis für die angefragte Menge wird als vergleichbar erkannt, wenn die Menge im Angebotstext belegt ist (Zitat).
- Echte Mengenabweichungen (z. B. 15 statt 20 Tische) bleiben markiert.
- Optionale Positionen (z. B. „Memory-Bedienteil, abwählbar“) werden nicht in den Vergleichspreis gerechnet, aber als Hinweis gezeigt.
- Nachweis an den beiden Angeboten im Vorhaben „Schreibtische“ per Trockenlauf.

### NX-002 Eigene Systemmails im Posteingang ignorieren · `QA` (FREIGABE für bffd0c7, 24.11.2026; Deploy offen) · S
**Problem:** Mails, die NegotiateX selbst an Konten mit derselben Adresse wie das Systempostfach schickt, landen im eigenen Posteingang und erscheinen als „keinem Vorgang zugeordnet“ (Rauschen im Log und im Tagesbericht).
**Abnahme:** Mails mit eigener Absenderadresse und eigener Message-ID-Domain werden vor der Zuordnung übersprungen und nur gezählt; Zuordnung echter Antworten unverändert.

### NX-003 Externe Anbieterrecherche bei leerem Bestand · `vorgeschlagen` · L
**Problem:** Der Scout sucht nur im eigenen Bestand. Neue Kunden haben einen leeren Bestand; der Agent kann dann nichts anfragen (Test „Neue Tische“ am 11.11.2026).
**Abnahme:** Vorschläge aus einer externen Quelle mit Quellenangabe und Kontaktweg, klar als „extern, ungeprüft“ gekennzeichnet; Auswahl und jede Anfrage weiter nur nach Freigabe. Vorher Entscheidung Rafael: Quelle, Kosten, Rechtsrahmen.

### NX-004 Browser-Tests ins Repository und regelmäßig ausführen · `QA` (FREIGABE für 5f5e885, 24.11.2026; Deploy offen) · M
**Problem:** Die Playwright-Tests liegen nur auf dem Server (`/root/negotiatex-sim/tests`) und laufen nur von Hand.
**Abnahme:** Tests im Repo (ohne Zugangsdaten, Token werden kurzlebig erzeugt), ein Befehl führt alle aus, Ergebnis mit Datum protokolliert; Lauf vor jedem Deploy.

### NX-005 Feinschliff Beleg-Regeln aus QA NX-001 · `QA` (FREIGABE für 65458fc, 24.11.2026; Deploy offen) · S
**Problem (QA 24.11.2026, alle niedrig):** `_dec` liest englische Dezimalzahlen mit drei Nachkommastellen als Tausender („0.125“ = 125), wenn die KI Text statt Zahl liefert; die Options-Schlagworte sind grob („Montage ohne Aufpreis enthalten“ gilt als Option); „ab 20 Stück … Rabatt“ und „20 x 20 cm“ gelten als Mengenbeleg; legitime Belege wie „Gemäß Ihrer Anfrage bieten wir 20 Schreibtische an“, „Menge: 20“, „20 Tagungsstühle“ werden abgelehnt (sichere Richtung). `rerun_review` verliert Options-Hinweise.
**Abnahme:** Tausender-Regel nur im Optionspfad; Ausschlüsse für „ab/mindestens“ und Maßangaben; die genannten legitimen Belege werden angenommen; Funktionstests für alle genannten Fälle.

## Fahrplan (Analyse 17.10.2026)

| ID | Thema | Status | Aufwand |
|---|---|---|---|
| NX-010 | Angebots-Check vor der Anmeldung ausfüllbar (erst Formular, dann Registrierung) | vorgeschlagen | M |
| NX-011 | Arbeitsbereich um das Vorhaben ordnen; Sourcing/RFQ/Fälle in die Bearbeitungsansicht; Kürzel (B4, RFQ, Case) entfernen | vorgeschlagen | L |
| NX-012 | Gemeinsame Lieferantenakte (Stamm, Kandidaturen, Data Center) | vorgeschlagen | L |
| NX-013 | Altbestand (klassisches Dashboard, Plattform-Admin-Schnittstellen) stilllegen | vorgeschlagen | M |
| NX-014 | Profil: Passwort ändern, Benachrichtigungen | vorgeschlagen | S |
| NX-015 | Installierbare Web-App mit Push-Benachrichtigungen | vorgeschlagen | M |
| NX-016 | Erinnerungen von Testwerten (15/40 Minuten) auf Produktionswerte umstellen | vorgeschlagen | S |

## KI-Bausteine (Hugging Face, Analyse 04.11.2026)

| ID | Thema | Status | Aufwand |
|---|---|---|---|
| NX-020 | Zweitmeinung bei der Angebotsextraktion: offenes Modell liest parallel, Abweichungen bei kritischen Feldern gehen in die Prüfung; Messung am Gold-Datensatz | vorgeschlagen | M |
| NX-021 | Reranker für Suche und Marktreferenz (statt fester Schwelle 0,55) | vorgeschlagen | M |
| NX-022 | OCR für gescannte Angebote (vorher prüfen, ob Scans heute scheitern) | vorgeschlagen | M |

## Entscheidungen von Rafael (kein Agent)

- Rechtsdienstleistungsgesetz: AGB-Prüfung für Dritte anwaltlich klären, bevor sie verkauft wird.
- Erfolgsgebühr und Preise festlegen.
- Externes Backup-Ziel buchen.
- Repository auf „privat“ stellen.

## Erledigt

| ID | Thema | Commit |
|---|---|---|
| – | Registrierung nur für Kundenkonten, eigene Seite | `8d41d21` |
| – | Angebote aus Antworten auf die Erstanfrage, Eingangsbestätigung, Auswertung an Kunde und Team, Team-Mails, Scout-Autostart | `2626b8d` |
| – | Deploy-Skript mit Sicherung und Rücksprung, Tagesbericht, kein CI-Deploy | `80c7395` |
| NX-001 | Pauschalangebote für Stückmengen vergleichen, optionale Positionen als Hinweis | `a12630b` |
