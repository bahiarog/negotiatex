"""
Gegenprobe zur Fachabnahme: kann die Auswertung ueberhaupt scheitern, und
fangen die Pruefregeln erfundene Werte ab? Ersetzt die KI-Extraktion durch
eine Attrappe, die aus der Wahrheit ableitet und gezielt Fehler einbaut --
ohne KI-Aufrufe.

Eingebaute Fehler und erwartetes Verhalten (Regelwerk v2):
  - falscher Betrag bei korrektem Rohwert   -> Rueckfrage (amount_mismatch), kein stiller Fehler
  - erfundene 8 Stunden ohne Beleg           -> Rueckfrage (hours_unverified), keine Stundenumrechnung
  - erfundene Steuerbasis "netto"            -> Rueckfrage (tax_unverified)
  - Summenzeile als Position                 -> als ueberzaehlige Position erkannt

Ausfuehren:
  docker exec -w /app -e PYTHONPATH=/app negotiatex-backend python tests/gold/mutation_check.py
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import evaluate_extraction as ev  # noqa: E402

TAX_LINE = re.compile(r"netto|brutto|\bnet\b|exkl|zzgl|inkl", re.I)


def fake_extract(text, doc_type, must):
    doc = next(d for d in ev.build_docs() if d.agency.split(" (")[0] in text)
    tax_line = next((l for l in doc.intro if TAX_LINE.search(l)), None)
    hours_header = next((c for c in doc.columns if "Std" in c), None)
    items = []
    for i, t in enumerate(doc.truth):
        amount, hours, hours_ev, tax = t.amount, (str(t.hours) if t.hours else None), hours_header if t.hours else None, t.tax
        if doc.doc_id == "T02-A" and i == 0:
            amount = t.amount + 10
        if doc.doc_id == "T06-A" and i == 0:
            hours, hours_ev = "8", None
        if doc.doc_id == "T05-A" and i == 0:
            tax = "net"
        items.append({"role_or_item": t.role, "seniority": t.seniority, "region": t.region, "amount": str(amount),
                      "amount_raw": t.amount_doc, "currency": t.currency, "unit": t.unit_doc, "tax_basis": tax,
                      "tax_evidence": tax_line if t.tax != "unknown" else None,
                      "tax_rate_pct": str(t.rate) if t.rate else None, "billable_hours_per_day": hours,
                      "hours_evidence": hours_ev, "valid_to": t.valid_to,
                      "source_evidence": f"{t.role} {t.unit_doc} {t.hours or ''} {t.amount_doc}", "ancillary_costs": {}})
    if doc.doc_id == "T11-A":
        items.append({"role_or_item": "Gesamt netto", "amount": "9999", "amount_raw": "9999", "currency": "EUR",
                      "unit": "Pauschal", "tax_basis": "net", "source_evidence": "Gesamt netto"})
    return items, {"input_tokens": 0, "output_tokens": 0}


def main():
    real_report = Path("/tmp/mdc_gold_report.json")
    backup = real_report.read_text() if real_report.exists() else None
    ev.extract_line_items = fake_extract
    ev.main()
    r = json.loads(real_report.read_text())
    if backup is not None:
        real_report.write_text(backup)
    problems = "\n".join(r["problems"])
    expect = {
        "falscher Betrag erkannt": r["field_accuracy_pct"]["amount"] < 100,
        "falscher Betrag nicht still": "T02-A" in problems and "STILLER FEHLER" not in problems,
        "erfundene Stunden -> Rueckfrage": "hours_unverified" in problems and r["invented_hours"] == 0,
        "erfundene Steuerbasis -> Rueckfrage": r["expected_blocking_triggered_pct"] == 100.0,
        "Summenzeile als Erfindung erkannt": r["precision_pct"] < 100,
        "keine stillen Fehler": r["silent_errors"] == 0,
    }
    print("\nGEGENPROBE:", {k: ("ok" if v else "FEHLT") for k, v in expect.items()})
    print("GEGENPROBE", "BESTANDEN" if all(expect.values()) else "NICHT BESTANDEN")


if __name__ == "__main__":
    main()
