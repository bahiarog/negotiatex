"""
Phase 1 structured line-item extraction. Sibling to pdf_parser.py (which
already does free-text/table extraction) rather than a rewrite of it, to
avoid touching the existing, already-working extract_text() used elsewhere
(e.g. routers/public.py, routers/audit.py).

Extraction is expected to be imperfect on real-world documents. Missing
fields MUST stay None -- this module never fabricates a plausible-looking
number. Callers (routers/cases.py) are responsible for deciding whether a
case needs to move to NEEDS_DATA based on which fields came back empty.
"""
import re
import logging
from decimal import Decimal, InvalidOperation
from typing import Optional

logger = logging.getLogger(__name__)

# Maps our structured field names to the header-text fragments (DE/EN) we
# look for in a CSV header row or an extracted PDF table header row.
_FIELD_ALIASES = {
    "position_nr": ["position", "pos", "lfd_nr", "nr"],
    "description": ["description", "beschreibung", "leistung", "artikel", "bezeichnung", "leistungsumfang"],
    "quantity": ["quantity", "menge", "anzahl", "qty"],
    "unit": ["unit", "einheit"],
    "unit_price": ["unit_price", "einzelpreis", "preis_pro_einheit", "price"],
    "net_total": ["net_total", "nettosumme", "netto_summe", "gesamt_netto", "summe", "total"],
    "currency": ["currency", "waehrung", "währung"],
    "tax_rate": ["tax_rate", "steuersatz", "mwst", "ust", "steuerkennzeichnung"],
    "contract_period": ["contract_period", "laufzeit"],
    "cancellation_notice": ["cancellation_notice", "kuendigungsfrist", "kündigungsfrist"],
}

_DECIMAL_FIELDS = ("quantity", "unit_price", "net_total", "tax_rate")

_EMPTY_ITEM = {
    "position_nr": None, "description": None, "quantity": None, "unit": None,
    "unit_price": None, "net_total": None, "currency": None, "tax_rate": None,
    "contract_period": None, "cancellation_notice": None,
}


def _to_decimal(raw) -> Optional[Decimal]:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s.lower() in ("nan", "none", "-", "n/a", ""):
        return None
    s = s.replace("€", "").replace("EUR", "").replace("USD", "").replace("%", "").strip()
    # German number format "1.234,56" -> "1234.56"; "1234,56" -> "1234.56"
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _clean_text(raw) -> Optional[str]:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s.lower() in ("nan", "none"):
        return None
    return s


def _build_colmap(header_cells) -> dict:
    colmap = {}
    header = [str(h or "").strip().lower() for h in header_cells]
    for field, aliases in _FIELD_ALIASES.items():
        for idx, h in enumerate(header):
            if any(a in h for a in aliases):
                colmap[field] = idx
                break
    return colmap


def extract_line_items_from_csv(file_path: str) -> list[dict]:
    """Simple column-mapped CSV import. Column names are matched
    case-insensitively against common DE/EN aliases; unmatched/missing
    columns simply leave that field as None."""
    import pandas as pd

    try:
        df = pd.read_csv(file_path, encoding="utf-8", sep=None, engine="python")
    except Exception as e:
        logger.error(f"CSV konnte nicht gelesen werden: {e}")
        return []

    columns = list(df.columns)
    colmap = _build_colmap(columns)
    if not colmap:
        return []

    items = []
    for i, (_, row) in enumerate(df.iterrows(), start=1):
        item = dict(_EMPTY_ITEM)
        item["position_nr"] = i
        item["confidence"] = 0.7  # CSV with mapped headers is fairly reliable
        for field, col_idx in colmap.items():
            col_name = columns[col_idx]
            raw = row.get(col_name)
            if field == "position_nr":
                try:
                    item["position_nr"] = int(_to_decimal(raw) or i)
                except Exception:
                    pass
            elif field in _DECIMAL_FIELDS:
                item[field] = _to_decimal(raw)
            else:
                item[field] = _clean_text(raw)
        items.append(item)
    return items


_TABLE_RE = re.compile(r"\[TABLE\]\n(.*?)\n\[/TABLE\]", re.S)


def extract_line_items_from_pdf_text(text: str) -> list[dict]:
    """Best-effort structured extraction from the pipe-delimited [TABLE]
    blocks that services/pdf_parser.py's PDF extraction already produces.
    If no recognizable header row is found in a table, that table is
    skipped entirely (no guessed mapping) rather than produce garbage
    fields."""
    items = []
    if not text:
        return items

    for match in _TABLE_RE.finditer(text):
        rows = [r for r in match.group(1).split("\n") if r.strip()]
        if len(rows) < 2:
            continue
        header_cells = [c.strip() for c in rows[0].split("|")]
        colmap = _build_colmap(header_cells)
        if not colmap:
            continue  # unrecognized table layout -- skip rather than guess

        for pos, row in enumerate(rows[1:], start=1):
            cells = [c.strip() for c in row.split("|")]
            item = dict(_EMPTY_ITEM)
            item["position_nr"] = pos
            item["confidence"] = 0.4  # PDF table parsing is inherently less reliable
            for field, idx in colmap.items():
                if idx >= len(cells):
                    continue
                raw = cells[idx]
                if field == "position_nr":
                    try:
                        item["position_nr"] = int(_to_decimal(raw) or pos)
                    except Exception:
                        pass
                elif field in _DECIMAL_FIELDS:
                    item[field] = _to_decimal(raw)
                else:
                    item[field] = _clean_text(raw)
            items.append(item)
    return items
