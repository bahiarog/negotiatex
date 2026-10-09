"""
Berechtigungstests Master Data Center (Etappe 4, Abnahmekriterium "Kein
Zugriff auf fremde Daten"; Anleitung Abschnitt 17: "Anderer Mandant /
manipulierte Tool-ID -> Zugriff vor Retrieval blockieren").

Legt voruebergehend an: Mandant B mit Owner, ein Nur-Mitglied im Bestands-
mandanten A, einen Nutzer ohne Mandant. Prueft dann ueber die echte API
(innerhalb des Backend-Containers, ohne nginx) jeden Endpunkt mit A-IDs aus
Sicht von B, alle Owner-Entscheidungen aus Sicht des Mitglieds, Zugriffe ohne
oder mit gefaelschtem Token, und RLS direkt in der Datenbank. Raeumt alle
Testobjekte danach wieder ab; Daten von A werden nur gelesen.

Ausfuehren:
  docker exec -w /app -e PYTHONPATH=/app negotiatex-backend python tests/mdc_permissions_acceptance.py
"""
import asyncio
import json
import os
import sys
import uuid

import httpx
from sqlalchemy import text

from database import AdminSessionLocal
from routers.auth import make_token

API = "http://localhost:8000/api/v1"
MDC = API + "/mdc"
TAG = f"mdc-permtest-{uuid.uuid4().hex[:8]}"
RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    RESULTS.append((ok, name))
    print(("PASS " if ok else "FAIL ") + name + (f"  | {detail}" if detail and not ok else ""))


async def setup() -> dict:
    async with AdminSessionLocal() as db:
        a = (await db.execute(text("""
            SELECT d.tenant_id, d.id AS doc_id, v.id AS ver_id, d.category_id
            FROM mdc_documents d JOIN mdc_document_versions v ON v.document_id = d.id
            WHERE v.import_status = 'indexed' ORDER BY d.created_at LIMIT 1"""))).one()
        item = (await db.execute(text("""
            SELECT id FROM mdc_line_items WHERE document_version_id = :v AND review_status = 'approved' LIMIT 1"""),
            {"v": a.ver_id})).scalar()
        snap = (await db.execute(text("SELECT id FROM mdc_analysis_snapshots WHERE tenant_id = :t LIMIT 1"), {"t": a.tenant_id})).scalar()
        rfq = (await db.execute(text("SELECT id FROM rfqs WHERE tenant_id = :t LIMIT 1"), {"t": a.tenant_id})).scalar()
        ctx = {"a_tenant": str(a.tenant_id), "a_doc": str(a.doc_id), "a_ver": str(a.ver_id), "a_cat": str(a.category_id),
               "a_item": str(item), "a_snap": str(snap), "a_rfq": str(rfq) if rfq else None,
               "a_doc_status": None, "b_tenant": str(uuid.uuid4())}
        users = {}
        for key in ("b_owner", "a_member", "no_tenant"):
            uid, email = str(uuid.uuid4()), f"{TAG}-{key}@example.invalid"
            await db.execute(text("INSERT INTO users (id, email, name, is_active, is_admin) VALUES (:id, :e, :n, true, false)"),
                             {"id": uid, "e": email, "n": f"Permtest {key}"})
            users[key] = (uid, email)
        await db.execute(text("INSERT INTO tenants (id, company_name) VALUES (:id, :n)"), {"id": ctx["b_tenant"], "n": f"{TAG} Mandant B"})
        await db.execute(text("INSERT INTO memberships (id, tenant_id, user_id, role) VALUES (gen_random_uuid(), :t, :u, 'owner')"),
                         {"t": ctx["b_tenant"], "u": users["b_owner"][0]})
        await db.execute(text("INSERT INTO memberships (id, tenant_id, user_id, role) VALUES (gen_random_uuid(), :t, :u, 'member')"),
                         {"t": ctx["a_tenant"], "u": users["a_member"][0]})
        ctx["a_doc_status"] = (await db.execute(text("SELECT string_agg(import_status::text, ',' ORDER BY version_number) FROM mdc_document_versions WHERE document_id = :d"), {"d": ctx["a_doc"]})).scalar()
        a_owner = (await db.execute(text("SELECT u.id, u.email FROM memberships m JOIN users u ON u.id = m.user_id WHERE m.tenant_id = :t AND m.role = 'owner' LIMIT 1"), {"t": ctx["a_tenant"]})).one()
        await db.commit()
    ctx["users"] = users
    ctx["tok"] = {k: make_token(uid, email, False) for k, (uid, email) in users.items()}
    ctx["tok"]["a_owner"] = make_token(str(a_owner.id), a_owner.email, False)
    forged = ctx["tok"]["b_owner"]
    ctx["tok"]["forged"] = forged[:-4] + ("AAAA" if not forged.endswith("AAAA") else "BBBB")
    return ctx


async def cleanup(ctx: dict, b_doc_paths: list) -> None:
    async with AdminSessionLocal() as db:
        await db.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": ctx["b_tenant"]})  # kaskadiert B-Daten
        await db.execute(text("DELETE FROM memberships WHERE user_id = ANY(CAST(:u AS uuid[]))"), {"u": [u for u, _ in ctx["users"].values()]})
        await db.execute(text("DELETE FROM users WHERE email LIKE :p"), {"p": f"{TAG}-%"})
        await db.commit()
    for p in b_doc_paths:
        try:
            os.remove(p)
        except FileNotFoundError:
            pass


async def main() -> int:
    ctx = await setup()
    b_doc_paths = []
    H = lambda who: {"Authorization": f"Bearer {ctx['tok'][who]}"}
    a = ctx
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            # --- 1. Fremder Mandant (Owner von B) gegen jedes A-Objekt -----------------
            print("\n# Fremder Mandant: Zugriff auf Objekte von Mandant A")
            denied = [
                ("GET", f"/documents/{a['a_doc']}", None),
                ("GET", f"/documents/{a['a_doc']}/versions/{a['a_ver']}/text", None),
                ("GET", f"/documents/{a['a_doc']}/versions/{a['a_ver']}/download", None),
                ("GET", f"/documents/{a['a_doc']}/versions/{a['a_ver']}/line-items", None),
                ("POST", f"/documents/{a['a_doc']}/versions/{a['a_ver']}/extract-lines?replace=true", None),
                ("PUT", f"/documents/{a['a_doc']}/rights", {"usage_purpose": "gekapert"}),
                ("POST", f"/documents/{a['a_doc']}/revoke", {"reason": "Angriff"}),
                ("POST", f"/documents/{a['a_doc']}/legal-hold", {"active": True, "reason": "Angriff"}),
                ("DELETE", f"/documents/{a['a_doc']}", {"reason": "Angriff"}),
                ("GET", f"/line-items/{a['a_item']}", None),
                ("PUT", f"/line-items/{a['a_item']}", {"original_amount": "1"}),
                ("POST", f"/line-items/{a['a_item']}/approve", {}),
                ("POST", f"/line-items/{a['a_item']}/reject", {"note": "x"}),
                ("POST", f"/line-items/{a['a_item']}/request-review", {"reason": "x"}),
                ("POST", "/analyses", {"line_item_id": a["a_item"]}),
                ("GET", f"/analyses/{a['a_snap']}", None),
                ("GET", f"/evidence/{a['a_item']}", None),
            ]
            for method, path, body in denied:
                r = await c.request(method, MDC + path, headers=H("b_owner"), json=body)
                check(r.status_code in (403, 404), f"B {method} {path.split('?')[0].replace(a['a_doc'], '<A-Dok>').replace(a['a_ver'], '<A-Ver>').replace(a['a_item'], '<A-Pos>').replace(a['a_snap'], '<A-Akte>')} -> {r.status_code}", r.text[:200])

            r = await c.post(MDC + "/documents/upload", headers=H("b_owner"), files={"file": ("x.csv", b"Rolle,Preis\nEditor,1\n", "text/csv")},
                             data={"revision_of_document_id": a["a_doc"]})
            check(r.status_code == 404, f"B Revision auf A-Dokument -> {r.status_code}", r.text[:200])
            r = await c.post(MDC + "/documents/upload", headers=H("b_owner"), files={"file": ("y.csv", b"Rolle,Preis\nEditor,2\n", "text/csv")},
                             data={"category_id": a["a_cat"]})
            check(r.status_code == 404, f"B Upload in A-Kategorie -> {r.status_code}", r.text[:200])
            if a["a_rfq"]:
                r = await c.post(f"{API}/rfq/{a['a_rfq']}/offers/{uuid.uuid4()}/reference-check", headers=H("b_owner"),
                                 json={"category_id": a["a_cat"], "role_or_item": "Editor", "unit": "Stunde"})
                check(r.status_code in (403, 404), f"B RFQ-Referenzabgleich auf A-RFQ -> {r.status_code}", r.text[:200])

            print("\n# Fremder Mandant: Listen, Suche, Kennzahlen enthalten keine A-Daten")
            for path, needle in [("/documents", a["a_doc"]), ("/line-items", a["a_item"]), ("/line-items?review_status=approved", a["a_item"]),
                                 ("/analyses", a["a_snap"]), (f"/analyses?line_item_id={a['a_item']}", a["a_snap"]), ("/audit", a["a_doc"])]:
                r = await c.get(MDC + path, headers=H("b_owner"))
                check(r.status_code == 200 and needle not in r.text, f"B Liste {path.split('?')[0]} ohne A-Daten -> {r.status_code}", r.text[:200])
            for path in ("/offer-targets", "/categories"):
                r = await c.get(MDC + path, headers=H("b_owner"))
                check(r.status_code == 200 and r.json() == [], f"B Liste {path} leer -> {r.status_code}", r.text[:200])
            r = await c.get(MDC + "/search", params={"q": "Editor Reisekosten"}, headers=H("b_owner"))
            check(r.status_code == 200 and r.json().get("hits") == [], "B Belegsuche findet keine A-Dokumente", r.text[:200])
            r = await c.get(MDC + "/suppliers", headers=H("b_owner"))
            check(r.status_code == 200 and r.json() == [], "B Lieferantenliste leer")
            r = await c.get(MDC + "/metrics", headers=H("b_owner"))
            check(r.status_code == 200 and r.json()["positions"]["total"] == 0 and r.json()["rights"]["documents"] == 0, "B Kennzahlen zeigen keine A-Zahlen")
            r = await c.get(MDC + "/health", headers=H("b_owner"))
            check(r.status_code == 200 and r.json()["checks"]["search_index"]["chunks"] == 0, "B Health zaehlt keine A-Suchabschnitte")

            print("\n# Gegenrichtung: eigene Daten von B sind fuer A unsichtbar")
            r = await c.post(MDC + "/documents/upload", headers=H("b_owner"),
                             files={"file": (f"{TAG}.csv", f"{TAG}\nRolle,Preis\nEditor,123\n".encode(), "text/csv")}, data={"title": f"{TAG} B-Dokument"})
            b_doc = r.json().get("id") if r.status_code == 200 else None
            check(b_doc is not None, f"B kann eigenes Dokument anlegen -> {r.status_code}", r.text[:200])
            if b_doc:
                async with AdminSessionLocal() as db:
                    b_doc_paths = list((await db.execute(text("SELECT file_path FROM mdc_document_versions WHERE document_id = :d"), {"d": b_doc})).scalars())
                r = await c.get(MDC + "/documents", headers=H("a_owner"))
                check(r.status_code == 200 and b_doc not in r.text, "A sieht B-Dokument nicht in der Liste")
                r = await c.get(MDC + f"/documents/{b_doc}", headers=H("a_owner"))
                check(r.status_code == 404, f"A GET B-Dokument -> {r.status_code}")

            # --- 2. Rollen: Nur-Mitglied in A -------------------------------------------
            print("\n# Nur-Mitglied im eigenen Mandanten: Owner-Entscheidungen gesperrt")
            for method, path, body in [
                ("POST", f"/line-items/{a['a_item']}/approve", {}), ("POST", f"/line-items/{a['a_item']}/reject", {"note": "x"}),
                ("PUT", f"/documents/{a['a_doc']}/rights", {"usage_purpose": "x"}), ("POST", f"/documents/{a['a_doc']}/revoke", {"reason": "x"}),
                ("POST", f"/documents/{a['a_doc']}/legal-hold", {"active": True, "reason": "x"}), ("DELETE", f"/documents/{a['a_doc']}", {"reason": "x"}),
                ("POST", "/categories", {"name": f"{TAG} Kategorie"}), ("GET", "/audit", None),
            ]:
                r = await c.request(method, MDC + path, headers=H("a_member"), json=body)
                check(r.status_code == 403, f"Mitglied {method} {path.split('/')[-1] or path} -> {r.status_code}", r.text[:200])
            for path in ["/documents", f"/documents/{a['a_doc']}", f"/evidence/{a['a_item']}", "/metrics", "/line-items"]:
                r = await c.get(MDC + path, headers=H("a_member"))
                check(r.status_code == 200, f"Mitglied darf lesen: {path.split('/')[1]} -> {r.status_code}", r.text[:200])
            r = await c.get(MDC + "/search", params={"q": "Editor"}, headers=H("a_member"))
            check(r.status_code == 200 and len(r.json()["hits"]) > 0, "Mitglied findet Belege des eigenen Mandanten")

            # --- 3. Anmeldung -----------------------------------------------------------
            print("\n# Ohne gueltige Anmeldung")
            for path in ["/documents", f"/documents/{a['a_doc']}", "/search?q=Editor", f"/evidence/{a['a_item']}", "/metrics", "/audit"]:
                r = await c.get(MDC + path)
                check(r.status_code == 401, f"ohne Token {path.split('?')[0]} -> {r.status_code}")
                r = await c.get(MDC + path, headers=H("forged"))
                check(r.status_code == 401, f"gefaelschtes Token {path.split('?')[0]} -> {r.status_code}")
            r = await c.get(MDC + "/documents", headers=H("no_tenant"))
            check(r.status_code == 403, f"Nutzer ohne Mandant -> {r.status_code}")

        # --- 4. Datenbank: RLS fuer jede Data-Center-Tabelle --------------------------
        print("\n# Datenbank: RLS mit App-Rolle")
        async with AdminSessionLocal() as db:
            tables = list((await db.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename LIKE 'mdc\\_%' ORDER BY 1"))).scalars())
            for t in tables:
                await db.execute(text("SET ROLE negotiatex_app"))
                await db.execute(text("SELECT set_config('app.tenant_id', '', false)"))
                n_none = (await db.execute(text(f"SELECT count(*) FROM {t}"))).scalar()
                await db.execute(text("SELECT set_config('app.tenant_id', :b, false)"), {"b": ctx["b_tenant"]})
                n_a = (await db.execute(text(f"SELECT count(*) FROM {t} WHERE tenant_id = :a"), {"a": ctx["a_tenant"]})).scalar()
                await db.execute(text("RESET ROLE"))
                check(n_none == 0 and n_a == 0, f"RLS {t}: ohne Mandant {n_none}, als B sichtbare A-Zeilen {n_a}")
            await db.rollback()
            still = (await db.execute(text("SELECT string_agg(import_status::text, ',' ORDER BY version_number) FROM mdc_document_versions WHERE document_id = :d"), {"d": ctx["a_doc"]})).scalar()
            check(still == ctx["a_doc_status"], f"A-Dokument nach allen Angriffen unveraendert ({still})")
    finally:
        await cleanup(ctx, b_doc_paths)
        async with AdminSessionLocal() as db:
            left = (await db.execute(text("SELECT count(*) FROM users WHERE email LIKE :p"), {"p": f"{TAG}-%"})).scalar()
        print(f"\nAufraeumen: Testnutzer uebrig {left}, Mandant B geloescht.")

    passed = sum(ok for ok, _ in RESULTS)
    print(f"\n{passed}/{len(RESULTS)} bestanden")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
