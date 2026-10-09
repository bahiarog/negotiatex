"""
Lokale Text-Embeddings fuer die semantische Belegsuche.

Das Modell liegt fest im Docker-Image (siehe Dockerfile) und laeuft im
Backend-Prozess; HF_HUB_OFFLINE verhindert Netzwerkzugriffe zur Laufzeit.
Dokumenttexte verlassen den Server also nie.

Modellauswahl (09.10.2026): jina-embeddings-v2-base-de schlug im Test mit
deutschen Einkaufsbegriffen paraphrase-multilingual-MiniLM-L12-v2 deutlich
(Treffer@1 11/12 vs. 9/12). Ein Modellwechsel aendert MODEL_NAME; Abschnitte
mit anderem embedding_model werden bei der Suche ignoriert und muessen neu
indexiert werden (maintenance/backfill_mdc_embeddings.py) -- Vektorraeume
verschiedener Modelle werden nie gemischt.
"""
import logging
import os
import threading

logger = logging.getLogger(__name__)

MODEL_NAME = "jinaai/jina-embeddings-v2-base-de"
DIM = 768
_CACHE_DIR = os.getenv("MDC_EMBEDDING_CACHE", "/opt/models")
# Geteilter 4-Kern-Server mit weiteren Anwendungen: Threads pro Worker begrenzen.
_THREADS = int(os.getenv("MDC_EMBEDDING_THREADS", "2"))

_model = None
_lock = threading.Lock()


def _get_model():
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                from fastembed import TextEmbedding
                _model = TextEmbedding(MODEL_NAME, cache_dir=_CACHE_DIR, threads=_THREADS)
                logger.info(f"Embedding-Modell geladen: {MODEL_NAME}")
    return _model


def warm_up() -> None:
    try:
        embed_texts(["Vorladen"])
    except Exception:
        logger.exception("Embedding-Modell konnte nicht vorgeladen werden -- Suche faellt auf Volltext zurueck.")


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Synchron und CPU-gebunden -- aus async-Code per asyncio.to_thread
    aufrufen. Vektoren werden L2-normalisiert (Kosinus = Skalarprodukt)."""
    if not texts:
        return []
    model = _get_model()
    with _lock:
        vectors = list(model.embed(texts))
    out = []
    for v in vectors:
        norm = float((v ** 2).sum()) ** 0.5 or 1.0
        out.append([float(x) / norm for x in v])
    return out


def to_pgvector(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.7f}" for x in vec) + "]"
