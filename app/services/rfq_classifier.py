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
"scope_note": null, "priced_quantity": null, "priced_quantity_quote": null, "optional_items": []}

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
- "scope_note": Freitext, falls der Bieter eine Abweichung vom Bedarf/Spezifikation kennzeichnet, sonst null.
  Optionale/abwaehlbare Positionen sind KEINE Abweichung (nicht in scope_note, sondern in "optional_items").
- "priced_quantity": die Stueckzahl/Menge, fuer die der Gesamtpreis gilt, NUR wenn das Dokument sie ausdruecklich
  nennt (z. B. "20 hoehenverstellbare Schreibtische"), als Zahl; sonst null.
- "priced_quantity_quote": woertliches, unveraendertes Zitat (hoechstens 200 Zeichen) aus dem Dokument, in dem diese
  Menge steht; sonst null.
- "optional_items": Liste der optionalen, abwaehlbaren oder ausdruecklich separat angebotenen Positionen, je
  {"description": "...", "price": Zahl oder null, "included_in_total": true/false/null, "quote": "woertliches Zitat"};
  "included_in_total" = true nur, wenn der Preis dieser Position in dem unter "total_price" genannten Betrag
  enthalten ist. Keine optionalen Positionen: []."""


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
        "priced_quantity": None, "priced_quantity_quote": None, "optional_items": [],
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
            empty[k] = data.get(k, empty[k])
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


def _norm_text(s) -> str:
    import re
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def quote_in_text(quote, text: str) -> bool:
    """Woertliches Zitat (bis auf Leerraum und Gross-/Kleinschreibung) im Dokument?"""
    q = _norm_text(quote)
    return len(q) >= 3 and q in _norm_text(text)


def _number_in(quote: str, n: Decimal) -> bool:
    """Ganze Zahl n steht als eigene Zahl im Zitat (nicht Teil von 120, 20,5, 20.11. oder 20 %)."""
    import re
    if n != n.to_integral_value():
        return False
    return bool(re.search(rf"(?<![\d.,]){int(n)}(?![\d]|[.,]\d|\s*%)", quote or ""))


def verify_priced_quantity(extracted: dict, text: str) -> Optional[dict]:
    """Menge, fuer die der Gesamtpreis gilt -- nur mit woertlichem Beleg im
    Angebotstext, der die Zahl enthaelt. Sonst None (nichts ohne Beleg)."""
    qty = _dec((extracted or {}).get("priced_quantity"))
    quote = (extracted or {}).get("priced_quantity_quote")
    if qty is None or qty <= 0 or not isinstance(quote, str):
        return None
    if not quote_in_text(quote, text) or not _number_in(quote, qty) or not _is_quantity_quote(quote, qty):
        return None
    return {"quantity": qty, "quote": " ".join(quote.split())[:300]}


# Einheiten direkt hinter der Zahl, die KEINE Stueckmenge bedeuten (QA NX-001: "Lieferung in 20 Werktagen").
# Ganze Woerter mit Pluralformen, damit "20 Tagungsstuehle" nicht als "20 Tag..." verworfen wird (NX-005).
_NOT_A_QUANTITY_RE = (r"(werktag|arbeitstag|kalendertag|tag|woche|monat|jahr|stunde|minute|sekunde)(e|en|n|s)?"
                      r"|std\.?|min\.?|tg\.?|prozent|uhr|km|kg|cm|mm|m|m²|m2|qm|meter|lfm|liter|l|g|t"
                      r"|euro|eur|€|teillieferung(en)?|raten?")
# Woerter direkt vor der Zahl, nach denen sie eine Schwelle oder Grenze ist ("ab 20 Stueck Rabatt").
_THRESHOLD_BEFORE_RE = (r"(^|[^a-zäöüß])(ab|mindestens|mind\.|min\.|bis|bis zu|höchstens|hoechstens|max\.|maximal"
                        r"|mehr als|weniger als|mindestabnahme|mindestmenge:?|mindestbestellmenge:?|kw|seite|raum"
                        r"|art\.?-nr\.?|artikel-nr\.?|anfrage-nr\.?)$")
# Beschriftete Mengenangabe ("Menge: 20").
_QTY_LABEL_RE = r"(^|[^a-zäöüß])(menge|anzahl|stückzahl|stueckzahl|stück|stueck)\s*:?$"


def _is_quantity_quote(quote: str, qty: Decimal) -> bool:
    """Das Zitat muss die angebotene Menge beschreiben: hinter der Zahl ein Wort (Stueck, Tische, x ...) oder
    eine beschriftete Angabe ("Menge: 20"); keine Zeitspanne oder Masseinheit, kein Mass ("20 x 20 cm"), keine
    Schwelle ("ab 20 Stueck"), keine Positionsnummer ("Pos. 20") und keine bloss zitierte Anfrage ("Ihre Anfrage
    ueber 20 ..."). Ein Verweis auf die Anfrage ist erlaubt, wenn vor der Zahl ein Angebotsverb steht ("Gemaess
    Ihrer Anfrage bieten wir 20 ... an"). Im Zweifel False -> Angebot bleibt markiert."""
    import re
    q = " ".join(str(quote or "").split())
    low = q.lower()
    if any(w in low for w in ("rabatt", "staffel", "optional", "zubuchbar", "auf wunsch")):
        return False  # Menge in einer Rabatt-/Staffel-/Optionsangabe ist nicht die angebotene Menge
    if "angefragt" in low:
        return False
    if "anfrag" in low:
        # Bezug auf die Anfrage nur in der engen Form "Gemaess Ihrer Anfrage bieten wir (Ihnen) 20 ..." und nur
        # einmal im Zitat; sonst ist es meist die zitierte Anfragemenge ("statt der angefragten 20", QA NX-005).
        if low.count("anfrag") != 1 or not re.search(
                rf"(gemäß|gemaess|laut|entsprechend) ihrer anfrage,? (bieten|liefern|offerieren) wir (ihnen )?"
                rf"{int(qty)}(?![\d]|[.,]\d)", low):
            return False
    for m in re.finditer(rf"(?<![\d.,]){int(qty)}(?![\d]|[.,]\d)", q):
        before = low[:m.start()].rstrip()
        after = low[m.end():].lstrip()
        if before.endswith(("pos.", "pos", "position", "nr.", "nr")):
            continue
        if re.search(_THRESHOLD_BEFORE_RE, before):
            continue
        if re.search(r"\d\s*[x×]$", before) or re.match(r"[x×]\s*\d", after):
            continue  # Massangabe "160 x 20" / "20 x 20 cm"
        if re.match(rf"({_NOT_A_QUANTITY_RE})(?![a-zäöüß0-9])", after):
            continue
        if re.search(_QTY_LABEL_RE, before) and (not after or not after[0].isdigit()):
            return True  # "Menge: 20" / "Stück: 20"
        if len(q) < 12 or not after or not (after[0].isalpha() or after[0] in "x×"):
            continue
        return True
    return False


def lump_sum_from_json(raw) -> Optional[dict]:
    """Gespeicherten Mengenbeleg (raw_extracted_json) fuer _comparability lesen."""
    if not isinstance(raw, dict) or not raw.get("lump_sum_quantity") or not raw.get("lump_sum_quote"):
        return None
    qty = _dec(raw.get("lump_sum_quantity"))
    return {"quantity": qty, "quote": str(raw["lump_sum_quote"])} if qty is not None else None


def _comparability(offer_quantity, expected_quantity, scope_note: Optional[str], spec_confirmed,
                   lump_sum: Optional[dict] = None) -> tuple[str, Optional[str]]:
    """Best-effort (dokumentierte Vereinfachung, wie detect_redline): ein
    Angebot gilt als NICHT direkt vergleichbar, wenn
      (a) eine erwartete Menge (RFQ.expected_quantity) hinterlegt ist und die
          Angebotsmenge davon abweicht, ODER
      (b) der Bieter explizit eine Spezifikationsabweichung gekennzeichnet hat
          (scope_note gesetzt oder spec_confirmed==False).
    Das ist ein einfacher Feldvergleich, KEIN semantisches Verstehen von
    'gleichwertig' -- bei Unsicherheit wird lieber geflaggt als stillschweigend
    gleichgesetzt (Playbook-Zitat B8).

    Ausnahme (NX-001): Gesamtpreis als Pauschale (Menge 1) fuer die angefragte
    Stueckzahl. `lump_sum` = {"quantity", "quote"} aus verify_priced_quantity
    (woertlich im Angebot belegt). Stimmt die belegte Menge mit der erwarteten
    ueberein, gilt die Menge als vergleichbar (Beleg steht in der Notiz);
    weicht sie ab, bleibt das Angebot markiert."""
    qty = _dec(offer_quantity)
    exp = _dec(expected_quantity)
    ok_note = None
    if exp is not None and qty is not None and qty != exp:
        covered = _dec((lump_sum or {}).get("quantity"))
        quote = ((lump_sum or {}).get("quote") or "")[:300]
        if qty != 1 or covered is None or not quote:
            return "flagged_different_scope", f"Menge {qty} weicht von erwarteter Menge {exp} ab."
        if covered != exp:
            return "flagged_different_scope", (f"Gesamtpreis gilt laut Angebot fuer Menge {covered.normalize():f}, "
                                               f"erwartet {exp.normalize():f} (Beleg: „{quote}“).")
        ok_note = f"Gesamtpreis fuer die angefragte Menge {exp.normalize():f}, im Angebot belegt: „{quote}“."
    if spec_confirmed is False:
        return "flagged_different_scope", "Bieter hat eine Spezifikationsabweichung ausdruecklich gekennzeichnet."
    if scope_note:
        return "flagged_different_scope", f"Bieter-Hinweis auf Abweichung: {scope_note[:300]}"
    return "comparable", ok_note


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
        flag, note = _comparability(o.quantity, rfq.expected_quantity, o.scope_note, o.spec_confirmed,
                                    lump_sum_from_json(o.raw_extracted_json))
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
                flag, _ = _comparability(o.quantity, rfq.expected_quantity, o.scope_note, o.spec_confirmed,
                                         lump_sum_from_json(o.raw_extracted_json))
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
