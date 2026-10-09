"""
Master Data Center Etappe 3 -- Belegsuche (search_reference_context).

v1: PostgreSQL-Volltextsuche (Konfiguration 'german') ueber Abschnitte
vollstaendig freigegebener Dokumentversionen. Eine Vektorsuche (pgvector)
ist vorbereitet (MDCRetrievalChunk.embedding_*), aber bewusst nicht
aktiviert: das DB-Image hat kein pgvector und es ist kein Embedding-
Anbieter freigegeben (siehe Bericht).
"""
import re

CHUNKING_VERSION = "lines-v1"
MAX_CHUNK_CHARS = 800
_ANCHOR_RE = re.compile(r"^(Zeile \d+|--- Page \d+ ---|--- Sheet: [^-]+ ---)")


def build_chunks(text: str) -> list[tuple[str, str]]:
    """Zerlegt den Dokumenttext entlang der Beleganker aus pdf_parser
    (Zeilen, Seiten, Tabellenblaetter) in Abschnitte von hoechstens
    MAX_CHUNK_CHARS Zeichen. Rueckgabe: [(anker, text)]."""
    chunks: list[tuple[str, str]] = []
    buf: list[str] = []
    first_anchor = last_anchor = None

    def flush():
        nonlocal buf, first_anchor, last_anchor
        if buf:
            anchor = first_anchor if first_anchor == last_anchor or not last_anchor else f"{first_anchor} bis {last_anchor}"
            chunks.append(((anchor or "")[:100], "\n".join(buf)))
        buf, first_anchor, last_anchor = [], None, None

    for line in (text or "").splitlines():
        if not line.strip():
            continue
        m = _ANCHOR_RE.match(line)
        anchor = m.group(1).strip("- ").strip() if m else None
        if buf and sum(len(b) + 1 for b in buf) + len(line) > MAX_CHUNK_CHARS:
            flush()
        if anchor:
            first_anchor = first_anchor or anchor
            last_anchor = anchor
        buf.append(line)
    flush()
    return chunks
