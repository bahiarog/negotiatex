"""
Kontrollierte Neuindexierung der Belegsuche:
  - Versionen mit veraltetem Zuschnitt (chunking_version) werden neu
    zerlegt und vektorisiert,
  - Abschnitte ohne Embedding oder mit einem anderen als dem aktuellen
    Modell werden neu vektorisiert (Vektorraeume werden nie gemischt).

Laeuft mandantenuebergreifend als Systemaufgabe ueber AdminSessionLocal --
dieselbe dokumentierte Ausnahme wie die Hintergrund-Jobs (database.py).
Jeder Abschnitt behaelt tenant_id und Version seiner Quelle.

Ausfuehren:
  docker exec -w /app -e PYTHONPATH=/app negotiatex-backend python maintenance/reindex_mdc_search.py
"""
import asyncio

from sqlalchemy import delete, select, text

from database import AdminSessionLocal
from models_mdc import MDCDocumentVersion, MDCImportStatus, MDCRetrievalChunk
from routers.mdc import embed_chunks
from services.mdc_embeddings import MODEL_NAME
from services.mdc_search import CHUNKING_VERSION, build_chunks


async def main() -> None:
    rechunked = embedded = 0
    async with AdminSessionLocal() as db:
        versions = (await db.execute(select(MDCDocumentVersion).where(
            MDCDocumentVersion.import_status == MDCImportStatus.indexed))).scalars().all()
        for v in versions:
            stale = (await db.execute(select(MDCRetrievalChunk.id).where(
                MDCRetrievalChunk.document_version_id == v.id,
                MDCRetrievalChunk.chunking_version != CHUNKING_VERSION).limit(1))).first()
            has_any = (await db.execute(select(MDCRetrievalChunk.id).where(
                MDCRetrievalChunk.document_version_id == v.id).limit(1))).first()
            if stale or not has_any:
                await db.execute(delete(MDCRetrievalChunk).where(MDCRetrievalChunk.document_version_id == v.id))
                chunks = []
                for idx, (anchor, chunk_text) in enumerate(build_chunks(v.extracted_text or "")):
                    c = MDCRetrievalChunk(tenant_id=v.tenant_id, document_version_id=v.id, chunk_index=idx,
                                          anchor=anchor or None, text=chunk_text, chunking_version=CHUNKING_VERSION)
                    db.add(c)
                    chunks.append(c)
                await db.flush()
                embedded += await embed_chunks(db, chunks)
                rechunked += 1
        await db.commit()

        while True:
            rows = (await db.execute(select(MDCRetrievalChunk).join(
                MDCDocumentVersion, MDCDocumentVersion.id == MDCRetrievalChunk.document_version_id,
            ).where(
                MDCDocumentVersion.import_status == MDCImportStatus.indexed,
                text("(mdc_retrieval_chunks.embedding IS NULL OR mdc_retrieval_chunks.embedding_model IS DISTINCT FROM :m)"),
            ).params(m=MODEL_NAME).limit(32))).scalars().all()
            if not rows:
                break
            n = await embed_chunks(db, rows)
            await db.commit()
            if n == 0:
                break
            embedded += n
    print(f"{rechunked} Version(en) neu zerlegt, {embedded} Abschnitt(e) mit {MODEL_NAME} vektorisiert (Zuschnitt {CHUNKING_VERSION}).")


if __name__ == "__main__":
    asyncio.run(main())
