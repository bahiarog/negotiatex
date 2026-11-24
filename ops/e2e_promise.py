"""
NX-037: Taeglicher Durchgangstest des Produktversprechens gegen die echte Anwendung und Datenbank.

Ablauf: eigener Test-Mandant "E2E-Test <Zeit>" mit Kundin (entscheidungsbefugt) und Einkaufsexperte ->
Vorhaben anlegen und beauftragen -> Dienstleister hinzufuegen -> Erstanfrage freigeben -> Antwort mit
Angebot und AGB-Anhang geht ein -> Pruefung -> Verhandlung vorbereiten und ablehnen -> Zuschlag ->
Abrechnung -> Pruefpfad. Am Ende wird der Test-Mandant mit allen Daten geloescht (auch bei Fehlern).

Sicherheit:
- Es wird KEINE E-Mail versendet: send_negotiation_email ist in allen Modulen durch eine Attrappe ersetzt,
  smtplib.SMTP wirft zusaetzlich sofort.
- Kein echter KI-Aufruf: anthropic.Anthropic ist eine Attrappe mit festen Antworten (Kosten 0, reproduzierbar).
- Dateien landen in /tmp. Der Scheduler laeuft nicht (kein Lifespan).

Aufruf auf dem Server (Cron: ops/negotiatex-e2e.cron):
  docker cp ops/e2e_promise.py negotiatex-backend:/tmp/e2e_promise.py
  docker exec -w /app -e PYTHONPATH=/app negotiatex-backend python /tmp/e2e_promise.py
Letzte Zeile der Ausgabe: "E2E-ERGEBNIS OK (n/n)" oder "E2E-ERGEBNIS FEHLER (k von n)".
"""
import asyncio
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import types
import uuid
from datetime import datetime, timezone

TMP = tempfile.mkdtemp(prefix="nx-e2e-")
os.environ["MDC_UPLOAD_DIR"] = os.path.join(TMP, "uploads")

# ---------------------------------------------------------------------------------------------------------
# Attrappen VOR dem Import der Anwendung
# ---------------------------------------------------------------------------------------------------------
import smtplib  # noqa: E402


class _NoSMTP:
    def __init__(self, *a, **k):
        raise RuntimeError("E2E: SMTP gesperrt")


smtplib.SMTP = _NoSMTP
smtplib.SMTP_SSL = _NoSMTP

import anthropic  # noqa: E402

OFFER_TOTAL = "10000.00"
AGB_CLAUSE = "Die Haftung des Auftragnehmers ist ausgeschlossen."
AI_CALLS = []


class _FakeMessages:
    def create(self, **kw):
        system = str(kw.get("system") or "")
        AI_CALLS.append(system[:60])
        if "Vertragsklauseln" in system:
            text = json.dumps({"findings": [{"topic": "Haftung", "severity": "critical",
                                             "message": "Haftung vollstaendig ausgeschlossen.", "quote": AGB_CLAUSE}]})
        elif "Verhandlungs-E-Mail" in system:
            import re
            user = str(kw["messages"][0]["content"])
            price = (re.search(r"Unser Preisvorschlag in dieser Runde: (\S+)", user) or [None, "?"])[1]
            text = json.dumps({"subject": "Ihr Angebot – unser Vorschlag",
                               "body": f"Guten Tag,\n\nwir schlagen einen Gesamtpreis von {price} EUR netto vor.\n\nFreundliche Gruesse"})
        elif "Angebotsdaten" in system:
            text = json.dumps({"unit_price": float(OFFER_TOTAL), "quantity": 1, "total_price": float(OFFER_TOTAL),
                               "currency": "EUR", "freight_cost": None, "other_costs": None, "delivery_date": None,
                               "payment_terms": "30 Tage netto", "offer_validity_until": None, "scope_note": None,
                               "spec_confirmed": True, "priced_quantity": None, "priced_quantity_quote": None,
                               "optional_items": []})
        else:
            raise RuntimeError(f"E2E: unerwarteter KI-Aufruf ({system[:60]!r})")
        return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)],
                                     usage=types.SimpleNamespace(input_tokens=0, output_tokens=0))


class _FakeAnthropic:
    def __init__(self, *a, **k):
        self.messages = _FakeMessages()


anthropic.Anthropic = _FakeAnthropic

import services.email_sender as email_sender  # noqa: E402

SENT = []
_REAL_SEND = email_sender.send_negotiation_email


def _fake_send(to_email, subject, body, from_name="NegotiateX.ai", message_id=None, in_reply_to=None,
               references=None, attachments=None):
    mid = message_id or email_sender.new_message_id()
    SENT.append({"to": (to_email or "").lower(), "subject": subject, "body": body, "message_id": mid})
    return {"sent": True, "message": "E2E-Attrappe", "message_id": mid}


from main import app  # noqa: E402  (registriert alle Router; Lifespan/Scheduler startet NICHT)

for _mod in list(sys.modules.values()):
    if getattr(_mod, "send_negotiation_email", None) is _REAL_SEND:
        _mod.send_negotiation_email = _fake_send

import httpx  # noqa: E402
from sqlalchemy import text  # noqa: E402

from database import AdminSessionLocal, admin_engine  # noqa: E402
from routers.auth import User, make_token  # noqa: E402
from models_v2 import Membership, MembershipRole, Tenant  # noqa: E402

STAMP = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
SUPPLIER = f"e2e-dienstleister-{STAMP}@example.invalid"
RESULTS = []


def check(part, name, ok, detail=""):
    RESULTS.append((part, name, bool(ok)))
    print(("OK     " if ok else "FEHLER ") + f"[{part}] {name}" + (f" ({detail})" if detail else ""))


def mails_to(addr):
    return [m for m in SENT if m["to"] == addr.lower()]


def docx_bytes(lines):
    try:
        import docx
        d = docx.Document()
        for ln in lines:
            d.add_paragraph(ln)
        buf = io.BytesIO()
        d.save(buf)
        return buf.getvalue(), ".docx"
    except Exception:
        return "\n".join(lines).encode("utf-8"), ".csv"


async def setup():
    async with AdminSessionLocal() as db:
        tenant = Tenant(company_name=f"E2E-Test {STAMP}")
        db.add(tenant)
        await db.flush()
        kunde = User(name="E2E Kundin", email=f"e2e-kunde-{STAMP}@example.invalid", company_name=tenant.company_name)
        experte = User(name="E2E Experte", email=f"e2e-experte-{STAMP}@example.invalid", company_name=tenant.company_name)
        db.add_all([kunde, experte])
        await db.flush()
        db.add_all([
            Membership(tenant_id=tenant.id, user_id=kunde.id, role=MembershipRole.member, roles_json=["customer"], can_decide=True),
            # Experte ist Inhaber: Loeschen im Data Center verlangt Einkaufsrolle UND Inhaber (Pruefung der Sperre)
            Membership(tenant_id=tenant.id, user_id=experte.id, role=MembershipRole.owner, roles_json=["procurement"],
                       can_decide=False),
        ])
        await db.commit()
        return tenant.id, kunde, experte


async def cleanup(tenant_id, user_ids):
    """Alles mit tenant_id des Test-Mandanten loeschen (als Eigentuemer; Reihenfolge per Wiederholung)."""
    async with admin_engine.connect() as c:
        tables = [r[0] for r in (await c.execute(text(
            "SELECT table_name FROM information_schema.columns WHERE table_schema='public' AND column_name='tenant_id'"))).all()]
        await c.commit()
        remaining = list(tables)
        for _round in range(8):
            failed = []
            for t in remaining:
                try:
                    await c.execute(text(f'DELETE FROM "{t}" WHERE tenant_id = :t'), {"t": tenant_id})
                    await c.commit()
                except Exception:
                    await c.rollback()
                    failed.append(t)
            remaining = failed
            if not remaining:
                break
        await c.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})
        for uid in user_ids:
            await c.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
        await c.commit()
        left = (await c.execute(text("SELECT count(*) FROM tenants WHERE id = :t"), {"t": tenant_id})).scalar()
    shutil.rmtree(TMP, ignore_errors=True)
    return left == 0 and not remaining, remaining


async def run(tenant_id, kunde, experte):
    hk = {"Authorization": "Bearer " + make_token(str(kunde.id), kunde.email, False)}
    he = {"Authorization": "Bearer " + make_token(str(experte.id), experte.email, False)}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://e2e") as cl:
        async def call(method, path, headers, **kw):
            r = await cl.request(method, "/api/v1" + path, headers=headers, **kw)
            if r.status_code >= 400:
                raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
            return r.json()

        # ---- 1. Einkaufen: Vorhaben, Auftrag, Dienstleister, Freigabe ------------------------------------
        p = await call("POST", "/projects", hk, json={
            "title": f"E2E Imagefilm {STAMP}", "service_type": "Videoproduktion",
            "description": "Imagefilm 90 Sekunden, ein Drehtag, Schnitt, Online-Nutzungsrechte 2 Jahre.",
            "budget_ceiling": "9000", "budget_target": "8000", "currency": "EUR", "notify_milestones": True})
        pid = p["id"]  # POST/PUT liefern das Vorhaben direkt (_fresh), GET die Detailansicht
        p = await call("POST", f"/projects/{pid}/start", hk, json={"confirm_mandate": True})
        check("Einkaufen", "Auftrag erteilt", p["status"] in ("sourcing", "collecting_offers"), p["status"])
        d = await call("GET", f"/projects/{pid}", hk)
        check("Einkaufen", "Leerer Bestand -> Hinweis statt erfundener Anbieter",
              any(e["kind"] == "no_suppliers" for e in d["events"]))
        await call("POST", f"/projects/{pid}/candidates", he, json={"items": [{
            "source": "manual", "company_name": "E2E Filmstudio", "contact_email": SUPPLIER, "contact_name": "Test"}]})
        d = await call("GET", f"/projects/{pid}", he)
        drafts = [x for x in (d.get("workspace") or {}).get("drafts") or [] if x["kind"] == "outreach"]
        check("Einkaufen", "Erstanfrage als Entwurf angelegt", len(drafts) == 1, f"{len(drafts)} Entwurf/Entwuerfe")
        check("Freigaben", "Vor der Freigabe keine Mail an den Dienstleister", len(mails_to(SUPPLIER)) == 0)
        await call("POST", f"/projects/{pid}/drafts/outreach/{drafts[0]['id']}/approve", he)
        sent = mails_to(SUPPLIER)
        check("Freigaben", "Nach der Freigabe genau eine Mail", len(sent) == 1, f"{len(sent)}")
        async with admin_engine.connect() as c:
            appr = (await c.execute(text(
                "SELECT a.approved_by, a.payload_hash, o.recipient_email, o.rendered_subject, o.rendered_body "
                "FROM outreach_approvals a JOIN outreach_actions o ON o.id = a.action_id WHERE o.id = :i"),
                {"i": drafts[0]["id"]})).first()
        check("Freigaben", "Freigabe mit Person und Inhalts-Hash dokumentiert",
              appr is not None and appr[0] == str(experte.id) and appr[1] == hashlib.sha256(
                  "|".join(str(x) for x in appr[2:5]).encode()).hexdigest())
        check("Freigaben", "Versendeter Text = freigegebener Text", bool(sent) and appr is not None and sent[0]["body"] == appr[4])

        # ---- 2. Angebot mit AGB-Anhang geht ein --------------------------------------------------------
        offer_bytes, ext = docx_bytes([f"Angebot E2E {STAMP}", "Imagefilm 90 Sekunden inkl. Drehtag und Schnitt.",
                                       f"Gesamtpreis netto: 10.000,00 EUR", "Zahlungsziel 30 Tage netto."])
        agb_bytes, ext2 = docx_bytes(["Allgemeine Geschaeftsbedingungen", AGB_CLAUSE, "Gerichtsstand ist Hamburg."])
        from services.email_poller import _dispatch_inbound_message
        mail = {"message_id": f"<e2e-{STAMP}@example.invalid>", "in_reply_to": sent[0]["message_id"] if sent else None,
                "references_header": sent[0]["message_id"] if sent else None, "from_addr": SUPPLIER,
                "to_addr": "info@negotiatex.ai", "subject": "AW: " + (sent[0]["subject"] if sent else ""),
                "body_text": "Anbei unser Angebot und unsere AGB.", "raw_source": "e2e",
                "attachments": [{"filename": "AGB_E2E" + ext2, "data": agb_bytes},
                                {"filename": "Angebot_E2E" + ext, "data": offer_bytes}]}
        async with AdminSessionLocal() as db:
            res = await _dispatch_inbound_message(db, mail)
        check("Einkaufen", "Antwort mit Anhang als Angebot erkannt", res.get("kind") == "offer", str(res)[:120])
        check("Einkaufen", "Eingangsbestaetigung automatisch (einzige erlaubte Mail ohne Freigabe)",
              len(mails_to(SUPPLIER)) == 2 and "Eingangsbestaetigung" in mails_to(SUPPLIER)[-1]["subject"])
        d = await call("GET", f"/projects/{pid}", he)
        offers = d["offers"]
        check("Einkaufen", "Angebot im Vorhaben sichtbar", len(offers) == 1)
        o = offers[0] if offers else {}
        check("Konditionen", "Preis aus dem Dokument uebernommen", o.get("total") in ("10000.00", "10000.0000", "10000"), o.get("total"))
        check("Konditionen", "Ueber Budget erkannt", o.get("within_budget") is False)
        rv = o.get("review") or {}
        check("AGB", "Klausel aus dem AGB-Anhang gefunden (mit Zitat)",
              any(f.get("quote") == AGB_CLAUSE for f in rv.get("findings") or []))
        check("AGB", "Pruefstatus kritisch, nicht 'ok'", rv.get("status") == "critical", rv.get("status"))
        labels = [x["label"] for x in d.get("documents") or []]
        check("Belege", "Angebot und AGB unter Dokumente", any(x.startswith("Angebot") for x in labels)
              and any(x.startswith("Anhang") for x in labels), ", ".join(labels))

        # ---- 3. Verhandlung vorbereiten und ablehnen -----------------------------------------------------
        await call("POST", f"/projects/{pid}/offers/{o['id']}/negotiate", he, json={})
        d = await call("GET", f"/projects/{pid}", he)
        neg = [x for x in (d.get("workspace") or {}).get("drafts") or [] if x["kind"] == "negotiation"]
        check("Verhandlung", "Preisvorschlag als Entwurf", len(neg) == 1)
        async with admin_engine.connect() as c:
            nb = (await c.execute(text("SELECT a.rendered_body FROM negotiation_actions a WHERE a.tenant_id = :t "
                                       "ORDER BY a.created_at DESC LIMIT 1"), {"t": tenant_id})).scalar() or ""
        # Preis aus der bestaetigten Strategie (Ziel 8.000, hoechstens 15 % unter Angebot -> 8.500), nie ueber dem Angebot
        check("Verhandlung", "Preis aus der Strategie, unter dem Angebotspreis", "8500" in nb.replace(".", "").replace(",00", ""),
              nb[:120].replace("\n", " "))
        before = len(mails_to(SUPPLIER))
        if neg:
            await call("POST", f"/projects/{pid}/drafts/negotiation/{neg[0]['id']}/reject", he, json={"reason": "E2E-Ablehnung"})
        check("Verhandlung", "Abgelehnter Vorschlag wird nicht versendet", len(mails_to(SUPPLIER)) == before)
        async with admin_engine.connect() as c:
            rej = (await c.execute(text("SELECT rejected_by, reason, payload_hash FROM action_rejections WHERE tenant_id = :t "
                                        "AND kind = 'negotiation'"), {"t": tenant_id})).first()
        check("Freigaben", "Ablehnung mit Person, Grund und Inhalts-Hash", rej is not None and rej[0] == str(experte.id)
              and rej[1] == "E2E-Ablehnung" and rej[2] is not None)

        # ---- 4. Zuschlag, Belege gesperrt, Abrechnung -----------------------------------------------------
        a = await call("POST", f"/projects/{pid}/award", hk, json={"offer_id": o["id"]})
        check("Zuschlag", "Vorhaben beauftragt", a["status"] == "awarded", a["status"])
        d = await call("GET", f"/projects/{pid}", he)
        check("Zuschlag", "Zusage nur als Entwurf (keine Mail ohne Freigabe)", len(mails_to(SUPPLIER)) == before)
        async with admin_engine.connect() as c:
            holds = (await c.execute(text("SELECT count(*), count(*) FILTER (WHERE legal_hold) FROM mdc_documents "
                                          "WHERE tenant_id = :t"), {"t": tenant_id})).first()
        check("Belege", "Alle Belege unter Aufbewahrungspflicht", holds[0] >= 2 and holds[0] == holds[1], f"{holds[1]}/{holds[0]}")
        doc_id = (d.get("documents") or [{}])[0].get("id")
        # Inhaber mit Einkaufsrolle (darf grundsaetzlich loeschen) versucht es mit Grund -> Aufbewahrungspflicht verweigert
        r = await cl.request("DELETE", f"/api/v1/mdc/documents/{doc_id}", headers=he, json={"reason": "E2E-Loeschversuch"})
        check("Belege", "Loeschen eines Belegs nach Zuschlag verweigert", r.status_code == 409, f"{r.status_code} {r.text[:80]}")
        await call("POST", f"/projects/{pid}/invoice", hk, json={"amount": "9500"})
        d = await call("GET", f"/projects/{pid}", he)
        res_ = d.get("result") or {}
        check("Savings", "Ausgangsbasis = Erstangebot", str(res_.get("baseline") or "").startswith("10000"), res_.get("baseline"))
        check("Savings", "Realisierte Einsparung berechnet (500)", str(res_.get("realized") or "").startswith("500"), res_.get("realized"))

        # ---- 5. Pruefpfad und Mandantentrennung ------------------------------------------------------------
        kinds = [e["kind"] for e in d.get("events") or []]
        check("Pruefpfad", "Zeitstrahl vollstaendig", all(k in kinds for k in ("created", "started", "offer_received", "awarded", "invoiced")),
              ", ".join(sorted(set(kinds)))[:150])
        async with admin_engine.connect() as c:
            other = (await c.execute(text("SELECT id FROM projects WHERE tenant_id <> :t LIMIT 1"), {"t": tenant_id})).scalar()
        if other:
            r = await cl.get(f"/api/v1/projects/{other}", headers=hk)
            check("Mandanten", "Fremdes Vorhaben fuer Test-Mandant unsichtbar", r.status_code in (403, 404), str(r.status_code))
        check("Sicherheit", "Keine echte Mail, kein echter KI-Aufruf", True, f"{len(SENT)} Mails abgefangen, {len(AI_CALLS)} KI-Attrappen")


async def main():
    t0 = time.time()
    tenant_id, kunde, experte = await setup()
    print(f"E2E-Test-Mandant {str(tenant_id)[:8]}… angelegt")
    try:
        await run(tenant_id, kunde, experte)
    except Exception as e:
        check("Ablauf", "Durchlauf ohne Abbruch", False, f"{type(e).__name__}: {str(e)[:300]}")
    finally:
        ok, remaining = await cleanup(tenant_id, [kunde.id, experte.id])
        check("Aufraeumen", "Test-Mandant vollstaendig geloescht", ok, ", ".join(remaining))
    bad = [r for r in RESULTS if not r[2]]
    print(f"Dauer {int(time.time() - t0)} s")
    print(f"E2E-ERGEBNIS {'OK' if not bad else 'FEHLER'} ({len(bad)} von {len(RESULTS)} fehlgeschlagen)"
          if bad else f"E2E-ERGEBNIS OK ({len(RESULTS)}/{len(RESULTS)})")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
