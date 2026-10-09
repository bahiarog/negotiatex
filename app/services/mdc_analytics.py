"""
Master Data Center Etappe 3 -- deterministischer Angebotsvergleich.

Reine Funktionen ohne DB- und ohne LLM-Zugriff: gleiche Eingaben liefern
immer dieselben Zahlen (Abnahmetest "Wiederholung derselben Analyse").

Ablauf (Anleitung Abschnitte 09/10):
  1. Harte Filter -- jede nicht passende Referenz wird mit Ausschlussgrund
     gefuehrt, nie stillschweigend weggelassen. Semantische Naehe gibt keine
     Preisposition frei: Rolle, Senioritaet, Region, Einheit und Waehrung
     muessen belegt uebereinstimmen.
  2. Aktualitaet -- abgelaufene oder zu alte Preise wandern in eine
     getrennte historische Liste, nicht in die aktuelle Kohorte.
  3. Ein Projekt (= Dokument) je Lieferant traegt hoechstens eine
     Beobachtung bei.
  4. Listen-, Angebots-, Verhandlungs-, Vertrags- und Rechnungspreise
     werden getrennt ausgewertet, nie zu einer Verteilung vermischt.
  5. Unterhalb der Benchmark-Schwelle nur deskriptive Einzelreferenzen --
     kein Marktpreis.

Die Erklaerung (explain) wird bewusst als Textbaustein aus den berechneten
Werten erzeugt, nicht von einem Sprachmodell: so kann sie keine Zahl
enthalten, die der Service nicht berechnet hat.
"""
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

POLICIES = {
    "RATECARD-PILOT-v1": {
        "policy_id": "RATECARD-PILOT-v1",
        "version": "1",
        "freshness_days": 90,
        "benchmark_min_projects": 10,
        "benchmark_min_suppliers": 5,
        "median_min_n": 3,
        "quartile_min_n": 5,
        "quantile_method": "lineare Interpolation (Hyndman-Fan Typ 7)",
        "outlier_rule": "1,5 x IQR, nur markiert, nie entfernt",
    },
}
DEFAULT_POLICY_ID = "RATECARD-PILOT-v1"

CLASS_LABELS = {
    "list_price": "Listenpreise", "quoted": "Angebotspreise", "negotiated_quote": "verhandelte Angebote",
    "contracted": "Vertragspreise", "invoiced": "abgerechnete Preise",
}
CLASS_ORDER = ["quoted", "negotiated_quote", "contracted", "invoiced", "list_price"]


def _norm(s: Optional[str]) -> str:
    return " ".join((s or "").lower().split())


def _d(v) -> Optional[Decimal]:
    if v is None:
        return None
    return v if isinstance(v, Decimal) else Decimal(str(v))


def _date(v) -> Optional[date]:
    if v is None or isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _pct(numerator: Decimal, denominator: Decimal) -> Decimal:
    return (numerator / denominator * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _q2(v: Decimal) -> Decimal:
    return v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def quantile(sorted_values: list[Decimal], p: Decimal) -> Decimal:
    """Typ 7 (lineare Interpolation), wie R-Default / numpy 'linear'."""
    n = len(sorted_values)
    h = (n - 1) * p
    lo = int(h)
    frac = h - lo
    if lo + 1 >= n:
        return sorted_values[-1]
    return sorted_values[lo] + frac * (sorted_values[lo + 1] - sorted_values[lo])


ANCILLARY_KEYS = ("fracht", "setup", "reise", "mindesthonorar")


def _unknown_costs(item: dict) -> list[str]:
    """Fehlende Angabe zaehlt als unbekannt -- nie als 'inklusive'."""
    costs = item.get("ancillary_costs_json") or {}
    return [k for k in ANCILLARY_KEYS if (costs.get(k) or {}).get("status", "unknown") == "unknown"]


def _target_gaps(target: dict) -> list[str]:
    gaps = []
    if not target.get("category_id"):
        gaps.append("Angebot ist keiner Kategorie zugeordnet.")
    if not _norm(target.get("role_or_item")):
        gaps.append("Rolle bzw. Leistung des Angebots fehlt.")
    for issue in target.get("open_issues_json") or []:
        if issue.get("blocking"):
            gaps.append(issue.get("message"))
    if target.get("normalized_amount_per_canonical_unit") is None and not any("Steuer" in g or "Brutto" in g for g in gaps):
        gaps.append("Kein normalisierter Nettopreis je Einheit berechenbar.")
    return gaps


def _evaluate_candidate(target: dict, c: dict, as_of: date, policy: dict) -> tuple[Optional[str], Optional[str]]:
    """Rueckgabe (ausschlussgrund, historisch_grund). Genau eins davon oder
    keins ist gesetzt."""
    if c.get("category_id") != target.get("category_id"):
        return "Andere Kategorie.", None
    if c.get("supplier_id") is None:
        return "Lieferant nicht belegt -- Unabhaengigkeit der Beobachtung nicht pruefbar.", None
    if _norm(c.get("role_or_item")) != _norm(target.get("role_or_item")):
        return f"Andere Rolle/Leistung ('{c.get('role_or_item')}').", None
    for field, label in (("seniority", "Senioritaet"), ("region", "Region")):
        tv, cv = _norm(target.get(field)), _norm(c.get(field))
        if not tv or not cv:
            return f"{label} nicht belegt -- Vergleichbarkeit nicht bestaetigt.", None
        if tv != cv:
            return f"Andere {label} ('{c.get(field)}' statt '{target.get(field)}').", None
    if c.get("canonical_unit") != target.get("canonical_unit"):
        if c.get("canonical_unit") == "Tag" and target.get("canonical_unit") == "Stunde":
            return "Tagessatz ohne bestaetigte Stundenzahl -- nicht in der Stunden-Kohorte.", None
        return f"Andere Einheit ('{c.get('canonical_unit')}' statt '{target.get('canonical_unit')}').", None
    if (c.get("original_currency") or "") != (target.get("original_currency") or ""):
        return "Andere Waehrung -- kein dokumentierter Umrechnungskurs.", None
    if c.get("normalized_amount_per_canonical_unit") is None:
        return "Kein normalisierter Nettowert (Steuerbasis oder Einheit offen).", None

    valid_from, valid_to = _date(c.get("valid_from")), _date(c.get("valid_to"))
    ref_date = valid_from or _date(c.get("offer_date"))
    if valid_to and valid_to < as_of:
        return None, f"Gueltigkeit endete am {valid_to.isoformat()}."
    if ref_date is None:
        return "Angebots-/Gueltigkeitsdatum unbekannt -- Aktualitaet nicht belegt.", None
    if ref_date > as_of:
        return f"Datum ({ref_date.isoformat()}) liegt nach dem Stichtag.", None
    currently_valid = valid_from is not None and valid_to is not None and valid_from <= as_of <= valid_to
    age = (as_of - ref_date).days
    if not currently_valid and age > policy["freshness_days"]:
        return None, f"Aelter als {policy['freshness_days']} Tage ({ref_date.isoformat()})."
    return None, None


def _ref_entry(c: dict, target_value: Decimal) -> dict:
    value = _d(c["normalized_amount_per_canonical_unit"])
    return {
        "line_item_id": c["id"], "document_id": c["document_id"], "document_title": c.get("document_title"),
        "version_number": c.get("version_number"), "supplier_id": c["supplier_id"], "supplier_name": c.get("supplier_name"),
        "price_status": c["price_status"], "value": str(_q2(value)),
        "offer_date": c.get("offer_date"), "valid_from": c.get("valid_from"), "valid_to": c.get("valid_to"),
        "gap_vs_reference_pct": str(_pct(target_value - value, value)),
        "source_evidence": c.get("source_evidence"),
    }


def compare(target: dict, candidates: list[dict], as_of: date, policy_id: str = DEFAULT_POLICY_ID) -> dict:
    policy = POLICIES[policy_id]
    base = {
        "policy": policy, "as_of": as_of.isoformat(),
        "target": {
            "line_item_id": target["id"], "role_or_item": target.get("role_or_item"),
            "seniority": target.get("seniority"), "region": target.get("region"),
            "canonical_unit": target.get("canonical_unit"), "currency": target.get("original_currency"),
            "value": str(_q2(_d(target["normalized_amount_per_canonical_unit"]))) if target.get("normalized_amount_per_canonical_unit") is not None else None,
        },
    }

    gaps = _target_gaps(target)
    if gaps:
        return {**base, "status": "missing_data", "missing_data": gaps, "classes": [], "primary_class": None,
                "excluded": [], "historical": [], "tco": {"status": "not_comparable", "reasons": gaps}}

    target_value = _d(target["normalized_amount_per_canonical_unit"])
    excluded, historical, included = [], [], []
    for c in sorted(candidates, key=lambda x: str(x["id"])):
        if c["id"] == target["id"] or c.get("document_id") == target.get("document_id"):
            continue
        reason, hist = _evaluate_candidate(target, c, as_of, policy)
        if reason:
            excluded.append({"line_item_id": c["id"], "document_title": c.get("document_title"),
                             "supplier_name": c.get("supplier_name"), "role_or_item": c.get("role_or_item"),
                             "reason": reason})
        elif hist:
            entry = _ref_entry(c, target_value)
            entry["historical_reason"] = hist
            historical.append(entry)
        else:
            included.append(c)

    # Ein Projekt (Dokument) je Lieferant: hoechstens eine Beobachtung.
    by_project: dict = {}
    for c in included:
        key = (c["supplier_id"], c["document_id"])
        rank = (str(_date(c.get("valid_from")) or _date(c.get("offer_date")) or ""), str(c.get("created_at") or ""), str(c["id"]))
        if key not in by_project or rank > by_project[key][0]:
            if key in by_project:
                excluded.append({"line_item_id": by_project[key][1]["id"], "document_title": by_project[key][1].get("document_title"),
                                 "supplier_name": by_project[key][1].get("supplier_name"), "role_or_item": by_project[key][1].get("role_or_item"),
                                 "reason": "Weitere Beobachtung desselben Projekts -- nur eine je Projekt zaehlt."})
            by_project[key] = (rank, c)
        else:
            excluded.append({"line_item_id": c["id"], "document_title": c.get("document_title"), "supplier_name": c.get("supplier_name"),
                             "role_or_item": c.get("role_or_item"), "reason": "Weitere Beobachtung desselben Projekts -- nur eine je Projekt zaehlt."})
    cohort = [v[1] for v in by_project.values()]

    classes = []
    for cls in CLASS_ORDER:
        members = [c for c in cohort if c["price_status"] == cls]
        if not members:
            continue
        refs = sorted((_ref_entry(c, target_value) for c in members), key=lambda r: (Decimal(r["value"]), r["line_item_id"]))
        values = [Decimal(r["value"]) for r in refs]
        n = len(values)
        projects = len({r["document_id"] for r in refs})
        suppliers = len({r["supplier_id"] for r in refs})
        stats: dict = {"n": n, "projects": projects, "suppliers": suppliers,
                       "min": str(values[0]), "max": str(values[-1]),
                       "median": None, "q1": None, "q3": None,
                       "premium_vs_median_pct": None, "hypothetical_reduction_pct": None,
                       "outlier_line_item_ids": [], "dominant_supplier": None}
        if n >= policy["median_min_n"]:
            median = _q2(quantile(values, Decimal("0.5")))
            stats["median"] = str(median)
            stats["premium_vs_median_pct"] = str(_pct(target_value - median, median))
            stats["hypothetical_reduction_pct"] = str(_pct(target_value - median, target_value))
        if n >= policy["quartile_min_n"]:
            q1, q3 = quantile(values, Decimal("0.25")), quantile(values, Decimal("0.75"))
            stats["q1"], stats["q3"] = str(_q2(q1)), str(_q2(q3))
            iqr = q3 - q1
            lo, hi = q1 - Decimal("1.5") * iqr, q3 + Decimal("1.5") * iqr
            stats["outlier_line_item_ids"] = [r["line_item_id"] for r in refs if not (lo <= Decimal(r["value"]) <= hi)]
        if n >= 2:
            counts: dict = {}
            for r in refs:
                counts[r["supplier_id"]] = counts.get(r["supplier_id"], 0) + 1
            top_supplier, top_count = max(counts.items(), key=lambda kv: (kv[1], str(kv[0])))
            if top_count / n > 0.5:
                name = next(r["supplier_name"] for r in refs if r["supplier_id"] == top_supplier)
                stats["dominant_supplier"] = {"supplier_id": top_supplier, "supplier_name": name, "share_pct": str(_pct(Decimal(top_count), Decimal(n)))}
        if projects >= policy["benchmark_min_projects"] and suppliers >= policy["benchmark_min_suppliers"]:
            class_status = "benchmark_eligible"
        else:
            class_status = "insufficient_evidence"
        classes.append({
            "reference_class": cls, "label": CLASS_LABELS[cls], "status": class_status,
            "comparison_status": "comparable_for_descriptive_review", "stats": stats, "references": refs,
        })

    primary = None
    if classes:
        primary = max(classes, key=lambda c: (c["status"] == "benchmark_eligible", c["stats"]["projects"], -CLASS_ORDER.index(c["reference_class"])))["reference_class"]

    if not classes:
        status = "no_comparable_references"
    elif any(c["status"] == "benchmark_eligible" for c in classes):
        status = "benchmark_eligible"
    else:
        status = "insufficient_evidence"

    tco_reasons = []
    unknown_target = _unknown_costs(target)
    if unknown_target:
        tco_reasons.append(f"Nebenkosten des Angebots unbekannt: {', '.join(unknown_target)}.")
    refs_unknown = sum(1 for c in cohort if _unknown_costs(c))
    if refs_unknown:
        tco_reasons.append(f"{refs_unknown} Referenz(en) mit unbekannten Nebenkosten.")
    tco = {"status": "not_comparable", "reasons": tco_reasons} if tco_reasons else {
        "status": "not_computed", "reasons": ["Gesamtkostenvergleich ist in Regelwerk v1 noch nicht implementiert -- nur Einheitspreis verglichen."]}

    return {**base, "status": status, "missing_data": [], "classes": classes, "primary_class": primary,
            "excluded": excluded, "historical": historical, "tco": tco}


def _de(v) -> str:
    return f"{Decimal(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _fmt(v: Optional[str], cur: Optional[str]) -> str:
    if v is None:
        return "—"
    return f"{_de(v)} {cur or ''}".strip()


def explain(result: dict) -> str:
    t = result["target"]
    unit = t.get("canonical_unit") or "Einheit"
    cur = t.get("currency")
    who = ", ".join(x for x in (t.get("role_or_item"), t.get("seniority"), t.get("region")) if x)
    lines = []

    if result["status"] == "missing_data":
        lines.append(f"Kein Vergleich moeglich fuer {who or 'dieses Angebot'}: " + " ".join(result["missing_data"]))
        lines.append("Bitte die offenen Angaben am Angebot klaeren (Rueckfrage beim Lieferanten oder Pruefung im Data Center).")
        return "\n".join(lines)

    lines.append(f"Angebot: {_fmt(t['value'], cur)} netto pro {unit} ({who}). Stichtag {result['as_of']}, Regelwerk {result['policy']['policy_id']}.")

    if result["status"] == "no_comparable_references":
        lines.append("Im geprueften Bestand gibt es keine vergleichbare, aktuelle Referenz.")
    for c in result["classes"]:
        s = c["stats"]
        base = f"{c['label']}: {s['n']} vergleichbare Referenz(en) aus {s['projects']} Projekt(en) von {s['suppliers']} Lieferant(en)"
        if s["median"] is not None:
            direction = "ueber" if Decimal(s["premium_vs_median_pct"]) > 0 else ("unter" if Decimal(s["premium_vs_median_pct"]) < 0 else "auf")
            lines.append(f"{base}, Spanne {_fmt(s['min'], cur)} bis {_fmt(s['max'], cur)}, Median {_fmt(s['median'], cur)}. "
                         f"Das Angebot liegt {_de(abs(Decimal(s['premium_vs_median_pct'])))} % {direction} diesem Median.")
        else:
            singles = "; ".join(f"{_fmt(r['value'], cur)} ({r.get('supplier_name') or 'Lieferant'})" for r in c["references"])
            lines.append(f"{base} -- zu wenige fuer einen Median, nur Einzelreferenzen: {singles}.")
        values = [Decimal(r["value"]) for r in c["references"]]
        subject = "Die Referenz liegt" if len(values) == 1 else f"Alle {len(values)} Referenzen dieser Klasse liegen"
        if values and all(v < Decimal(t["value"]) for v in values):
            lines.append(f"{subject} unter dem Angebot.")
        elif values and all(v > Decimal(t["value"]) for v in values):
            lines.append(f"{subject} ueber dem Angebot.")
        if c["status"] != "benchmark_eligible":
            p = result["policy"]
            lines.append(f"Die Datenbasis ist klein (unter {p['benchmark_min_projects']} Projekten bzw. {p['benchmark_min_suppliers']} Lieferanten) -- "
                         "das ist ein deskriptiver Vergleich, kein repraesentativer Marktpreis.")
        if s.get("dominant_supplier"):
            lines.append(f"Hinweis: {s['dominant_supplier']['supplier_name']} stellt {_de(s['dominant_supplier']['share_pct'])} % der Beobachtungen.")

    if result["historical"]:
        lines.append(f"{len(result['historical'])} weitere Referenz(en) nur historisch (abgelaufen oder zu alt), nicht im aktuellen Vergleich.")
    if result["excluded"]:
        lines.append(f"{len(result['excluded'])} Position(en) ausgeschlossen -- Gruende siehe Ausschlussliste.")
    lines.append("Gesamtkosten: " + " ".join(result["tco"]["reasons"]))
    if result["status"] != "benchmark_eligible":
        lines.append("Empfehlung: weitere Vergleichsangebote einholen, bevor ein Zielpreis festgelegt wird.")
    lines.append("Interne Referenzen sind kein Konkurrenzangebot und duerfen gegenueber dem Lieferanten nicht als solches genannt werden.")
    return "\n".join(lines)
