"""
Teil B9 -- Klauselkategorien-bewusste Redline-Erkennung fuer Vertraege.

Wiederverwendet das ehrliche Vereinfachungsprinzip von
services/sourcing_classifier.detect_redline (einfacher, whitespace-
normalisierter Text-Vergleich -- KEIN echtes Dokumenten-Diffing/semantisches
Verstehen). Erweitert es nur um eine Zuordnung geaenderter Absaetze zu festen
Klausel-Kategorien (Haftung/IP/Datenschutz/Laufzeit), damit eine Aenderung in
genau diesen Kategorien separat auf `legal_review_required` routet (B9:
'jede erkannte Aenderung an Haftung, IP, Datenschutz, Laufzeit/Kuendigung
routet auf legal_review_required, getrennt von einem einfachen kaufmaennischen
Redline'). Best-effort, dokumentiert als solcher -- kein Anspruch auf
vollstaendige juristische Klassifikation.
"""
import re

_CLAUSE_KEYWORDS = {
    "liability": [
        r"haftung", r"haftungs", r"schadens?ersatz", r"liability", r"liable",
    ],
    "ip": [
        r"geistiges\s+eigentum", r"nutzungsrechte", r"urheberrecht", r"intellectual\s+property",
        r"schutzrechte", r"lizenz",
    ],
    "data_protection": [
        r"datenschutz", r"dsgvo", r"personenbezogene\s+daten", r"data\s+protection", r"gdpr",
    ],
    "term_termination": [
        r"laufzeit", r"kuendigung", r"kündigung", r"vertragsdauer", r"termination", r"\bterm\b",
    ],
}


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").strip())


def _paragraphs(t: str) -> list[str]:
    raw = (t or "").replace("\r\n", "\n")
    parts = [p for p in re.split(r"\n\s*\n", raw) if p.strip()]
    return parts or ([raw] if raw.strip() else [])


def detect_redline(sent_text: str, returned_text: str) -> bool:
    """Identisch zu sourcing_classifier.detect_redline (Whitespace-normalisierter
    Volltextvergleich), hier dupliziert statt importiert, damit dieses Modul
    eigenstaendig lesbar bleibt (kleine, bewusst in Kauf genommene Redundanz --
    siehe Bericht)."""
    return _norm(sent_text) != _norm(returned_text)


def classify_clause_changes(sent_text: str, returned_text: str) -> list[str]:
    """Best-effort: vergleicht Absatz fuer Absatz (nach Position); ein Absatz
    gilt als 'geaendert', wenn der normalisierte Text an dieser Position
    abweicht ODER sich die Anzahl der Absaetze unterscheidet (dann werden
    zusaetzliche/fehlende Absaetze ebenfalls gepruft). Fuer jeden geaenderten
    Absatz wird geprueft, ob er eine der festen Klausel-Kategorie-Phrasen
    enthaelt (im GEAENDERTEN Text ODER im urspruenglichen Text an derselben
    Stelle) -- so wird auch eine GELOESCHTE Haftungsklausel erkannt, nicht nur
    eine neu eingefuegte. Reiner Stichwort-Scan, KEINE juristische Bewertung."""
    sent_paras = _paragraphs(sent_text)
    ret_paras = _paragraphs(returned_text)
    max_len = max(len(sent_paras), len(ret_paras))

    touched_categories: set[str] = set()
    for i in range(max_len):
        s = sent_paras[i] if i < len(sent_paras) else ""
        r = ret_paras[i] if i < len(ret_paras) else ""
        if _norm(s) == _norm(r):
            continue
        combined = (s + " " + r).lower()
        for category, patterns in _CLAUSE_KEYWORDS.items():
            if any(re.search(p, combined) for p in patterns):
                touched_categories.add(category)

    return sorted(touched_categories)


def extract_numbers_for_mandate_check(text: str) -> dict:
    """Sehr einfache Heuristik: sucht nach Euro-Betraegen und Zahltagen im
    Text, um grobe Hinweise fuer die kaufmaennische Pruefung zu liefern
    (B9 'Kaufmaennische Punkte'). Liefert KEINE verlaesslichen strukturierten
    Vertragsdaten -- dient nur als Hinweis im Review, ersetzt nicht die
    menschliche Pruefung."""
    eur_amounts = [m.replace(".", "").replace(",", ".") for m in re.findall(r"([\d]{1,3}(?:\.\d{3})*(?:,\d{2})?)\s*(?:€|EUR)", text or "")]
    payment_days = re.findall(r"(\d{1,3})\s*Tage", text or "")
    return {"eur_amounts_found": eur_amounts, "payment_days_found": payment_days}
