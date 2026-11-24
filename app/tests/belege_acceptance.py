"""Akzeptanztest Beleg-Regeln (NX-001/NX-005): Mengenbelege, Preise, optionale Positionen.

Ohne Datenbank und ohne KI. Aufruf aus dem Repo-Wurzelverzeichnis:
    python app/tests/belege_acceptance.py
Exit-Code 0 = alle Faelle wie erwartet.
"""
import os
import sys
import types
from decimal import Decimal as D

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
if "database" not in sys.modules:  # Modelle brauchen nur Base, keine Engine
    from sqlalchemy.orm import DeclarativeBase
    _db = types.ModuleType("database")

    class _Base(DeclarativeBase):
        pass
    _db.Base = _Base
    sys.modules["database"] = _db

from services.rfq_classifier import _is_quantity_quote, verify_priced_quantity  # noqa: E402
from services.offer_intake import (_dec, _dec_de, _amounts_in, _optional_items,  # noqa: E402
                                   add_extraction_notes, reconcile_prices)

FAILS = 0


def check(name, got, want):
    global FAILS
    ok = got == want
    FAILS += not ok
    print(("ok   " if ok else "FEHL ") + name + " -> " + repr(got) + ("" if ok else f"  (erwartet {want!r})"))


# Mengenbelege fuer die angefragte Menge 20: True = Beleg angenommen
QUANTITY_CASES = [
    ("Gemäß Ihrer Anfrage bieten wir 20 Schreibtische an", True),
    ("Gemäß Ihrer Anfrage bieten wir Ihnen 20 Schreibtische an", True),
    ("Menge: 20", True),
    ("Menge:20 Stück", True),
    ("Stück: 20", True),
    ("20 Tagungsstühle", True),
    ("Lieferung und Leistung: 20 höhenverstellbare Schreibtische", True),
    ("20 x Schreibtisch ErgoLift", True),
    ("Angebot über 20 Schreibtische", True),
    ("Steh-Sitz-Schreibtisch 20 Stk.", True),
    ("Wir liefern 20 Schreibtische", True),
    # Anfragemenge zitiert (QA NX-005, hoch)
    ("Wir liefern 15 statt der angefragten 20 Schreibtische", False),
    ("Wir bieten 15 Tische an, Ihre Anfrage lautete 20 Stück", False),
    ("Sie haben 20 Schreibtische angefragt, wir liefern 15", False),
    ("Wir bieten folgendes zu Ihrer Anfrage über 20 Schreibtische", False),
    ("Wir bieten Ihnen gemäß Anfrage über 20 Schreibtische folgendes an", False),
    ("Laut Ihrer Anfrage 20 Schreibtische, wir bieten 18 an", False),
    ("Ihre Anfrage über 20 Schreibtische", False),
    ("Ihre Angebotsanfrage über 20 Schreibtische", False),
    ("die angefragten 20 Schreibtische", False),
    # Schwellen, Masse, Zeiten, Einheiten, Nummern
    ("ab 20 Stück gewähren wir 5 % Rabatt", False),
    ("mindestens 20 Stück je Bestellung", False),
    ("Mindestabnahme 20 Stück", False),
    ("Mindestmenge: 20 Stück", False),
    ("bei mehr als 20 Stück Rabatt", False),
    ("Tischplatte 20 x 20 cm Kabelkanal", False),
    ("Tischplatte 160 x 20 cm", False),
    ("Lieferung in 20 Werktagen", False),
    ("Lieferzeit 20 Tage nach Auftrag", False),
    ("Lieferung in KW 20 frei Haus", False),
    ("Skonto 2% bei Zahlung in 20 Tg.", False),
    ("Preis 20 Euro pro Stück netto", False),
    ("Seite 20 von 30 Schreibtische", False),
    ("Lieferung erfolgt in 20 Teillieferungen", False),
    ("Stromkabel 20 lfm inklusive", False),
    ("Pos. 20 Schreibtisch Montage", False),
    ("Garantie 20 Monate auf alle Teile", False),
    ("Kabel 20 m Länge inklusive", False),
    ("Gewicht 20 kg pro Tisch", False),
    ("Menge: 20 cm Kabel inkl.", False),
    ("20 Tische", False),  # zu kurz ohne Beschriftung
]


def main():
    print("== Mengenbelege (20) ==")
    for q, want in QUANTITY_CASES:
        check(q, _is_quantity_quote(q, D(20)), want)
    text = "Gemäß Ihrer Anfrage bieten wir 20 Schreibtische an.\nMenge: 20\nGesamt 13.832,00 EUR"
    check("verify_priced_quantity 'Menge: 20'",
          verify_priced_quantity({"priced_quantity": 20, "priced_quantity_quote": "Menge: 20"}, text) is not None, True)

    print("== Zahlen ==")
    check("_dec('0.125')", _dec("0.125"), D("0.125"))
    check("_dec('1.580,00')", _dec("1.580,00"), D("1580.00"))
    check("_dec_de('1.580')", _dec_de("1.580"), D("1580"))
    check("_dec_de('0.125')", _dec_de("0.125"), D("0.125"))
    check("_amounts_in", _amounts_in("Memory-Bedienteil optional, gesamt 1.580 EUR"), [D("1580")])
    t = "Gesamtpreis netto 13.832,00 EUR fuer 20 Schreibtische"
    check("Gesamtpreis als Text '13.832', im Dokument 13.832,00",
          reconcile_prices({"total_price": "13.832", "unit_price": None, "quantity": None}, t)["total_price"], D("13832"))
    r = reconcile_prices({"total_price": "13.832", "unit_price": None, "quantity": None}, "Gesamtpreis 13,832 Tsd.")
    check("Gesamtpreis '13.832' ohne deutschen Beleg -> noch nicht ermittelt", r["total_price"], None)
    check("... mit Hinweis", any("nicht eindeutig" in n for n in r["price_notes"]), True)
    check("Gesamtpreis als Zahl", reconcile_prices({"total_price": 13832, "unit_price": None, "quantity": None}, t)["total_price"],
          D("13832"))

    print("== Optionen ==")
    t2 = ("Montage ohne Aufpreis enthalten: 1.200,00 EUR\nMemory-Bedienteil (abwählbar), gesamt 1.580 EUR\n"
          "Lieferung ohne zusätzliche Kosten\nZusätzlich: Kabelwanne 200,00 EUR\nGesamtpreis 13.832,00 EUR")
    f, v = _optional_items([
        {"description": "Montage", "price": 1200, "included_in_total": True, "quote": "Montage ohne Aufpreis enthalten: 1.200,00 EUR"},
        {"description": "Memory", "price": "1.580", "included_in_total": True, "quote": "Memory-Bedienteil (abwählbar), gesamt 1.580 EUR"},
        {"description": "Lieferung", "price": None, "included_in_total": True, "quote": "Lieferung ohne zusätzliche Kosten"},
        {"description": "Kabelwanne", "price": 200, "included_in_total": False, "quote": "Zusätzlich: Kabelwanne 200,00 EUR"},
    ], t2, D("13832"))
    check("'ohne Aufpreis' ist keine Option", "regulaere Position gewertet: Montage" in f[0]["message"], True)
    check("Optionspreis '1.580' belegt", v[0]["price"], "1580")
    check("'ohne zusätzliche Kosten' ist keine Option", "regulaere Position gewertet: Lieferung" in f[2]["message"], True)
    check("'Zusätzlich' = nicht im Preis", v[1]["included_in_total"], False)
    check("Angebotspreis bleibt Dokumentpreis", reconcile_prices(
        {"total_price": "13.832,00", "unit_price": None, "quantity": None,
         "optional_items": [{"description": "Memory", "price": "1.580", "included_in_total": True,
                             "quote": "Memory-Bedienteil (abwählbar), gesamt 1.580 EUR"}]}, t2)["total_price"], D("13832.00"))

    print("== erneute Pruefung behaelt Hinweise ==")
    rv = types.SimpleNamespace(findings_json=[{"code": "x"}])
    add_extraction_notes(rv, {"price_notes": ["n"], "optional_notes": [{"code": "optional_item"}], "channel": "E-Mail"})
    check("Hinweise angehaengt", [x["code"] for x in rv.findings_json], ["x", "price_extraction", "optional_item"])
    rv2 = types.SimpleNamespace(findings_json=[])
    add_extraction_notes(rv2, None)
    check("ohne gespeicherte Hinweise unveraendert", rv2.findings_json, [])

    print(f"\n{FAILS} FEHLER" if FAILS else "\nALLE FAELLE BESTANDEN")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
