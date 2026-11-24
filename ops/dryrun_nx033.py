"""
Abnahme NX-033 (Unveraenderlichkeit des Pruefpfads) gegen die echte Datenbank -- als Anwendungsrolle
negotiatex_app, jede Pruefung in einer eigenen Transaktion, die per rollback verworfen wird.
Es wird nichts dauerhaft geaendert und nichts versendet.

Voraussetzung: Deploy von NX-033 und app/migrations/audit_immutability.sql ausgefuehrt.
Aufruf auf dem Server:
  docker cp ops/dryrun_nx033.py negotiatex-backend:/tmp/dryrun_nx033.py
  docker exec -w /app -e PYTHONPATH=/app negotiatex-backend python /tmp/dryrun_nx033.py
"""
import asyncio
import sys

from sqlalchemy import text

from database import engine  # Anwendungsrolle negotiatex_app (RLS aktiv)
from database import admin_engine

FAILS = 0


def check(name, ok, detail=""):
    global FAILS
    FAILS += not ok
    print(("OK     " if ok else "FEHLER ") + name + (f" ({detail})" if detail else ""))


async def expect_denied(name, tenant, sql, params=None):
    async with engine.connect() as c:
        tx = await c.begin()
        try:
            await c.execute(text("SELECT set_config('app.tenant_id', :t, false)"), {"t": tenant})
            await c.execute(text(sql), params or {})
            check(name, False, "wurde ausgefuehrt")
        except Exception as e:
            check(name, "permission denied" in str(e).lower(), str(e).splitlines()[0][:120])
        finally:
            await tx.rollback()


async def expect_allowed(name, tenant, sql, params=None):
    async with engine.connect() as c:
        tx = await c.begin()
        try:
            await c.execute(text("SELECT set_config('app.tenant_id', :t, false)"), {"t": tenant})
            await c.execute(text(sql), params or {})
            check(name, True)
        except Exception as e:
            check(name, False, str(e).splitlines()[0][:160])
        finally:
            await tx.rollback()


async def main():
    async with admin_engine.connect() as c:
        tenant = (await c.execute(text("SELECT tenant_id FROM project_events ORDER BY created_at DESC LIMIT 1"))).scalar()
        ev = (await c.execute(text("SELECT id FROM project_events WHERE tenant_id = :t LIMIT 1"), {"t": tenant})).scalar()
        appr = {}
        for t in ("outreach_approvals", "rfq_approvals", "negotiation_approvals"):
            appr[t] = (await c.execute(text(f"SELECT a.id FROM {t} a LIMIT 1"))).scalar()
    tenant = str(tenant)
    print(f"Mandant {tenant[:8]}…, Ereignis {str(ev)[:8]}…")

    for t, aid in appr.items():
        if aid is None:
            print(f"HINWEIS {t}: keine Zeile vorhanden, UPDATE/DELETE-Pruefung ohne Treffer")
        await expect_denied(f"{t}: UPDATE verweigert", tenant, f"UPDATE {t} SET payload_hash = payload_hash")
        await expect_denied(f"{t}: DELETE verweigert", tenant, f"DELETE FROM {t}")
    await expect_denied("project_events: Titel aendern verweigert", tenant,
                        "UPDATE project_events SET title = title WHERE id = :i", {"i": ev})
    await expect_denied("project_events: DELETE verweigert", tenant, "DELETE FROM project_events WHERE id = :i", {"i": ev})
    await expect_allowed("project_events: emailed_at nachtragen erlaubt", tenant,
                         "UPDATE project_events SET emailed_at = emailed_at WHERE id = :i", {"i": ev})
    await expect_allowed("action_rejections: anlegen erlaubt", tenant,
                         "INSERT INTO action_rejections (id, tenant_id, kind, action_id, rejected_by, created_at) "
                         "VALUES (gen_random_uuid(), :t, 'rfq', 'dryrun', 'dryrun', now())", {"t": tenant})
    await expect_denied("action_rejections: UPDATE verweigert", tenant, "UPDATE action_rejections SET reason = reason")
    await expect_denied("action_rejections: DELETE verweigert", tenant, "DELETE FROM action_rejections")

    # Mandantentrennung: Eintrag fuer fremden Mandanten anlegen -> RLS verweigert
    await expect_denied_rls(tenant)

    print(f"\n{FAILS} FEHLER" if FAILS else "\nALLE PRUEFUNGEN BESTANDEN")
    return 1 if FAILS else 0


async def expect_denied_rls(tenant):
    async with engine.connect() as c:
        tx = await c.begin()
        try:
            await c.execute(text("SELECT set_config('app.tenant_id', :t, false)"), {"t": tenant})
            await c.execute(text("INSERT INTO action_rejections (id, tenant_id, kind, action_id, rejected_by, created_at) "
                                 "VALUES (gen_random_uuid(), gen_random_uuid(), 'rfq', 'dryrun', 'dryrun', now())"))
            check("action_rejections: fremder Mandant verweigert (RLS)", False, "wurde angelegt")
        except Exception as e:
            msg = str(e).lower()
            check("action_rejections: fremder Mandant verweigert (RLS)", "row-level security" in msg or "foreign key" in msg,
                  str(e).splitlines()[0][:120])
        finally:
            await tx.rollback()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
