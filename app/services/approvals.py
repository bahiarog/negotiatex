"""Prüfpfad für Ablehnungen (NX-033).

Freigaben werden je Modul in *_approvals gespeichert (wer, wann, Hash). Ablehnungen und verworfene
Entwürfe landen hier in einer gemeinsamen, für die Anwendung unveränderlichen Tabelle
(models_projects.ActionRejection), damit ein Prüfer sieht, wer welchen Inhalt warum nicht verschickt hat.
"""
import hashlib
from typing import Optional


def content_hash(recipient, subject, body) -> str:
    raw = "|".join("" if p is None else str(p) for p in (recipient, subject, body))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def record_rejection(db, *, tenant_id, kind: str, action_id, rejected_by, reason: Optional[str] = None,
                           recipient=None, subject=None, body=None, project_id=None):
    """Legt einen Eintrag an (kein Commit -- der Aufrufer committet zusammen mit der Statusänderung)."""
    from models_projects import ActionRejection
    if project_id is None:
        project_id = await _project_for_action(db, kind, action_id)
    row = ActionRejection(
        tenant_id=tenant_id, kind=kind, action_id=str(action_id), project_id=project_id,
        rejected_by=str(rejected_by or "unbekannt")[:100], reason=(reason or "").strip()[:2000] or None,
        recipient_email=(str(recipient)[:255] if recipient else None), subject=(str(subject)[:500] if subject else None),
        payload_hash=content_hash(recipient, subject, body) if (recipient or subject or body) else None,
    )
    db.add(row)
    return row


async def _project_for_action(db, kind: str, action_id):
    """Vorhaben zur Aktion, soweit eindeutig ermittelbar; sonst None (Eintrag bleibt trotzdem erhalten)."""
    from sqlalchemy import select
    from models_projects import Project
    try:
        if kind == "rfq":
            from models_contracts import RFQAction
            a = (await db.execute(select(RFQAction).where(RFQAction.id == action_id))).scalar_one_or_none()
            if a:
                return (await db.execute(select(Project.id).where(Project.rfq_id == a.rfq_id))).scalars().first()
        if kind == "outreach":
            from models_sourcing import OutreachAction, SupplierCandidate
            a = (await db.execute(select(OutreachAction).where(OutreachAction.id == action_id))).scalar_one_or_none()
            if a:
                c = (await db.execute(select(SupplierCandidate).where(SupplierCandidate.id == a.supplier_candidate_id))).scalar_one_or_none()
                if c:
                    return (await db.execute(select(Project.id).where(Project.sourcing_request_id == c.sourcing_request_id))).scalars().first()
    except Exception:
        return None
    return None
