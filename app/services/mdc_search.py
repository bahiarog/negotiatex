"""
Master Data Center -- Zuschnitt der Suchabschnitte (search_reference_context).

lines-v2: Tabellenzeilen ("Zeile N: ...", aus pdf_parser fuer CSV) werden
einzeln indexiert, Fliesstext wird je Seite/Tabellenblatt in Abschnitte von
hoechstens MAX_CHUNK_CHARS Zeichen gepackt. Grund: Abschnitte, die ein
ganzes Dokument umfassen, verwaessern die Bedeutung fuer die semantische
Suche (gemessen 09.10.2026: relevante Treffer fielen unter das Rauschen) und
liefern ungenaue Belegstellen. Ein Wechsel der Version erfordert Neu-
indexierung (maintenance/reindex_mdc_search.py).
"""
import re

CHUNKING_VERSION = "lines-v2"
MAX_CHUNK_CHARS = 400
_ROW_RE = re.compile(r"^Zeile (\d+):")
_PAGE_RE = re.compile(r"^--- Page (\d+) ---$")
_SHEET_RE = re.compile(r"^--- Sheet: (.+?) ---$")


def build_chunks(text: str) -> list[tuple[str, str]]:
    """Rueckgabe: [(anker, text)]. Anker ist 'Zeile N', 'Seite N' oder
    'Blatt X' -- genau die Stelle, die als Beleg angezeigt wird."""
    chunks: list[tuple[str, str]] = []
    buf: list[str] = []
    section = None

    def flush():
        if buf:
            chunks.append(((section or "")[:100], "\n".join(buf)))
            buf.clear()

    for line in (text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        page, sheet, row = _PAGE_RE.match(s), _SHEET_RE.match(s), _ROW_RE.match(s)
        if page or sheet:
            flush()
            section = f"Seite {page.group(1)}" if page else f"Blatt {sheet.group(1)}"
            continue
        if row:
            flush()
            chunks.append((f"Zeile {row.group(1)}", s))
            continue
        if buf and sum(len(b) + 1 for b in buf) + len(s) > MAX_CHUNK_CHARS:
            flush()
        buf.append(s)
    flush()
    return chunks
