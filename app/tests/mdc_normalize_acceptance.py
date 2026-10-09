# Abnahmefaelle Normalisierung (Anleitung S. 9/18). Ausfuehren:
# docker exec -w /app -e PYTHONPATH=/app negotiatex-backend python tests/mdc_normalize_acceptance.py
from datetime import date
from decimal import Decimal
from services.mdc_extractor import normalize_and_check, parse_amount_raw

TODAY = date(2026, 10, 9)
BASE = {
    "role_or_item": "Editor", "original_amount": "880", "original_amount_raw": "880",
    "original_currency": "eur", "original_unit": "Tag", "tax_basis": "net",
    "tax_evidence": "Alle Preise netto zzgl. MwSt.",
    "source_evidence": "Zeile 3: Editor,Mid,Tag,8,880", "offer_date": "2026-09-01",
    "ancillary_costs_json": {k: {"status": "exclusive"} for k in ("fracht", "setup", "reise", "mindesthonorar")},
}


def run(name, overrides, check):
    r = normalize_and_check({**BASE, **overrides}, today=TODAY)
    ok = check(r)
    codes = [(i["code"], i["blocking"]) for i in r["open_issues_json"]]
    print(("PASS " if ok else "FAIL ") + name, "|", r["canonical_unit"], r["normalized_amount_per_canonical_unit"], codes)
    return ok


results = [
    run("Anleitungsbeispiel: 880/Tag bei 8 bestaetigten Stunden = 110/Stunde",
        {"billable_hours_per_day": "8", "hours_evidence": "Abrechenbare Stunden pro Tag"},
        lambda r: r["canonical_unit"] == "Stunde" and r["normalized_amount_per_canonical_unit"] == Decimal("110.0000") and not r["has_blocking_issues"]),
    run("Tagessatz ohne Stunden: keine Umrechnung, Luecke sichtbar, nicht blockierend",
        {},
        lambda r: r["canonical_unit"] == "Tag" and any(i["code"] == "day_rate_without_hours" for i in r["open_issues_json"]) and not r["has_blocking_issues"]),
    run("Steuerbasis unbekannt: blockiert",
        {"tax_basis": "unknown"},
        lambda r: r["has_blocking_issues"] and r["normalized_amount_net"] is None),
    run("Brutto ohne Steuersatz: blockiert",
        {"tax_basis": "gross", "tax_evidence": "Preise brutto."},
        lambda r: r["has_blocking_issues"] and r["normalized_amount_net"] is None),
    run("Brutto mit 19%: netto korrekt",
        {"tax_basis": "gross", "original_amount": "1190", "original_amount_raw": "1.190,00", "tax_rate_pct": "19",
         "tax_evidence": "Bruttopreise inkl. 19 % MwSt."},
        lambda r: r["normalized_amount_net"] == Decimal("1000.0000") and not r["has_blocking_issues"]),
    run("Mehrdeutiger Betrag 1.250: blockiert",
        {"original_amount": "1250", "original_amount_raw": "1.250"},
        lambda r: any(i["code"] == "ambiguous_amount" for i in r["open_issues_json"]) and r["has_blocking_issues"]),
    run("Mehrdeutiger Betrag vom Menschen bestaetigt: nicht mehr blockiert",
        {"original_amount": "1250", "original_amount_raw": "1.250", "amount_confirmed": True},
        lambda r: not r["has_blocking_issues"]),
    run("Extraktion weicht vom Rohwert ab: blockiert",
        {"original_amount": "88", "original_amount_raw": "880"},
        lambda r: any(i["code"] == "amount_mismatch" for i in r["open_issues_json"])),
    run("Abgelaufenes Angebot: nur historisch",
        {"valid_to": "2023-12-31"},
        lambda r: any(i["code"] == "expired" for i in r["open_issues_json"])),
    run("Nebenkosten unbekannt: Hinweis kein TCO",
        {"ancillary_costs_json": {}},
        lambda r: any(i["code"] == "ancillary_unknown" for i in r["open_issues_json"]) and not r["has_blocking_issues"]),
    run("Fehlende Belegstelle: blockiert",
        {"source_evidence": ""},
        lambda r: any(i["code"] == "missing_evidence" for i in r["open_issues_json"]) and r["has_blocking_issues"]),
    run("Stundensatz direkt",
        {"original_unit": "Std.", "original_amount": "150", "original_amount_raw": "150,00 EUR"},
        lambda r: r["canonical_unit"] == "Stunde" and r["normalized_amount_per_canonical_unit"] == Decimal("150.0000")),
    # v2: erfundene Werte ohne Beleg im Dokument
    run("Netto ohne Beleg im Dokument: blockiert, keine Rechnung",
        {"tax_evidence": None},
        lambda r: any(i["code"] == "tax_unverified" for i in r["open_issues_json"]) and r["normalized_amount_net"] is None),
    run("Beleg passt nicht zur Aussage (Reisekosten inklusive != brutto): blockiert",
        {"tax_basis": "gross", "tax_rate_pct": "19", "tax_evidence": "Reisekosten inklusive"},
        lambda r: any(i["code"] == "tax_unverified" for i in r["open_issues_json"])),
    run("Mensch hat Steuerbasis bestaetigt: kein Beleg noetig",
        {"tax_evidence": None, "tax_confirmed": True},
        lambda r: not r["has_blocking_issues"]),
    run("Erfundene 8 Stunden (kein Stunden-Beleg): blockiert, bleibt Tagessatz",
        {"billable_hours_per_day": "8", "hours_evidence": None},
        lambda r: any(i["code"] == "hours_unverified" for i in r["open_issues_json"]) and r["canonical_unit"] == "Tag"),
    run("Stunden-Beleg nennt andere Zahl als behauptet: blockiert",
        {"billable_hours_per_day": "9", "hours_evidence": "Tagessatz = 8 Stunden", "source_evidence": "Zeile 3: Editor,Mid,Tag,880"},
        lambda r: any(i["code"] == "hours_unverified" for i in r["open_issues_json"])),
    run("Stundenzahl vom Menschen bestaetigt: Umrechnung",
        {"billable_hours_per_day": "8", "hours_confirmed": True, "hours_evidence": None},
        lambda r: r["canonical_unit"] == "Stunde" and r["normalized_amount_per_canonical_unit"] == Decimal("110.0000")),
]

from services.mdc_extractor import verified_quote
TEXT = "Zeile 2: Alle Preise netto zzgl. MwSt.\nZeile 3: Editor,Mid,Tag,8,880"
for q, expected in [("Alle Preise  netto zzgl. MwSt.", True), ("Alle Preise netto zzgl. 19% MwSt.", False), ("x", False)]:
    ok = (verified_quote(q, TEXT) is not None) == expected
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + f"verified_quote({q!r}) im Dokument: {expected}")

for raw, expected in [("1.234,56", (Decimal("1234.56"), False)), ("1,234.56", (Decimal("1234.56"), False)),
                      ("650", (Decimal("650"), False)), ("12.500,00 EUR", (Decimal("12500.00"), False)),
                      ("1.250", (None, True))]:
    got = parse_amount_raw(raw)
    ok = got == expected
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + f"parse_amount_raw({raw!r}) -> {got}")

print(f"\n{sum(results)}/{len(results)} bestanden")
