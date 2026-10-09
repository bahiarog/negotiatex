"""
Fachabnahme der Preisextraktion gegen den Gold-Datensatz (gold_dataset.py).

Ruft Parser -> KI-Extraktion -> deterministische Pruefung direkt auf; es wird
nichts in einen Mandanten geschrieben (Dummy- und Kundendaten bleiben
getrennt). Kritisch ist nicht jeder Fehler -- jede Position wird ohnehin von
einem Menschen geprueft --, sondern eine FALSCHE Zahl OHNE blockierenden
Hinweis ("stiller Fehler"): die waere ohne Rueckfrage freigabefaehig.

Vorgeschlagene Abnahmeschwellen (fachlich zu bestaetigen):
  Positionen gefunden >= 95 %, keine erfundenen Positionen >= 95 %,
  Betrag korrekt >= 98 %, erwartete Rueckfragen 100 % ausgeloest,
  erfundene Stundenzahlen ohne Rueckfrage 0, stille Fehler 0.
Unnoetige Rueckfragen (Position korrekt, trotzdem blockiert) werden als
Effizienzkennzahl ausgewiesen, aber nicht als Abnahmekriterium gewertet.

Ausfuehren:
  docker exec -w /app -e PYTHONPATH=/app negotiatex-backend python tests/gold/evaluate_extraction.py
"""
import asyncio
import json
import sys
import tempfile
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from gold_dataset import build_docs, render  # noqa: E402
from services.mdc_extractor import extract_line_items, normalize_and_check, verified_quote, resolve_hours_evidence  # noqa: E402
from services.pdf_parser import extract_text  # noqa: E402

MUST = {"rolle": "z.B. Editor, Producer, DoP", "senioritaet": "Junior/Mid/Senior", "region": "z.B. DACH", "einheit": "Tag oder Stunde"}
THRESHOLDS = {"recall": 95.0, "precision": 95.0, "amount": 98.0, "expected_block": 100.0}


def _norm(s):
    return " ".join(str(s or "").lower().replace("-", " ").split())


def _dec(v):
    try:
        return Decimal(str(v)) if v not in (None, "") else None
    except Exception:
        return None


# Gleichwertige Rueckfragegruende: entscheidend ist, dass die Position nicht
# ohne Klaerung freigabefaehig ist.
EQUIV = {
    "tax_basis_unknown": {"tax_basis_unknown", "tax_unverified"},
    "gross_without_rate": {"gross_without_rate", "tax_unverified"},
    "ambiguous_amount": {"ambiguous_amount", "amount_mismatch"},
}


def _fields(item: dict, text: str) -> dict:
    """Wie der Router: Zitate zaehlen nur, wenn sie wirklich im Dokument stehen."""
    return {
        "role_or_item": item.get("role_or_item"), "original_amount": item.get("amount"),
        "original_amount_raw": item.get("amount_raw"), "amount_confirmed": False,
        "original_currency": item.get("currency"), "original_unit": item.get("unit"),
        "tax_basis": item.get("tax_basis") if item.get("tax_basis") in ("net", "gross", "unknown") else "unknown",
        "tax_evidence": verified_quote(item.get("tax_evidence"), text),
        "hours_evidence": resolve_hours_evidence(item.get("hours_evidence"), text) if item.get("billable_hours_per_day") not in (None, "") else None,
        "tax_rate_pct": item.get("tax_rate_pct"), "billable_hours_per_day": item.get("billable_hours_per_day"),
        "ancillary_costs_json": item.get("ancillary_costs") or {}, "source_evidence": item.get("source_evidence"),
        "offer_date": item.get("offer_date"), "valid_from": item.get("valid_from"), "valid_to": item.get("valid_to"),
    }


def _role_match(gold: str, got: str) -> bool:
    g, x = _norm(gold), _norm(got)
    return bool(g) and bool(x) and (g == x or g in x or x in g)


def _match(truth: list, items: list) -> list:
    pairs, used = [], set()
    for t in truth:
        best = None
        for j, it in enumerate(items):
            if j in used or not _role_match(t.role, it.get("role_or_item")):
                continue
            score = (_norm(t.seniority) == _norm(it.get("seniority"))) * 2 + (_dec(it.get("amount")) == t.amount)
            if best is None or score > best[0]:
                best = (score, j)
        if best is not None:
            used.add(best[1])
            pairs.append((t, items[best[1]]))
        else:
            pairs.append((t, None))
    extras = [it for j, it in enumerate(items) if j not in used]
    return pairs, extras


def _run_one(doc, out_dir: Path):
    path = render(doc, out_dir)
    text = asyncio.run(extract_text(str(path), path.name))
    t0 = time.monotonic()
    items, usage = extract_line_items(text, "ratecard", MUST)
    return doc, text, items, usage, time.monotonic() - t0


def main():
    docs = build_docs()
    out_dir = Path(tempfile.mkdtemp(prefix="mdc_gold_"))
    with ThreadPoolExecutor(max_workers=4) as pool:
        runs = list(pool.map(lambda d: _run_one(d, out_dir), docs))

    tot = Counter()
    by_case = defaultdict(Counter)
    by_fmt = defaultdict(Counter)
    problems = []
    tokens_in = tokens_out = 0
    seconds = []

    for doc, text, items, usage, secs in runs:
        tokens_in += usage.get("input_tokens") or 0
        tokens_out += usage.get("output_tokens") or 0
        seconds.append(secs)
        pairs, extras = _match(doc.truth, items)
        tot["gold"] += len(doc.truth)
        tot["extracted"] += len(items)
        tot["extra"] += len(extras)
        by_fmt[doc.fmt]["gold"] += len(doc.truth)
        for e in extras:
            problems.append(f"{doc.doc_id} ({doc.fmt}): erfundene/ueberzaehlige Position '{e.get('role_or_item')}' {e.get('amount')}")
        for t, it in pairs:
            cases = doc.hard_cases or ("Standard",)
            if it is None:
                tot["missed"] += 1
                for c in cases:
                    by_case[c]["missed"] += 1
                problems.append(f"{doc.doc_id} ({doc.fmt}): Position '{t.role}' nicht gefunden")
                continue
            tot["matched"] += 1
            by_fmt[doc.fmt]["matched"] += 1
            res = normalize_and_check(_fields(it, text))
            blocking = {i["code"] for i in res["open_issues_json"] if i["blocking"]}
            amount_ok = _dec(it.get("amount")) == t.amount
            unit_ok = res["canonical_unit"] == t.canonical
            tax_ok = (it.get("tax_basis") or "unknown") == t.tax
            hours_got = _dec(it.get("billable_hours_per_day"))
            hours_ok = hours_got == t.hours
            invented_hours = t.hours is None and hours_got is not None and "hours_unverified" not in blocking
            norm_ok = res["normalized_amount_per_canonical_unit"] == t.expected_per_unit
            exp_block_ok = all(blocking & EQUIV.get(code, {code}) for code in t.blocking)
            allowed = set().union(*(EQUIV.get(c, {c}) for c in t.blocking)) if t.blocking else set()
            unnecessary = blocking - allowed - {"missing_evidence"}
            if unnecessary:
                problems.append(f"{doc.doc_id} ({doc.fmt}) {t.role}: unnoetige Rueckfrage {sorted(unnecessary)}")
            valid_ok = t.valid_to is None or str(it.get("valid_to") or "")[:10] == t.valid_to
            cur_ok = (it.get("currency") or "").upper() == t.currency
            sen_ok = _norm(t.seniority) == _norm(it.get("seniority")) if t.seniority else True
            wrong_money = not amount_ok or (t.expected_per_unit is not None and not norm_ok) or invented_hours or not cur_ok
            silent = wrong_money and not blocking

            for key, ok in (("amount", amount_ok), ("currency", cur_ok), ("unit", unit_ok), ("tax", tax_ok), ("hours", hours_ok),
                            ("normalized", norm_ok), ("valid_to", valid_ok), ("seniority", sen_ok)):
                tot[f"{key}_ok"] += ok
                by_fmt[doc.fmt][f"{key}_ok"] += ok
            tot["invented_hours"] += invented_hours
            tot["silent"] += silent
            tot["unnecessary_block"] += bool(unnecessary)
            if t.blocking:
                tot["expected_block"] += 1
                tot["expected_block_ok"] += exp_block_ok
            for c in cases:
                by_case[c]["n"] += 1
                by_case[c]["amount_ok"] += amount_ok
                by_case[c]["normalized_ok"] += norm_ok
                by_case[c]["silent"] += silent
                if t.blocking:
                    by_case[c]["expected_block"] += 1
                    by_case[c]["expected_block_ok"] += exp_block_ok
            if not (amount_ok and norm_ok and exp_block_ok and cur_ok and not invented_hours and valid_ok):
                problems.append(
                    f"{doc.doc_id} ({doc.fmt}) {t.role}: Betrag {it.get('amount')} (soll {t.amount}), roh '{it.get('amount_raw')}', "
                    f"Steuer {it.get('tax_basis')} (soll {t.tax}), Std {hours_got} (soll {t.hours}), "
                    f"normalisiert {res['normalized_amount_per_canonical_unit']} {res['canonical_unit']} (soll {t.expected_per_unit} {t.canonical}), "
                    f"blockierend {sorted(blocking)} (erwartet {list(t.blocking)})" + (" -> STILLER FEHLER" if silent else ""))

    pct = lambda a, b: round(100 * a / b, 1) if b else 100.0
    m = tot["matched"]
    report = {
        "documents": len(docs), "gold_positions": tot["gold"], "extracted_positions": tot["extracted"],
        "recall_pct": pct(m, tot["gold"]), "precision_pct": pct(tot["extracted"] - tot["extra"], tot["extracted"]),
        "field_accuracy_pct": {k: pct(tot[f"{k}_ok"], m) for k in ("amount", "currency", "unit", "tax", "hours", "normalized", "valid_to", "seniority")},
        "expected_blocking_triggered_pct": pct(tot["expected_block_ok"], tot["expected_block"]),
        "invented_hours": tot["invented_hours"], "silent_errors": tot["silent"],
        "positions_with_unnecessary_question": tot["unnecessary_block"],
        "by_hard_case": {c: dict(v) for c, v in sorted(by_case.items())},
        "by_format": {f: {"recall_pct": pct(v["matched"], v["gold"]), "amount_pct": pct(v["amount_ok"], v["matched"])} for f, v in sorted(by_fmt.items())},
        "extraction": {"avg_seconds": round(sum(seconds) / len(seconds), 1), "avg_input_tokens": round(tokens_in / len(docs)),
                       "avg_output_tokens": round(tokens_out / len(docs))},
        "problems": problems,
    }
    checks = {
        "Positionen gefunden": report["recall_pct"] >= THRESHOLDS["recall"],
        "keine erfundenen Positionen": report["precision_pct"] >= THRESHOLDS["precision"],
        "Betrag korrekt": report["field_accuracy_pct"]["amount"] >= THRESHOLDS["amount"],
        "erwartete Rueckfragen ausgeloest": report["expected_blocking_triggered_pct"] >= THRESHOLDS["expected_block"],
        "keine erfundenen Stundenzahlen": report["invented_hours"] == 0,
        "keine stillen Fehler": report["silent_errors"] == 0,
    }
    report["acceptance"] = {k: ("bestanden" if v else "NICHT bestanden") for k, v in checks.items()}
    report["passed"] = all(checks.values())
    Path("/tmp/mdc_gold_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))

    print(f"Gold-Datensatz: {report['documents']} Dokumente, {report['gold_positions']} Positionen, {report['extracted_positions']} extrahiert")
    print(f"Positionen gefunden {report['recall_pct']} %, ohne Erfindungen {report['precision_pct']} %")
    print("Feldgenauigkeit:", ", ".join(f"{k} {v} %" for k, v in report["field_accuracy_pct"].items()))
    print(f"Erwartete Rueckfragen ausgeloest {report['expected_blocking_triggered_pct']} %, erfundene Stunden {report['invented_hours']}, "
          f"stille Fehler {report['silent_errors']}, unnoetige Rueckfragen {report['positions_with_unnecessary_question']}")
    print("Je Format:", report["by_format"])
    print("Je Schwierigkeitsfall:")
    for c, v in report["by_hard_case"].items():
        print(f"  {c:24s} {dict(v)}")
    print(f"Extraktion: {report['extraction']}")
    print("Abnahme:", report["acceptance"], "->", "BESTANDEN" if report["passed"] else "NICHT BESTANDEN")
    if problems:
        print(f"\n{len(problems)} Auffaelligkeiten:")
        for p in problems:
            print("  -", p)


if __name__ == "__main__":
    main()
