import os, uuid, logging
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
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
class RegisterPayload(BaseModel):
    name: str; email: str; company_name: Optional[str] = None; password: str

class LoginPayload(BaseModel):
    email: str; password: str

# -- Endpoints --
@router.post("/register")
async def register(payload: RegisterPayload, db: AsyncSession = Depends(get_db)):
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
