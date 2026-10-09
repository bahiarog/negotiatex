# Abnahmefaelle Preisanalytik (Anleitung S. 13/14/18). Ausfuehren:
# docker exec -w /app -e PYTHONPATH=/app negotiatex-backend python tests/mdc_analytics_acceptance.py
import copy
import json
from datetime import date
from services.mdc_analytics import compare, explain

AS_OF = date(2026, 10, 9)
CAT = "cat-video"
KNOWN = {k: {"status": "exclusive", "amount": None} for k in ("fracht", "setup", "reise", "mindesthonorar")}


def item(id_, value, *, role="Editor", sen="Mid", reg="DACH", unit="Stunde", cur="EUR", status="quoted",
         supplier=None, doc=None, offer_date="2026-09-15", valid_from=None, valid_to=None, issues=None, cat=CAT, anc=KNOWN):
    return {"id": id_, "document_id": doc or f"doc-{id_}", "document_title": f"Angebot {id_}", "version_number": 1,
            "category_id": cat, "supplier_id": supplier if supplier is not None else f"sup-{id_}", "supplier_name": f"Agentur {id_}",
            "role_or_item": role, "seniority": sen, "region": reg, "canonical_unit": unit, "original_currency": cur,
            "normalized_amount_per_canonical_unit": None if value is None else str(value), "price_status": status,
            "offer_date": offer_date, "valid_from": valid_from, "valid_to": valid_to, "ancillary_costs_json": anc,
            "open_issues_json": issues or [], "source_evidence": f"Zeile 1: {id_}", "created_at": "2026-10-01T00:00:00"}


TARGET = item("NEU", 150, offer_date="2026-10-08")
REFS = [item("A", 100), item("B", 110), item("C", 120)]
results = []


def check(name, cond, detail=""):
    results.append(cond)
    print(("PASS " if cond else "FAIL ") + name + (f"  | {detail}" if detail else ""))


# Use Case Seite 14 / API-Beispiel Seite 13
r = compare(TARGET, REFS, AS_OF)
q = next(c for c in r["classes"] if c["reference_class"] == "quoted")
s = q["stats"]
check("Use Case: Median 110", s["median"] == "110.00", s["median"])
check("Use Case: 36,36 % ueber Median", s["premium_vs_median_pct"] == "36.36", s["premium_vs_median_pct"])
check("Use Case: hypothetischer Abschlag 26,67 %", s["hypothetical_reduction_pct"] == "26.67", s["hypothetical_reduction_pct"])
check("Use Case: 3 Beobachtungen / 3 Projekte / 3 Lieferanten", (s["n"], s["projects"], s["suppliers"]) == (3, 3, 3))
check("3 statt 10 Projekte -> insufficient_evidence", r["status"] == "insufficient_evidence" and q["status"] == "insufficient_evidence")
check("comparison_status deskriptiv", q["comparison_status"] == "comparable_for_descriptive_review")
txt = explain(r)
check("Erklaerung: keine Marktpreisbehauptung", "kein repraesentativer Marktpreis" in txt and "Alle 3 Referenzen" in txt)
check("Erklaerung: interne Referenz nicht als Konkurrenzangebot", "nicht als solches genannt" in txt)

# Junior statt Senior / Tagessatz ohne Stunden / abgelaufenes Angebot
extra = [item("JUN", 80, sen="Junior"), item("TAG", 900, unit="Tag"),
         item("ALT", 95, offer_date="2023-05-01", valid_to="2023-06-30")]
r2 = compare(TARGET, REFS + extra, AS_OF)
reasons = {e["line_item_id"]: e["reason"] for e in r2["excluded"]}
check("Junior statt Senior ausgeschlossen", "JUN" in reasons and "Senioritaet" in reasons["JUN"], reasons.get("JUN"))
check("Tagessatz ohne Stunden nicht in Stunden-Kohorte", "TAG" in reasons and "Tagessatz" in reasons["TAG"], reasons.get("TAG"))
check("Altes Angebot nur historisch", [h["line_item_id"] for h in r2["historical"]] == ["ALT"])
check("Kohorte unveraendert 100/110/120", next(c for c in r2["classes"] if c["reference_class"] == "quoted")["stats"]["median"] == "110.00")

# Listenpreise / Angebote nicht vermischen
r3 = compare(TARGET, REFS + [item("LIST", 85, status="list_price")], AS_OF)
check("Listenpreis als eigene Klasse", sorted(c["reference_class"] for c in r3["classes"]) == ["list_price", "quoted"]
      and next(c for c in r3["classes"] if c["reference_class"] == "quoted")["stats"]["n"] == 3)
check("Primaere Klasse = Angebotspreise", r3["primary_class"] == "quoted")

# Ein Projekt je Lieferant
r4 = compare(TARGET, REFS + [item("A2", 105, supplier="sup-A", doc="doc-A")], AS_OF)
q4 = next(c for c in r4["classes"] if c["reference_class"] == "quoted")
check("Zweite Beobachtung desselben Projekts zaehlt nicht", q4["stats"]["n"] == 3 and any("desselben Projekts" in e["reason"] for e in r4["excluded"]))

# Mehrere Projekte eines Lieferanten: keine zusaetzliche Anbieterbreite
r5 = compare(TARGET, [item("P1", 100, supplier="S"), item("P2", 104, supplier="S"), item("P3", 120, supplier="T")], AS_OF)
s5 = r5["classes"][0]["stats"]
check("Projekte vs. Lieferanten getrennt gezaehlt", (s5["n"], s5["projects"], s5["suppliers"]) == (3, 3, 2))
check("Dominanter Lieferant markiert", s5["dominant_supplier"] is not None and s5["dominant_supplier"]["share_pct"] == "66.67")

# Brutto unklar / fehlende Daten am Angebot
bad = item("NEU2", None, issues=[{"code": "tax_basis_unknown", "message": "Steuerbasis unbekannt.", "blocking": True}])
r6 = compare(bad, REFS, AS_OF)
check("Brutto unklar -> missing_data, kein Vergleich", r6["status"] == "missing_data" and not r6["classes"])
check("Erklaerung nennt die Luecke", "Steuerbasis unbekannt" in explain(r6))

# Nebenkosten unbekannt -> kein TCO
r7 = compare(item("NEU3", 150, anc={}), REFS, AS_OF)
check("Fracht unbekannt -> TCO nicht vergleichbar", r7["tco"]["status"] == "not_comparable")

# Keine passenden Daten
r8 = compare(item("NEU4", 150, role="Colorist"), REFS, AS_OF)
check("Keine Daten -> sauberer Datenmangel", r8["status"] == "no_comparable_references" and len(r8["excluded"]) == 3)

# Benchmark-Schwelle: 10 Projekte, 5 Lieferanten
many = [item(f"M{i}", 100 + i, supplier=f"S{i % 5}") for i in range(10)]
r9 = compare(TARGET, many, AS_OF)
check("Ab 10 Projekten / 5 Lieferanten benchmark_eligible", r9["status"] == "benchmark_eligible", str(r9["classes"][0]["stats"]["q1"]))
check("Quartile ab n>=5 berechnet", r9["classes"][0]["stats"]["q1"] is not None)

# Ausreisser markiert, nicht entfernt
r10 = compare(TARGET, [item(f"O{i}", v) for i, v in enumerate([100, 102, 104, 106, 400])], AS_OF)
s10 = r10["classes"][0]["stats"]
check("Ausreisser markiert, aber in n enthalten", s10["n"] == 5 and s10["outlier_line_item_ids"] == ["O4"])

# Andere Kategorie / anderer Mandant faellt nie in die Kohorte
r11 = compare(TARGET, REFS + [item("X", 50, cat="andere")], AS_OF)
check("Andere Kategorie ausgeschlossen", any(e["line_item_id"] == "X" and "Kategorie" in e["reason"] for e in r11["excluded"]))

# Wiederholung: gleiche Eingabe -> identisches Ergebnis (auch bei anderer Reihenfolge)
a = compare(TARGET, REFS + extra, AS_OF)
b = compare(copy.deepcopy(TARGET), list(reversed(REFS + extra)), AS_OF)
check("Wiederholung liefert identische Zahlen und Quellen", json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True))

# Laufende Ratecard (gueltig bis Jahresende) ist trotz altem Startdatum aktuell
r12 = compare(TARGET, [item("RC", 90, offer_date=None, valid_from="2026-01-01", valid_to="2026-12-31")], AS_OF)
check("Gueltige Ratecard mit altem Startdatum zaehlt als aktuell", r12["classes"] and r12["classes"][0]["stats"]["n"] == 1)

print("\n--- Erklaerung Use Case ---\n" + explain(r2))
print(f"\n{sum(results)}/{len(results)} bestanden")
