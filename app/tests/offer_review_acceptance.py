"""Akzeptanztest NX-034: AGB-/Klauselpruefung belastbar.

Ohne Datenbank und ohne echten KI-Aufruf (anthropic wird durch eine Attrappe ersetzt).
Aufruf aus dem Repo-Wurzelverzeichnis:
    python app/tests/offer_review_acceptance.py
"""
import json
import os
import sys
import types
from datetime import date, timedelta
from decimal import Decimal as D

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
if "database" not in sys.modules:
    from sqlalchemy.orm import DeclarativeBase
    _db = types.ModuleType("database")

    class _Base(DeclarativeBase):
        pass
    _db.Base = _Base
    sys.modules["database"] = _db

import services.offer_compliance as oc  # noqa: E402
from services.offer_intake import compose_review_text, _attachment_rank, _looks_like_terms  # noqa: E402

FAILS = 0
CALLS = []


def check(name, got, want):
    global FAILS
    ok = got == want
    FAILS += not ok
    print(("ok   " if ok else "FEHL ") + name + " -> " + repr(got) + ("" if ok else f"  (erwartet {want!r})"))


class FakeClient:
    """Liefert je Aufruf die naechste Antwort aus RESPONSES (Text) oder wirft, wenn sie eine Exception ist."""
    def __init__(self, *a, **k):
        self.messages = self

    def create(self, **kw):
        CALLS.append(kw["messages"][0]["content"])
        r = RESPONSES.pop(0) if RESPONSES else json.dumps({"findings": []})
        if isinstance(r, Exception):
            raise r
        return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=r)])


RESPONSES: list = []
oc.anthropic = types.SimpleNamespace(Anthropic=FakeClient)


def offer(**kw):
    base = dict(quantity=D(1), unit_price=D(1000), freight_cost=0, other_costs=0, currency="EUR",
                offer_validity_until=date.today() + timedelta(days=30), delivery_date=None, payment_terms=None,
                spec_confirmed=True, comparability_flag="comparable", comparability_note=None)
    base.update(kw)
    return types.SimpleNamespace(**base)


PROJECT = types.SimpleNamespace(quantity=None, budget_ceiling=None, budget_target=None, needed_by=None, conditions_text=None,
                                service_type="Video", description=None, must_criteria_json=None)
TEXT = "Angebot Imagefilm. Die Haftung ist auf den Auftragswert begrenzt. Gesamtpreis 1.000,00 EUR"
HAFTUNG = json.dumps({"findings": [{"topic": "Haftung", "severity": "warning", "message": "Haftung begrenzt",
                                    "quote": "Die Haftung ist auf den Auftragswert begrenzt."}]})


def main():
    global RESPONSES
    print("== Status ==")
    RESPONSES = [HAFTUNG]
    r = oc.review(offer(), PROJECT, TEXT)
    check("normale Pruefung mit Klausel -> warning", r["status"], "warning")
    check("Regelversion v2", r["rules_version"], "offer-review-v2")

    RESPONSES = [json.dumps({"findings": []})]
    check("ohne Auffaelligkeiten -> ok", oc.review(offer(), PROJECT, TEXT)["status"], "ok")

    RESPONSES = [RuntimeError("API down")]
    r = oc.review(offer(), PROJECT, TEXT)
    check("KI-Fehler -> not_checked (nie ok)", r["status"], "not_checked")
    check("... Hinweis sichtbar (warning)", [f["severity"] for f in r["findings"] if f["code"] == "clause_review_failed"], ["warning"])

    RESPONSES = ["kein json"]
    check("unbrauchbare KI-Antwort -> not_checked", oc.review(offer(), PROJECT, TEXT)["status"], "not_checked")

    RESPONSES = [json.dumps({"findings": None})]
    check("findings=null -> not_checked", oc.review(offer(), PROJECT, TEXT)["status"], "not_checked")

    check("kein lesbarer Text -> not_checked", oc.review(offer(), PROJECT, "")["status"], "not_checked")

    RESPONSES = [RuntimeError("API down")]
    r = oc.review(offer(offer_validity_until=date.today() - timedelta(days=1)), PROJECT, TEXT)
    check("KI-Fehler + kritischer Regelbefund -> critical bleibt", r["status"], "critical")
    check("... Hinweis auf fehlende Klauselpruefung dabei", any(f["code"] == "clause_review_failed" for f in r["findings"]), True)

    print("== Lange Angebote ==")
    CALLS.clear()
    long_text = ("Allgemeine Bedingungen. " * 2500) + "Die Haftung ist auf den Auftragswert begrenzt."  # ~60.000 Zeichen
    RESPONSES = [json.dumps({"findings": []}), json.dumps({"findings": []}), HAFTUNG, json.dumps({"findings": []})]
    r = oc.review(offer(), PROJECT, long_text)
    check("60.000 Zeichen -> alle Abschnitte geprueft", len(CALLS), 4)
    check("... Klausel am Ende gefunden", any(f.get("topic") == "Haftung" for f in r["findings"]), True)
    check("... Status warning (vollstaendig geprueft)", r["status"], "warning")
    CALLS.clear()
    RESPONSES = []
    r = oc.review(offer(), PROJECT, "x" * 120000)
    check("120.000 Zeichen -> hoechstens 4 KI-Aufrufe", len(CALLS), 4)
    check("... Hinweis auf Kuerzung", any(f["code"] == "clause_review_truncated" for f in r["findings"]), True)
    check("... Status not_checked", r["status"], "not_checked")

    print("== Anhaenge ==")
    text, notes = compose_review_text("Angebot", [{"name": "AGB.pdf", "text": "Die Haftung ist ausgeschlossen."},
                                                  {"name": "Scan.pdf", "text": ""}])
    check("AGB-Text im Pruefungstext", "--- Anhang: AGB.pdf ---\nDie Haftung ist ausgeschlossen." in text, True)
    check("unlesbarer Anhang -> Hinweis", [n["code"] for n in notes], ["attachment_unreadable"])
    RESPONSES = [json.dumps({"findings": [{"topic": "Haftung", "severity": "critical", "message": "Haftung ausgeschlossen",
                                           "quote": "Die Haftung ist ausgeschlossen."}]})]
    r = oc.review(offer(), PROJECT, text, notes)
    check("Klausel aus AGB-Anhang belegt und gefunden", any(f.get("quote") == "Die Haftung ist ausgeschlossen." for f in r["findings"]), True)
    RESPONSES = [json.dumps({"findings": []})]
    check("unlesbarer Anhang -> not_checked", oc.review(offer(), PROJECT, text, notes)["status"], "not_checked")
    names = ["AGB_Filmwerk.pdf", "Anlage.pdf", "Angebot_2026-117.pdf"]
    check("Angebot wird als Hauptdokument gewaehlt",
          sorted([{"filename": n} for n in names], key=_attachment_rank)[0]["filename"], "Angebot_2026-117.pdf")
    check("Angebotsbedingungen.pdf gilt als Bedingungen", _looks_like_terms("Angebotsbedingungen.pdf"), True)
    check("ohne Angebotsnamen: Reihenfolge bleibt", sorted([{"filename": "a.pdf"}, {"filename": "b.pdf"}], key=_attachment_rank)[0]["filename"], "a.pdf")

    print(f"\n{FAILS} FEHLER" if FAILS else "\nALLE FAELLE BESTANDEN")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
