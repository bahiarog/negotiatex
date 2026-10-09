"""
Teil B7/B8 -- Angebots-Extraktion (KI, kein `tools=`) und Angebotsvergleich.

Extraktion: identisches Muster wie routers/suppliers.py
EXTRACTION_SYSTEM_PROMPT/_extract_json -- NIE Werte erfinden, fehlende Felder
bleiben null/"unknown". Keine zweite Parsing-Pipeline, nur ein anderes
Zielschema (Angebotsfelder statt Lieferanten-Stammdaten).

Vergleich: compute_offer_comparison() ist reines Decimal-basiertes
Server-Code -- KEIN LLM-Aufruf. Die KI wird nirgends gefragt "wer gewinnt",
die Rechnung ist deterministisch (Playbook-Zitat B8: 'Der Agent darf daraus
keine Einsparung bei identischem Umfang ableiten', wenn Umfang/Qualitaet
abweicht -- durchgesetzt durch den Vergleich gegen RFQ.expected_quantity,
siehe `_comparability`).
"""
import logging
from decimal import Decimal, InvalidOperation
from typing import Optional

logger = logging.getLogger(__name__)

OFFER_EXTRACTION_SYSTEM_PROMPT = """Du extrahierst strukturierte Angebotsdaten aus einem Angebots-\
dokument (PDF-Text oder E-Mail-Text) eines Lieferanten, als Antwort auf eine Angebotsanfrage (RFQ).

Gib AUSSCHLIESSLICH ein JSON-Objekt zurueck, exakt mit diesen Feldern (keine zusaetzlichen Felder, \
kein Freitext davor/danach):
{"total_price": null, "unit_price": null, "quantity": null, "currency": null, "freight_cost": null, "other_costs": null, \
"delivery_date": null, "payment_terms": null, "offer_validity_until": null, "spec_confirmed": null, \
"scope_note": null}

Regeln:
- Trage NUR Werte ein, die im Dokument eindeutig erkennbar sind. Bei Unsicherheit oder Nichtvorhandensein: null.
- Erfinde NIEMALS plausibel klingende Zahlen.
- "total_price": der im Dokument ausdruecklich genannte Gesamtpreis NETTO (Angebotssumme), als Zahl.
- "unit_price" ist der Stueckpreis NETTO (ohne Steuer), als Zahl (Punkt als Dezimaltrennzeichen). Bei einem
  Pauschal-/Dienstleistungsangebot ohne Stueckpreis: unit_price = Gesamtpreis netto und quantity = 1.
- "freight_cost"/"other_costs" NUR fuer Kosten, die ZUSAETZLICH zum Gesamtpreis berechnet werden; Positionen, die
  bereits in der Angebotssumme enthalten sind, hier NICHT erneut eintragen. 0 falls ausdruecklich "keine", sonst null.
- Datumsangaben im Format YYYY-MM-DD.
- "spec_confirmed": true nur wenn der Bieter die Spezifikation ausdruecklich bestaetigt; false, wenn er \
ausdruecklich eine Abweichung nennt; sonst null.
- "scope_note": Freitext, falls der Bieter eine Abweichung vom Bedarf/Spezifikation kennzeichnet, sonst null."""


def _extract_json(text: str) -> dict:
    import json, re
    text = (text or "").strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        text = m.group(0)
    return json.loads(text)


def extract_offer_fields(document_text: str) -> dict:
    """Ruft Anthropic auf (KEIN tools=) um Angebotsfelder aus Freitext zu
    extrahieren. Bei jedem Fehler: leeres Geruest zurueckgeben (alle Felder
    null) statt zu raten -- der Aufrufer (Router) verlangt dann manuelle
    Nacherfassung."""
    empty = {
        "total_price": None, "unit_price": None, "quantity": None, "currency": None, "freight_cost": None,
        "other_costs": None, "delivery_date": None, "payment_terms": None,
        "offer_validity_until": None, "spec_confirmed": None, "scope_note": None,
    }
    try:
        import anthropic
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=1000,
            thinking={"type": "disabled"},  # sonst verbraucht das Nachdenken das Token-Budget, Antwort bliebe leer
            system=OFFER_EXTRACTION_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": f"Angebotstext:\n\n{document_text[:12000]}"}],
            # Bewusst KEIN `tools=` Parameter -- siehe Moduldoc.
        )
        raw = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        data = _extract_json(raw)
        for k in empty:
            empty[k] = data.get(k, None)
        return empty
    except Exception:
        logger.exception("rfq_classifier: Angebots-Extraktion fehlgeschlagen -> alle Felder unknown/null.")
        return empty


def _dec(v) -> Optional[Decimal]:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


def compute_total_cost(unit_price, quantity, freight_cost, other_costs) -> Optional[Decimal]:
    """Gesamtkosten = Stueckpreis*Menge + Fracht + weitere Kosten. Decimal
    durchgehend, nie float. None, falls Stueckpreis oder Menge fehlt (keine
    Gesamtkosten ohne beide Kernfelder -- nicht geraten)."""
    up, qty = _dec(unit_price), _dec(quantity)
    if up is None or qty is None:
        return None
    freight = _dec(freight_cost) or Decimal("0")
    other = _dec(other_costs) or Decimal("0")
    return (up * qty) + freight + other


def _comparability(offer_quantity, expected_quantity, scope_note: Optional[str], spec_confirmed) -> tuple[str, Optional[str]]:
    """Best-effort (dokumentierte Vereinfachung, wie detect_redline): ein
    Angebot gilt als NICHT direkt vergleichbar, wenn
      (a) eine erwartete Menge (RFQ.expected_quantity) hinterlegt ist und die
          Angebotsmenge davon abweicht, ODER
      (b) der Bieter explizit eine Spezifikationsabweichung gekennzeichnet hat
          (scope_note gesetzt oder spec_confirmed==False).
    Das ist ein einfacher Feldvergleich, KEIN semantisches Verstehen von
    'gleichwertig' -- bei Unsicherheit wird lieber geflaggt als stillschweigend
    gleichgesetzt (Playbook-Zitat B8)."""
    qty = _dec(offer_quantity)
    exp = _dec(expected_quantity)
    if exp is not None and qty is not None and qty != exp:
        return "flagged_different_scope", f"Menge {qty} weicht von erwarteter Menge {exp} ab."
    if spec_confirmed is False:
        return "flagged_different_scope", "Bieter hat eine Spezifikationsabweichung ausdruecklich gekennzeichnet."
    if scope_note:
        return "flagged_different_scope", f"Bieter-Hinweis auf Abweichung: {scope_note[:300]}"
    return "comparable", None


def compute_offer_comparison(rfq, offers: list, weights: dict) -> dict:
    """Reines Decimal-Rechenwerk, KEIN LLM-Aufruf. `offers` sind ORM-Objekte
    (models_contracts.RFQOffer) mit status != withdrawn (superseded Zeilen werden
    als Historie mitgeliefert, aber nicht in die Rangliste aufgenommen).

    Rueckgabe enthaelt je Bieter die volle Kostenbasis, den
    Vergleichbarkeits-Flag, eine Rangliste NUR der vergleichbaren Angebote,
    die abgeleiteten Verbesserungs-Kennzahlen (Playbook-Beispiel: 4.600 EUR /
    1.400 EUR) sofern eine Verhandlungs-Historie (version>1) vorliegt, und das
    explizite 'Einkauf entscheidet'-Feld -- der Zuschlag ist IMMER eine
    menschliche Aktion ueber /rfq/{id}/award, nie automatisch."""
    rows = []
    by_candidate: dict = {}
    for o in offers:
        if o.status.value == "withdrawn" if hasattr(o.status, "value") else o.status == "withdrawn":
            continue
        total = compute_total_cost(o.unit_price, o.quantity, o.freight_cost, o.other_costs)
        flag, note = _comparability(o.quantity, rfq.expected_quantity, o.scope_note, o.spec_confirmed)
        # Falls bereits bei Ingestion geflaggt (z.B. manuell), nie "entflaggen" --
        # nur zusaetzlich flaggen (strengerer der beiden Zustaende gewinnt).
        existing_flag = o.comparability_flag.value if hasattr(o.comparability_flag, "value") else o.comparability_flag
        if existing_flag == "flagged_different_scope":
            flag = "flagged_different_scope"
            note = note or o.comparability_note
        row = {
            "offer_id": str(o.id),
            "supplier_candidate_id": str(o.supplier_candidate_id),
            "version": o.version,
            "status": o.status.value if hasattr(o.status, "value") else o.status,
            "unit_price_net": str(o.unit_price) if o.unit_price is not None else None,
            "quantity": str(o.quantity) if o.quantity is not None else None,
            "freight_cost": str(o.freight_cost) if o.freight_cost is not None else None,
            "other_costs": str(o.other_costs) if o.other_costs is not None else None,
            "goods_value_net": str(((_dec(o.unit_price) or Decimal(0)) * (_dec(o.quantity) or Decimal(0))).quantize(Decimal("0.01"))) if total is not None else None,
            "total_cost_net": str(total.quantize(Decimal("0.01"))) if total is not None else None,
            "currency": o.currency,
            "delivery_date": o.delivery_date,
            "payment_terms": o.payment_terms,
            "offer_validity_until": o.offer_validity_until.isoformat() if o.offer_validity_until else None,
            "comparability_flag": flag,
            "comparability_note": note,
            "received_at": o.received_at.isoformat() if o.received_at else None,
        }
        rows.append(row)
        by_candidate.setdefault(str(o.supplier_candidate_id), []).append((o, total))

    comparable_rows = [r for r in rows if r["comparability_flag"] == "comparable" and r["total_cost_net"] is not None]
    flagged_rows = [r for r in rows if r["comparability_flag"] != "comparable" or r["total_cost_net"] is None]

    ranked = sorted(comparable_rows, key=lambda r: Decimal(r["total_cost_net"]))
    for i, r in enumerate(ranked, start=1):
        r["rank"] = i
    rank_lookup = {r["offer_id"]: r["rank"] for r in ranked}
    for r in rows:
        r["rank"] = rank_lookup.get(r["offer_id"])

    # Negotiation deltas: fuer jeden Bieter mit >1 Version, vergleiche die
    # NEUESTE (hoechste Version) Zeile gegen (a) seine eigene aelteste Version
    # und (b) das guenstigste vergleichbare AS-SUBMITTED (version==1) Angebot
    # eines ANDEREN Bieters.
    best_other_as_submitted = None
    for cid, entries in by_candidate.items():
        for o, total in entries:
            if o.version == 1 and total is not None:
                flag, _ = _comparability(o.quantity, rfq.expected_quantity, o.scope_note, o.spec_confirmed)
                if flag == "comparable":
                    if best_other_as_submitted is None or total < best_other_as_submitted[1]:
                        best_other_as_submitted = (cid, total)

    negotiation_deltas = []
    for cid, entries in by_candidate.items():
        if len(entries) < 2:
            continue
        entries_sorted = sorted(entries, key=lambda e: e[0].version)
        first_o, first_total = entries_sorted[0]
        last_o, last_total = entries_sorted[-1]
        if first_total is None or last_total is None:
            continue
        q = Decimal("0.01")
        delta = {
            "supplier_candidate_id": cid,
            "own_original_total_net": str(first_total.quantize(q)),
            "negotiated_total_net": str(last_total.quantize(q)),
            "improvement_vs_own_original_eur": str((first_total - last_total).quantize(q)),
        }
        if best_other_as_submitted and best_other_as_submitted[0] != cid:
            other_cid, other_total = best_other_as_submitted
            delta["compared_against_candidate_id"] = other_cid
            delta["best_other_as_submitted_total_net"] = str(other_total.quantize(q))
            delta["advantage_vs_best_other_as_submitted_eur"] = str((other_total - last_total).quantize(q))
        negotiation_deltas.append(delta)

    return {
        "rfq_id": str(rfq.id),
        "evaluation_weights": weights,
        "offers": rows,
        "ranked_comparable": ranked,
        "flagged_not_comparable": flagged_rows,
        "negotiation_deltas": negotiation_deltas,
        "einkauf_entscheidet_ueber_eignung_und_zuschlag": True,
        "note": (
            "Rangliste und Verbesserungskennzahlen beziehen sich ausschliesslich auf vergleichbare "
            "Angebote (gleicher erwarteter Umfang). Bei abweichendem Umfang/Qualitaet wird KEINE "
            "Einsparung bei identischem Umfang unterstellt -- solche Angebote erscheinen unter "
            "'flagged_not_comparable'. Der Zuschlag ist ausschliesslich eine menschliche Entscheidung "
            "ueber POST /rfq/{id}/award, nie automatisch."
        ),
    }
