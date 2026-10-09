"""
Taeglicher Wartungsjob des Master Data Center: setzt abgelaufene
Nutzungsrechte durch (Suchabschnitte und Vektoren entfernen). Vergleich,
Belege und Suche pruefen die Rechte zusaetzlich bei jedem Zugriff -- der Job
raeumt den Index auf, damit kein gesperrter Inhalt dort liegen bleibt.

Laeuft mandantenuebergreifend ueber AdminSessionLocal (dokumentierte
System-Ausnahme, database.py). Beide uvicorn-Worker registrieren den
Scheduler; eine Postgres-Advisory-Sperre stellt sicher, dass der Job je Lauf
nur einmal arbeitet.
"""
import logging

from sqlalchemy import text

from database import AdminSessionLocal

logger = logging.getLogger(__name__)
_LOCK_ID = 7270002


async def enforce_rights_expiry_job() -> None:
    async with AdminSessionLocal() as db:
        got = (await db.execute(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": _LOCK_ID})).scalar()
        if not got:
            return
        rows = (await db.execute(text("""
            WITH blocked AS (
                SELECT v.id AS version_id, d.id AS document_id, d.tenant_id
                FROM mdc_documents d JOIN mdc_document_versions v ON v.document_id = d.id
                WHERE d.rights_revoked_at IS NOT NULL
                   OR (d.rights_valid_until IS NOT NULL AND d.rights_valid_until < CURRENT_DATE)
            ), removed AS (
                DELETE FROM mdc_retrieval_chunks c USING blocked b
                WHERE c.document_version_id = b.version_id
                RETURNING b.document_id, b.tenant_id
            )
            SELECT document_id, tenant_id, count(*) AS n FROM removed GROUP BY 1, 2
        """))).all()
        for r in rows:
            await db.execute(text("""
                UPDATE mdc_document_versions SET import_status = 'approved'
                WHERE document_id = :d AND import_status = 'indexed'"""), {"d": r.document_id})
            await db.execute(text("""
                INSERT INTO mdc_audit_events (id, tenant_id, actor, action, object_type, object_id, details_json, created_at)
                VALUES (gen_random_uuid(), :t, 'system', 'rights_expired_deindexed', 'document', :d,
                        CAST(:details AS json), now())"""),
                {"t": r.tenant_id, "d": str(r.document_id), "details": f'{{"removed_search_chunks": {r.n}}}'})
        await db.commit()
        if rows:
            logger.info(f"MDC-Rechteablauf: {sum(r.n for r in rows)} Suchabschnitte aus {len(rows)} Dokument(en) entfernt.")


def register_mdc_jobs(scheduler) -> None:
    from apscheduler.triggers.cron import CronTrigger
    scheduler.add_job(enforce_rights_expiry_job, trigger=CronTrigger(hour=2, minute=30),
                      id="mdc_enforce_rights_expiry", name="MDC: abgelaufene Nutzungsrechte durchsetzen",
                      replace_existing=True)
