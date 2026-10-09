"""
Email service -- sends emails via SMTP.
Configure via env vars: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM
Falls back to logging if SMTP is not configured.

Jede Mail bekommt eine eigene Message-ID, die zurueckgegeben und von den
Aufrufern gespeichert wird. Antworten der Empfaenger verweisen per
In-Reply-To/References genau darauf -- ohne eigene ID vergibt der
Mailserver eine fremde, und Antworten lassen sich nicht mehr zuordnen.
"""
import html
import logging
import os
import smtplib
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import make_msgid
from typing import Optional

logger = logging.getLogger(__name__)

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", "noreply@negotiatex.ai")
MESSAGE_ID_DOMAIN = (SMTP_FROM.split("@", 1)[1] if "@" in SMTP_FROM else "negotiatex.ai")


def new_message_id() -> str:
    return make_msgid(domain=MESSAGE_ID_DOMAIN)


def send_negotiation_email(to_email: str, subject: str, body: str, from_name: str = "NegotiateX.ai",
                           message_id: Optional[str] = None, in_reply_to: Optional[str] = None,
                           references: Optional[str] = None, attachments: Optional[list] = None) -> dict:
    """Rueckgabe: {"sent": bool, "message": str, "message_id": str}.
    attachments: [{"filename": str, "content": bytes, "content_type": "application/pdf"}]."""
    message_id = message_id or new_message_id()
    if not SMTP_HOST or not SMTP_USER:
        logger.info(f"[EMAIL NOT SENT — SMTP not configured] To: {to_email} | Subject: {subject}")
        return {
            "sent": False, "message": "SMTP nicht konfiguriert. E-Mail wurde NICHT gesendet.", "message_id": message_id,
            "preview": {"to": to_email, "subject": subject, "body_preview": body[:200]},
        }

    try:
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(body, "plain", "utf-8"))
        html_body = html.escape(body).replace("\n", "<br>")
        alt.attach(MIMEText(f"<html><body style='font-family:Arial,sans-serif;line-height:1.6'>{html_body}</body></html>", "html", "utf-8"))
        if attachments:
            msg = MIMEMultipart("mixed")
            msg.attach(alt)
            for a in attachments:
                part = MIMEApplication(a["content"], _subtype=(a.get("content_type") or "application/octet-stream").split("/")[-1])
                part.add_header("Content-Disposition", "attachment", filename=a["filename"])
                msg.attach(part)
        else:
            msg = alt
        msg["Subject"] = subject
        msg["From"] = f"{from_name} <{SMTP_FROM}>"
        msg["To"] = to_email
        msg["Message-ID"] = message_id
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = references or in_reply_to

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)

        logger.info(f"Email sent to {to_email}: {subject} {message_id}")
        return {"sent": True, "message": f"E-Mail erfolgreich an {to_email} gesendet.", "message_id": message_id}

    except Exception as e:
        logger.error(f"Email send failed: {e}")
        return {"sent": False, "message": f"Fehler beim Senden: {str(e)}", "message_id": message_id}
