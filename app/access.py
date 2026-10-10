"""
Rollen und Bereiche (CTO-Vorgabe "Navigation, Kunden- und Arbeitsbereich",
Konzeptstand 10.10.2026).

Rollen je Mitgliedschaft (mehrere moeglich, keine getrennten Datenkopien):
  customer     -- Kundenbereich: eigene bzw. freigegebene Vorhaben des Mandanten
  procurement  -- Arbeitsbereich: operative Einkaufsarbeit (Sourcing, NDA,
                  Angebote, Verhandlung, Data Center)
  admin        -- Administration des eigenen Mandanten (Nutzer, Rollen,
                  Einstellungen, Systembetrieb)
Entscheidungsbefugnis (can_decide) ist ein eigenes Recht: Agent starten,
Verhandlungsmandat bestaetigen, Anbieter beauftragen. Admin-Sein gewaehrt
sie NICHT automatisch.

Plattformweite Daten (Altbestand "klassisches Dashboard", Nutzerliste aller
Mandanten) sind nur mit dem ausdruecklichen Plattform-Flag users.is_admin
erreichbar -- nie ueber eine Mandantenrolle.

Die Navigation wird aus GET /api/v1/me/access abgeleitet; massgeblich ist aber
ausschliesslich diese serverseitige Pruefung je Route.
"""
from dataclasses import dataclass, field

from fastapi import Depends, Header, HTTPException, Request

from database import get_db
from deps import get_current_membership, get_current_user

ROLES = ("customer", "procurement", "admin")
ROLE_LABELS = {"customer": "Kunde / Auftraggeber", "procurement": "Procurement-Experte", "admin": "Admin"}


def roles_of(membership) -> set[str]:
    raw = getattr(membership, "roles_json", None)
    if raw:
        return {r for r in raw if r in ROLES}
    # Altbestand ohne gepflegte Rollen: Eigentuemer = Kunde + Admin, sonst Kunde
    role = getattr(membership.role, "value", membership.role)
    return {"customer", "admin"} if role == "owner" else {"customer"}


@dataclass
class Access:
    user: object
    membership: object
    roles: set = field(default_factory=set)
    can_decide: bool = False

    @property
    def tenant_id(self):
        return self.membership.tenant_id

    @property
    def workspace(self) -> bool:
        return bool(self.roles & {"procurement", "admin"})

    @property
    def procurement(self) -> bool:
        return "procurement" in self.roles

    @property
    def admin(self) -> bool:
        return "admin" in self.roles

    @property
    def can_approve_messages(self) -> bool:
        """Ausgehende Nachrichten an Dienstleister freigeben: operative
        Experten oder Kunden mit Entscheidungsbefugnis."""
        return self.procurement or self.can_decide


async def get_access(user=Depends(get_current_user), membership=Depends(get_current_membership)) -> Access:
    return Access(user=user, membership=membership, roles=roles_of(membership),
                  can_decide=bool(getattr(membership, "can_decide", False)))


async def require_workspace(access: Access = Depends(get_access)) -> Access:
    if not access.workspace:
        raise HTTPException(403, "Kein Zugriff: Diese Funktion gehoert zum Arbeitsbereich.")
    return access


async def require_procurement(access: Access = Depends(get_access)) -> Access:
    if not access.procurement:
        raise HTTPException(403, "Kein Zugriff: Nur fuer Procurement-Experten.")
    return access


async def require_admin(access: Access = Depends(get_access)) -> Access:
    if not access.admin:
        raise HTTPException(403, "Kein Zugriff: Nur fuer Administratoren dieses Mandanten.")
    return access


def require_decider(access: Access) -> None:
    if not access.can_decide:
        raise HTTPException(403, "Fuer diese Entscheidung fehlt Ihnen die Entscheidungsbefugnis. "
                                 "Bitte wenden Sie sich an Ihren Administrator.")


def require_message_approver(access: Access) -> None:
    if not access.can_approve_messages:
        raise HTTPException(403, "Nachrichten an Dienstleister duerfen nur Procurement-Experten oder "
                                 "entscheidungsbefugte Nutzer freigeben.")


async def require_platform_admin(user=Depends(get_current_user)):
    """Plattformweiter Zugriff (Altbestand ueber alle Mandanten). Nur mit
    ausdruecklichem Plattform-Flag, nie ueber eine Mandantenrolle."""
    if not getattr(user, "is_admin", False):
        raise HTTPException(403, "Kein Zugriff: Nur fuer Plattform-Administratoren.")
    return user


def platform_admin_except(*public_suffixes: str):
    """Router-weite Sperre fuer Altbestand-Router, die einzelne oeffentliche,
    tokengebundene Endpunkte enthalten (z.B. Lieferanten-Einladung). Alle
    anderen Pfade verlangen einen Plattform-Administrator."""
    async def dep(request: Request, authorization: str = Header(default=None), db=Depends(get_db)):
        path = request.url.path.rstrip("/")
        if any(path.endswith(s) for s in public_suffixes):
            return None
        user = await get_current_user(authorization, db)
        if not getattr(user, "is_admin", False):
            raise HTTPException(403, "Kein Zugriff: Nur fuer Plattform-Administratoren.")
        return user
    return dep
