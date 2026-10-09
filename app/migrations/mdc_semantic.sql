-- Semantische Belegsuche (pgvector). Idempotent; als Superuser (negotiatex) ausfuehren.
-- Voraussetzung: DB-Container auf negotiatex-postgres:16-pgvector0.8.0 (db/Dockerfile).
-- Vorgehen bei der Umstellung am 09.10.2026: pg_dumpall-Backup, Wiederherstellungstest
-- in Wegwerf-Container mit dem neuen Image (Zeilen, Policies, Rollen identisch), dann
-- Wechsel des Images auf demselben Volume (gleiche Alpine/musl-Basis -> Kollation unveraendert).
-- Dimension 768 = jinaai/jina-embeddings-v2-base-de (services/mdc_embeddings.py).

CREATE EXTENSION IF NOT EXISTS vector;
ALTER TABLE mdc_retrieval_chunks ADD COLUMN IF NOT EXISTS embedding vector(768);
CREATE INDEX IF NOT EXISTS ix_mdc_chunks_embedding ON mdc_retrieval_chunks USING hnsw (embedding vector_cosine_ops);
