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

### NX-002 Eigene Systemmails im Posteingang ignorieren · `erledigt` (deployt 24.11.2026, bfccad5; offen: prüfen, ob der Relay den Header durchlässt) · S
**Problem:** Mails, die NegotiateX selbst an Konten mit derselben Adresse wie das Systempostfach schickt, landen im eigenen Posteingang und erscheinen als „keinem Vorgang zugeordnet“ (Rauschen im Log und im Tagesbericht).
**Abnahme:** Mails mit eigener Absenderadresse und eigener Message-ID-Domain werden vor der Zuordnung übersprungen und nur gezählt; Zuordnung echter Antworten unverändert.

### NX-003 Externe Anbieterrecherche bei leerem Bestand · `vorgeschlagen` · L
**Problem:** Der Scout sucht nur im eigenen Bestand. Neue Kunden haben einen leeren Bestand; der Agent kann dann nichts anfragen (Test „Neue Tische“ am 11.11.2026).
**Abnahme:** Vorschläge aus einer externen Quelle mit Quellenangabe und Kontaktweg, klar als „extern, ungeprüft“ gekennzeichnet; Auswahl und jede Anfrage weiter nur nach Freigabe. Vorher Entscheidung Rafael: Quelle, Kosten, Rechtsrahmen.

### NX-004 Browser-Tests ins Repository und regelmäßig ausführen · `erledigt` (deployt 24.11.2026, bfccad5; täglicher Lauf 07:30 als geplante Aufgabe auf Rafaels PC) · M
**Problem:** Die Playwright-Tests liegen nur auf dem Server (`/root/negotiatex-sim/tests`) und laufen nur von Hand.
**Abnahme:** Tests im Repo (ohne Zugangsdaten, Token werden kurzlebig erzeugt), ein Befehl führt alle aus, Ergebnis mit Datum protokolliert; Lauf vor jedem Deploy.

### NX-005 Feinschliff Beleg-Regeln aus QA NX-001 · `erledigt` (deployt 24.11.2026, bfccad5) · S
**Problem (QA 24.11.2026, alle niedrig):** `_dec` liest englische Dezimalzahlen mit drei Nachkommastellen als Tausender („0.125“ = 125), wenn die KI Text statt Zahl liefert; die Options-Schlagworte sind grob („Montage ohne Aufpreis enthalten“ gilt als Option); „ab 20 Stück … Rabatt“ und „20 x 20 cm“ gelten als Mengenbeleg; legitime Belege wie „Gemäß Ihrer Anfrage bieten wir 20 Schreibtische an“, „Menge: 20“, „20 Tagungsstühle“ werden abgelehnt (sichere Richtung). `rerun_review` verliert Options-Hinweise.
**Abnahme:** Tausender-Regel nur im Optionspfad; Ausschlüsse für „ab/mindestens“ und Maßangaben; die genannten legitimen Belege werden angenommen; Funktionstests für alle genannten Fälle.

## Versprechen einlösen (Abgleich 24.11.2026)

Abgleich des Versprechens „kauft ein, prüft AGB und Konditionen, verhandelt, sendet Auftragsdokumente, erstellt Stammdaten, trägt Savings ein, dokumentiert Freigaben, bereitet Audits vor“ mit dem Code (Stand bfccad5). Ergebnis: bis zur Verhandlung durchgängig; ab dem Zuschlag nicht eingelöst. Belege (Datei:Zeile) im Analysebericht vom 24.11.2026.

| Versprechensteil | Stand |
|---|---|
| Einkaufen | teilweise: nur eigener Bestand (NX-003); fester Testmodus in allen Mails (NX-030) |
| AGB prüfen | teilweise: nur erster Anhang, Fehlschlag erscheint als „ok“ (NX-034); RDG-Klärung offen |
| Konditionen prüfen | vorhanden |
| Verhandeln | teilweise: Ergebnis wird von Hand eingetragen (NX-036) |
| Auftragsdokumente senden | fehlt (NX-031) |
| Stammdaten erstellen | teilweise: Stammblatt wird gesammelt, nicht übernommen (NX-035, NX-012) |
| Savings eintragen | teilweise: Eingaben ohne Beleg, kein Export (NX-036) |
| Freigaben dokumentieren | teilweise: änderbar, Ablehnungen ohne Akteur (NX-033) |
| Audit-Vorbereitung | fehlt: kein Prüfpaket, Belege löschbar (NX-032, NX-033) |

| ID | Thema | Status | Aufwand | Risiko |
|---|---|---|---|---|
| NX-030 | Produktivbetrieb statt festem „Testlauf, keine Beauftragung“: Einstellung je Mandant/Vorhaben, Standard Test, Umschalten nur Admin und protokolliert | freigegeben (Rafael, 24.11.2026) | M | hoch (vorher Rechtsrahmen/Haftung) |
| NX-031 | Auftragsdokument aus dem Zuschlag: PDF mit Zahlen nur aus der beauftragten Angebotsversion, im Data Center mit SHA-256, Versand nur nach hash-gebundener Freigabe, Rückbestätigung zuordnen | freigegeben (Rafael, 24.11.2026) | L | hoch (Vorlage von Legal freigeben) |
| NX-032 | Prüfpaket je Vorhaben: ZIP mit Zeitstrahl, Freigaben/Ablehnungen, Mails im Original, Dokumentversionen, Prüfberichten, Ergebnis und manifest.json mit Hashes; Mandantentrennung getestet | freigegeben (Rafael, 24.11.2026) | L | mittel |
| NX-033 | Unveränderlichkeit: kein UPDATE/DELETE auf Freigaben und Zeitstrahl für die App-Rolle, Ablehnungen mit Akteur, Aufbewahrungssperre der Belege ab Zuschlag, Dubletten verknüpfen statt umhängen | erledigt (deployt b9de08b, Migration + Trockenlauf 24.11.2026) | M | mittel |
| NX-034 | AGB-Prüfung belastbar: alle Anhänge, sichtbarer Status „nicht geprüft“ bei Fehlschlag/Kürzung, Knopf „erneut prüfen“ | erledigt (deployt b9de08b, 24.11.2026) | M | mittel |
| NX-035 | Stammdaten nach Zuschlag in den Lieferantenstamm übernehmen; Bankdaten-Prüfung im Vier-Augen-Prinzip mit Protokoll | freigegeben (Rafael, 24.11.2026) | M | mittel |
| NX-036 | Belegte Einsparungen: Verhandlungsergebnis und Rechnungsbetrag nur mit Beleg oder als „manuell“ gekennzeichnet; CSV-Export | freigegeben (Rafael, 24.11.2026) | M | niedrig |
| NX-037 | End-to-End-Abnahmetest je Versprechensteil (Mail-Stub, zweiter Mandant als Gegenprobe, Rollback), täglich | erledigt (deployt f9b2fdb, Cron 05:45 UTC, 25.11.2026: 34/34) | M | niedrig |
| NX-038 | Folgepunkte NX-037: Preis im Verhandlungsentwurf als „8500.0000“ statt „8.500,00 €“ formatiert; Gegenproben des Durchgangstests mit Positivkontrolle (A sieht, B nicht); deploy.py baut das Backend auch bei reinen ops/-Aenderungen neu | vorgeschlagen | S | niedrig |

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
