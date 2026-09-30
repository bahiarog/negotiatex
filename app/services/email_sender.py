"""
Email service — sends negotiation emails via SMTP.
Configure via env vars: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM
Falls back to logging if SMTP is not configured.
"""
import os, smtplib, logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

logger = logging.getLogger(__name__)

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", "noreply@negotiatex.ai")

def send_negotiation_email(to_email: str, subject: str, body: str, from_name: str = "NegotiateX.ai") -> dict:
    """
    Send a negotiation email. Returns {"sent": bool, "message": str}.
    If SMTP is not configured, logs the email and returns sent=False with a preview.
    """
    if not SMTP_HOST or not SMTP_USER:
        logger.info(f"[EMAIL NOT SENT — SMTP not configured] To: {to_email} | Subject: {subject}")
        return {
            "sent": False,
            "message": "SMTP nicht konfiguriert. E-Mail wurde NICHT gesendet.",
            "preview": {"to": to_email, "subject": subject, "body_preview": body[:200]}
        }

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{from_name} <{SMTP_FROM}>"
        msg["To"] = to_email

        # Plain text
        msg.attach(MIMEText(body, "plain", "utf-8"))
        # HTML version
        html_body = body.replace("\n", "<br>")
        msg.attach(MIMEText(f"<html><body style='font-family:Arial,sans-serif;line-height:1.6'>{html_body}</body></html>", "html", "utf-8"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)

        logger.info(f"Email sent to {to_email}: {subject}")
        return {"sent": True, "message": f"E-Mail erfolgreich an {to_email} gesendet."}

    except Exception as e:
        logger.error(f"Email send failed: {e}")
        return {"sent": False, "message": f"Fehler beim Senden: {str(e)}"}
