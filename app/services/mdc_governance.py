"""
Master Data Center Etappe 4 -- Berechtigungen, Audit, Nutzungsrechte.

Rollen (bestehendes Mitgliedschaftsmodell: owner/member):
  owner  -- entscheidet: Preispositionen freigeben/ablehnen, Kategorien,
            Nutzungsrechte, Widerruf, Aufbewahrungssperre, Loeschen, Audit.
  member -- arbeitet zu: hochladen, extrahieren, korrigieren (hebt eine
            Freigabe auf), Pruefanfragen, Analysen, Suche, Belege.
Eine eigene Rolle "Procurement Data Steward" (Anleitung Abschnitt 14) waere
eine Erweiterung des membership_role-Enums und ist bewusst noch nicht
angelegt, solange es pro Mandant nur wenige Nutzer gibt.
"""
from datetime import date, datetime
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from models_mdc import MDCAuditEvent


def require_owner(membership) -> None:
    role = membership.role.value if hasattr(membership.role, "value") else membership.role
    if role != "owner":
        raise HTTPException(403, "Nur Owner des Mandanten duerfen diese Entscheidung treffen.")


async def audit(db: AsyncSession, tenant_id, actor, action: str, object_type: str,
                object_id=None, **details) -> None:
    """Haengt ein Audit-Ereignis an die laufende Transaktion an -- es wird
    nur gespeichert, wenn die Aktion selbst erfolgreich committet wird."""
    db.add(MDCAuditEvent(
        tenant_id=tenant_id, actor=str(actor), action=action, object_type=object_type,
        object_id=str(object_id) if object_id is not None else None,
        details_json={k: (v.isoformat() if isinstance(v, (date, datetime)) else v) for k, v in details.items()},
    ))


def usage_block_reason(doc, today: Optional[date] = None) -> Optional[str]:
    """None, wenn das Dokument genutzt werden darf; sonst der Grund."""
    today = today or date.today()
    if doc.rights_revoked_at is not None:
        return f"Nutzungsrecht am {doc.rights_revoked_at.date().isoformat()} widerrufen."
    if doc.rights_valid_until is not None and doc.rights_valid_until < today:
        return f"Nutzungsrecht am {doc.rights_valid_until.isoformat()} abgelaufen."
    return None


def require_usable(doc) -> None:
    reason = usage_block_reason(doc)
    if reason:
        raise HTTPException(403, reason)


# SQL-Bedingung fuer Rohabfragen (Alias d = mdc_documents).
USABLE_SQL = "d.rights_revoked_at IS NULL AND (d.rights_valid_until IS NULL OR d.rights_valid_until >= CURRENT_DATE)"
