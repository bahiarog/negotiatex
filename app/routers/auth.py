import os, uuid, logging
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, Column, String, Boolean, DateTime
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
from pydantic import BaseModel
import hashlib, secrets, json
from database import get_db, Base

logger = logging.getLogger(__name__)
router = APIRouter()

ADMIN_EMAIL = "bahiarog@me.com"
SECRET = os.getenv("SECRET_KEY", "negotiatex-secret-2025-change-in-prod")
TURNSTILE_SITE_KEY = os.getenv("TURNSTILE_SITE_KEY", "").strip()
TURNSTILE_SECRET_KEY = os.getenv("TURNSTILE_SECRET_KEY", "").strip()


async def verify_turnstile(token: Optional[str], remote_ip: Optional[str] = None) -> None:
    """Verify a Cloudflare Turnstile token. Raises HTTPException on failure.
    If TURNSTILE_SECRET_KEY is not configured, verification is skipped (logged
    as a warning) so registration never silently breaks before the key is set up."""
    if not TURNSTILE_SECRET_KEY:
        logger.warning("TURNSTILE_SECRET_KEY not configured — skipping captcha verification.")
        return
    if not token:
        raise HTTPException(400, "Bitte Captcha bestätigen.")
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            data = {"secret": TURNSTILE_SECRET_KEY, "response": token}
            if remote_ip:
                data["remoteip"] = remote_ip
            resp = await client.post("https://challenges.cloudflare.com/turnstile/v0/siteverify", data=data)
            result = resp.json()
        if not result.get("success"):
            logger.warning(f"Turnstile verification failed: {result.get('error-codes')}")
            raise HTTPException(400, "Captcha-Prüfung fehlgeschlagen. Bitte erneut versuchen.")
    except HTTPException:
        raise
    except Exception:
        logger.exception("Turnstile verification request failed")
        raise HTTPException(503, "Captcha-Dienst momentan nicht erreichbar. Bitte später erneut versuchen.")

# -- User Model --
class User(Base):
    __tablename__ = "users"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255))
    email = Column(String(255), unique=True, nullable=False)
    company_name = Column(String(255))
    password_hash = Column(String(255))
    is_admin = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    plan = Column(String(50), default="free")
    created_at = Column(DateTime, server_default=func.now())

# -- Helpers --
def hash_password(password: str) -> str:
    salt = SECRET.encode()
    return hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 100000).hex()

def make_token(user_id: str, email: str, is_admin: bool) -> str:
    import time, jwt as pyjwt
    payload = {"id": user_id, "email": email, "is_admin": is_admin, "exp": int(time.time()) + 86400 * 30, "iat": int(time.time())}
    return pyjwt.encode(payload, SECRET, algorithm="HS256")

def user_to_dict(user):
    return {"id": str(user.id), "name": user.name, "email": user.email,
            "company_name": user.company_name, "is_admin": user.is_admin,
            "plan": user.plan}

# -- Schemas --
# -- Password reset token --
class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), nullable=False)
    token_hash = Column(String(64), nullable=False, unique=True)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())


class RegisterPayload(BaseModel):
    name: str; email: str; company_name: Optional[str] = None; password: str
    turnstile_token: Optional[str] = None

class LoginPayload(BaseModel):
    email: str; password: str

class ForgotPasswordPayload(BaseModel):
    email: str

class ResetPasswordPayload(BaseModel):
    token: str; password: str

# -- Endpoints --
@router.get("/config")
async def auth_config():
    """Public, non-secret config the frontend needs (e.g. captcha site key)."""
    return {"turnstile_site_key": TURNSTILE_SITE_KEY or None}


@router.post("/register")
async def register(payload: RegisterPayload, request: Request, db: AsyncSession = Depends(get_db)):
    await verify_turnstile(payload.turnstile_token, request.client.host if request.client else None)

    r = await db.execute(select(User).where(User.email == payload.email.lower()))
    if r.scalar_one_or_none():
        raise HTTPException(400, "E-Mail bereits registriert.")
    if len(payload.password) < 8:
        raise HTTPException(400, "Passwort muss mindestens 8 Zeichen haben.")
    user = User(
        name=payload.name,
        email=payload.email.lower(),
        company_name=payload.company_name,
        password_hash=hash_password(payload.password),
        is_admin=(payload.email.lower() == ADMIN_EMAIL),
        plan="paid" if payload.email.lower() == ADMIN_EMAIL else "free",
    )
    db.add(user); await db.commit(); await db.refresh(user)
    token = make_token(str(user.id), user.email, user.is_admin)

    try:
        from services.email_sender import send_negotiation_email
        send_negotiation_email(
            ADMIN_EMAIL,
            f"Neue Registrierung: {user.name} ({user.email})",
            (
                f"Ein neuer Nutzer hat sich bei NegotiateX registriert:\n\n"
                f"Name: {user.name}\n"
                f"E-Mail: {user.email}\n"
                f"Unternehmen: {user.company_name or '—'}\n"
                f"Registriert am: {user.created_at}\n"
            ),
            from_name="NegotiateX System",
        )
    except Exception:
        logger.exception("Registration notification email failed to send")

    return {"access_token": token, "user": user_to_dict(user)}

@router.post("/login")
async def login(payload: LoginPayload, db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(User).where(User.email == payload.email.lower()))
    user = r.scalar_one_or_none()
    if not user or user.password_hash != hash_password(payload.password):
        raise HTTPException(401, "E-Mail oder Passwort falsch.")
    if not user.is_active:
        raise HTTPException(403, "Konto deaktiviert. Bitte kontaktieren Sie uns.")
    token = make_token(str(user.id), user.email, user.is_admin)
    return {"access_token": token, "user": user_to_dict(user)}

@router.post("/forgot-password")
async def forgot_password(payload: ForgotPasswordPayload, db: AsyncSession = Depends(get_db)):
    """Always returns a generic success message, regardless of whether the
    email exists, so attackers cannot enumerate registered addresses."""
    generic_msg = {"message": "Falls ein Konto mit dieser E-Mail existiert, haben wir einen Link zum Zurücksetzen gesendet."}

    r = await db.execute(select(User).where(User.email == payload.email.lower()))
    user = r.scalar_one_or_none()
    if not user:
        return generic_msg

    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    expires_at = datetime.utcnow() + timedelta(hours=1)

    reset = PasswordResetToken(user_id=user.id, token_hash=token_hash, expires_at=expires_at)
    db.add(reset)
    await db.commit()

    from services.email_sender import send_negotiation_email
    reset_link = f"https://negotiatex.ai/reset-password?token={raw_token}"
    body = (
        f"Hallo {user.name or ''},\n\n"
        f"Sie haben eine Passwort-Zurücksetzung für Ihr NegotiateX-Konto angefordert.\n"
        f"Klicken Sie auf den folgenden Link, um ein neues Passwort zu vergeben "
        f"(gültig für 1 Stunde):\n\n{reset_link}\n\n"
        f"Falls Sie das nicht angefordert haben, ignorieren Sie diese E-Mail einfach."
    )
    try:
        send_negotiation_email(user.email, "Passwort zurücksetzen — NegotiateX.ai", body)
    except Exception:
        logger.exception("Password reset email failed to send")

    return generic_msg


@router.post("/reset-password")
async def reset_password(payload: ResetPasswordPayload, db: AsyncSession = Depends(get_db)):
    if len(payload.password) < 8:
        raise HTTPException(400, "Passwort muss mindestens 8 Zeichen haben.")

    token_hash = hashlib.sha256(payload.token.encode()).hexdigest()
    r = await db.execute(select(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash))
    reset = r.scalar_one_or_none()

    if not reset or reset.used_at is not None or reset.expires_at < datetime.utcnow():
        raise HTTPException(400, "Der Link ist ungültig oder abgelaufen. Bitte fordern Sie einen neuen an.")

    r2 = await db.execute(select(User).where(User.id == reset.user_id))
    user = r2.scalar_one_or_none()
    if not user:
        raise HTTPException(404, "Benutzer nicht gefunden.")

    user.password_hash = hash_password(payload.password)
    reset.used_at = datetime.utcnow()
    await db.commit()

    return {"message": "Passwort erfolgreich geändert. Sie können sich jetzt anmelden."}


@router.get("/me")
async def me(token: str, db: AsyncSession = Depends(get_db)):
    import jwt as pyjwt, time
    try:
        raw = token.replace("Bearer ", "").strip()
        payload = pyjwt.decode(raw, SECRET, algorithms=["HS256"])
        r = await db.execute(select(User).where(User.email == payload["email"]))
        user = r.scalar_one_or_none()
        if not user: raise HTTPException(404, "User nicht gefunden.")
        return user_to_dict(user)
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(401, "Token abgelaufen.")
    except Exception as e:
        raise HTTPException(401, "Ungültiger Token.")


def verify_token(token: str) -> dict:
    """Decode and validate Bearer token. Returns payload or raises HTTPException."""
    import jwt as pyjwt
    from fastapi import HTTPException
    try:
        raw = token.replace("Bearer ", "").strip()
        payload = pyjwt.decode(raw, SECRET, algorithms=["HS256"])
        return payload
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token abgelaufen.")
    except Exception:
        raise HTTPException(status_code=401, detail="Ungültiger Token.")
