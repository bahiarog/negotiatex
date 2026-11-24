"""Akzeptanztest NX-002: eigene Systemmails im Posteingang werden uebersprungen, alles andere zugeordnet.

Ohne Datenbank, ohne IMAP. Aufruf aus dem Repo-Wurzelverzeichnis:
    python app/tests/inbox_own_mail_acceptance.py
"""
import asyncio
import os
import sys
import types

os.environ["SMTP_FROM"] = "info@negotiatex.ai"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import services.email_poller as ep  # noqa: E402

FAILS = 0


def check(name, got, want):
    global FAILS
    ok = got == want
    FAILS += not ok
    print(("ok   " if ok else "FEHL ") + name + " -> " + repr(got) + ("" if ok else f"  (erwartet {want!r})"))


OWN = {"from_addr": "info@negotiatex.ai", "message_id": "<173.1.2@negotiatex.ai>", "subject": "Neues Vorhaben",
       "system_header": "1"}
RELAY = dict(OWN, message_id="<N03d522999PZ0zx.RZmta@negotiatex.ai>", subject="Relay")
UPPER = dict(OWN, from_addr="INFO@NegotiateX.ai", message_id="<1@NEGOTIATEX.AI>", subject="Gross")
WEBMAIL_REPLY = {"from_addr": "info@negotiatex.ai", "message_id": "<N9x.RZmta@negotiatex.ai>",
                 "in_reply_to": "<173.1.2@negotiatex.ai>", "subject": "AW: Anfrage / 9eaa74bf"}
WEBMAIL_NEW = {"from_addr": "info@negotiatex.ai", "message_id": "<N9y.RZmta@negotiatex.ai>", "subject": "Testangebot"}
HEADER_BUT_REPLY = dict(OWN, in_reply_to="<x@negotiatex.ai>", subject="Header+Antwort")
SUPPLIER = {"from_addr": "angebot@filmwerk.de", "message_id": "<abc@filmwerk.de>",
            "in_reply_to": "<173.1.2@negotiatex.ai>", "subject": "AW: Anfrage / 9eaa74bf", "system_header": "1"}
FOREIGN_ID = dict(OWN, message_id="<abc@gmail.com>", subject="Fremde ID")
NO_MID = dict(OWN, message_id="", subject="Ohne ID")


def main():
    check("eigene Systemmail", ep._is_own_system_mail(OWN), True)
    check("eigene Systemmail, ID vom Relay umgeschrieben", ep._is_own_system_mail(RELAY), True)
    check("Gross-/Kleinschreibung", ep._is_own_system_mail(UPPER), True)
    check("Webmail-Antwort aus dem Systempostfach", ep._is_own_system_mail(WEBMAIL_REPLY), False)
    check("Webmail-Mail aus dem Systempostfach ohne Bezug", ep._is_own_system_mail(WEBMAIL_NEW), False)
    check("Header, aber Antwortbezug", ep._is_own_system_mail(HEADER_BUT_REPLY), False)
    check("Dienstleister (auch mit gefaelschtem Header)", ep._is_own_system_mail(SUPPLIER), False)
    check("eigene Adresse, fremde Message-ID", ep._is_own_system_mail(FOREIGN_ID), False)
    check("ohne Message-ID", ep._is_own_system_mail(NO_MID), False)

    # _poll_inbox: nur fremde bzw. nicht gekennzeichnete Mails gehen in die Zuordnung
    seen = []

    async def fake_dispatch(db, m):
        seen.append(m["subject"])
        return {"matched": False}

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def rollback(self):
            pass

    class FakeTrack:
        async def __aenter__(self):
            return types.SimpleNamespace(outcome=None)

        async def __aexit__(self, *a):
            return False

    imap = types.ModuleType("services.email_imap")
    imap.fetch_unseen_messages = lambda: [OWN, SUPPLIER, RELAY, WEBMAIL_REPLY, WEBMAIL_NEW]
    dbm = types.ModuleType("database")
    dbm.AdminSessionLocal = FakeSession
    al = types.ModuleType("services.agent_log")
    al.track = lambda *a, **k: FakeTrack()
    saved = {k: sys.modules.get(k) for k in ("services.email_imap", "database", "services.agent_log")}
    sys.modules.update({"services.email_imap": imap, "database": dbm, "services.agent_log": al})
    orig = ep._dispatch_inbound_message
    ep._dispatch_inbound_message = fake_dispatch
    try:
        asyncio.run(ep._poll_inbox())
    finally:
        ep._dispatch_inbound_message = orig
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    check("zugeordnet werden", seen, [SUPPLIER["subject"], WEBMAIL_REPLY["subject"], WEBMAIL_NEW["subject"]])

    print(f"\n{FAILS} FEHLER" if FAILS else "\nALLE FAELLE BESTANDEN")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
