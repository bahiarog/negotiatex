"""
Eingehende E-Mails -- bislang die fehlende Haelfte (SMTP-Versand existierte
schon in email_sender.py). Nutzt bewusst nur stdlib (imaplib + email), keine
neue Abhaengigkeit.

Matching einer Antwort auf einen Fall erfolgt AUSSCHLIESSLICH ueber die
Header Message-ID / In-Reply-To / References gegen gespeicherte ausgehende
EmailMessage.message_id-Werte -- nicht ueber den Betreff ("Allein der
Betreff genuegt nicht", Playbook A2). Jede verarbeitete Nachricht wird per
UNIQUE-Constraint auf message_id genau einmal gespeichert; ein erneuter
Poll-Lauf, der dieselbe Message-ID erneut sieht, ueberspringt sie (IntegrityError
abgefangen) -- das macht das Polling idempotent auch ohne verlaessliche
IMAP \\Seen-Flags.
"""
import email
import imaplib
import logging
import os
from email.header import decode_header
from email.utils import parseaddr

logger = logging.getLogger(__name__)

IMAP_HOST = os.getenv("IMAP_HOST", "imap.strato.de")
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
IMAP_USER = os.getenv("SMTP_USER", "")  # gleiche Mailbox wie Versand, siehe .env
IMAP_PASS = os.getenv("SMTP_PASS", "")


def _decode(value) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            out.append(text.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


def _get_body_text(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition", "")):
                try:
                    return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="replace")
                except Exception:
                    continue
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="replace")
                except Exception:
                    continue
        return ""
    try:
        return msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", errors="replace")
    except Exception:
        return str(msg.get_payload())


def fetch_unseen_messages() -> list[dict]:
    """Verbindet sich, holt alle ungelesenen Nachrichten, parst die
    relevanten Header + Body, markiert sie als gelesen und gibt eine Liste
    von dicts zurueck. Wirft nie -- bei Verbindungsfehlern wird eine leere
    Liste zurueckgegeben und der Fehler geloggt (der APScheduler-Job soll
    bei einem einzelnen Fehlschlag nicht crashen, sondern beim naechsten
    Intervall erneut versuchen)."""
    if not IMAP_USER or not IMAP_PASS:
        logger.warning("IMAP: SMTP_USER/SMTP_PASS nicht konfiguriert -- ueberspringe Poll.")
        return []

    results: list[dict] = []
    try:
        conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        conn.login(IMAP_USER, IMAP_PASS)
        conn.select("INBOX")

        status, data = conn.search(None, "UNSEEN")
        if status != "OK":
            logger.warning(f"IMAP search fehlgeschlagen: {status}")
            conn.logout()
            return []

        ids = data[0].split()
        for num in ids:
            status, msg_data = conn.fetch(num, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            raw_bytes = msg_data[0][1]
            msg = email.message_from_bytes(raw_bytes)

            message_id = (msg.get("Message-ID") or "").strip()
            if not message_id:
                # Keine Message-ID -> kann nicht zuverlaessig zugeordnet werden,
                # trotzdem zur Sichtung aufnehmen (wird im Router als
                # unknown_sender/Review behandelt, nicht verworfen).
                logger.warning("IMAP: eingehende Nachricht ohne Message-ID gefunden.")

            from_name, from_addr = parseaddr(msg.get("From", ""))
            to_name, to_addr = parseaddr(msg.get("To", ""))

            results.append({
                "message_id": message_id,
                "in_reply_to": (msg.get("In-Reply-To") or "").strip() or None,
                "references_header": msg.get("References") or None,
                "from_addr": from_addr.strip().lower(),
                "to_addr": to_addr.strip().lower(),
                "subject": _decode(msg.get("Subject")),
                "body_text": _get_body_text(msg),
                "raw_source": raw_bytes.decode("utf-8", errors="replace"),
            })
            # Als gelesen markieren -> verhindert, dass derselbe Poll-Zyklus
            # (oder ein spaeterer) dieselbe UNSEEN-Nachricht erneut liefert.
            conn.store(num, "+FLAGS", "\\Seen")

        conn.logout()
    except Exception:
        logger.exception("IMAP-Poll fehlgeschlagen")
        return results

    return results
