"""
External API key management — create, list, revoke API keys.
API keys have format: ntx_<32 random chars>
Stored as SHA256 hash, never in plaintext after creation.
"""
import os, hashlib, secrets, logging
from datetime import datetime
from fastapi import APIRouter, Request, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from database import get_db
from models import APIKey, Offer, AICheck
import jwt as pyjwt

router = APIRouter()
logger = logging.getLogger(__name__)
SECRET = os.getenv("SECRET_KEY", "negotiatex-secret-2025-change-in-prod")

def _get_user(request: Request) -> dict:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Unauthorized")
    try:
        return pyjwt.decode(auth[7:], SECRET, algorithms=["HS256"])
    except Exception:
        raise HTTPException(401, "Invalid token")

def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()

async def verify_api_key(api_key: str, db: AsyncSession) -> dict | None:
    """Verify an API key and return user info, or None if invalid."""
    key_hash = _hash_key(api_key)
    r = await db.execute(select(APIKey).where(APIKey.key_hash == key_hash, APIKey.is_active == True))
    record = r.scalar_one_or_none()
    if not record:
        return None
    record.last_used = datetime.utcnow()
    record.requests_today = (record.requests_today or 0) + 1
    await db.commit()
    return {"user_id": str(record.user_id), "key_id": str(record.id)}

class CreateKeyRequest(BaseModel):
    name: str

@router.get("/keys")
async def list_keys(request: Request, db: AsyncSession = Depends(get_db)):
    user = _get_user(request)
    import uuid as _uuid
    user_id = _uuid.UUID(user["id"])
    r = await db.execute(select(APIKey).where(APIKey.user_id == user_id, APIKey.is_active == True))
    keys = r.scalars().all()
    return [{"id": str(k.id), "name": k.name, "prefix": k.key_prefix,
             "last_used": k.last_used.isoformat() if k.last_used else None,
             "requests_today": k.requests_today, "created_at": k.created_at.isoformat()} for k in keys]

@router.post("/keys")
async def create_key(req: CreateKeyRequest, request: Request, db: AsyncSession = Depends(get_db)):
    user = _get_user(request)
    import uuid as _uuid
    user_id = _uuid.UUID(user["id"])
    raw_key = "ntx_" + secrets.token_urlsafe(32)
    key_hash = _hash_key(raw_key)
    key_prefix = raw_key[:12]
    db.add(APIKey(user_id=user_id, name=req.name, key_hash=key_hash, key_prefix=key_prefix))
    await db.commit()
    return {"key": raw_key, "prefix": key_prefix, "name": req.name,
            "warning": "Speichern Sie diesen Key — er wird NICHT erneut angezeigt."}

@router.delete("/keys/{key_id}")
async def revoke_key(key_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    user = _get_user(request)
    import uuid as _uuid
    r = await db.execute(select(APIKey).where(APIKey.id == _uuid.UUID(key_id)))
    key = r.scalar_one_or_none()
    if not key or str(key.user_id) != user["id"]:
        raise HTTPException(404, "Key not found")
    key.is_active = False
    await db.commit()
    return {"revoked": key_id}

# ── Public v1 API endpoints (authenticated via API key) ──────────────────────

@router.get("/v1/offers")
async def v1_list_offers(request: Request, db: AsyncSession = Depends(get_db)):
    """External API: list analyzed offers for the authenticated API key's user."""
    api_key = request.headers.get("X-API-Key", "")
    if not api_key:
        raise HTTPException(401, "X-API-Key header required")
    user_info = await verify_api_key(api_key, db)
    if not user_info:
        raise HTTPException(401, "Invalid or inactive API key")
    r = await db.execute(
        select(Offer).where(Offer.status == "analyzed").limit(50)
    )
    offers = r.scalars().all()
    return [{"id": str(o.id), "title": o.title, "category": o.category,
             "total_net": o.total_net, "ai_score": o.ai_score,
             "status": o.status.value if o.status else None,
             "created_at": o.created_at.isoformat()} for o in offers]

@router.get("/v1/offers/{offer_id}/analysis")
async def v1_get_analysis(offer_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """External API: get full AI analysis for an offer."""
    api_key = request.headers.get("X-API-Key", "")
    if not api_key:
        raise HTTPException(401, "X-API-Key header required")
    user_info = await verify_api_key(api_key, db)
    if not user_info:
        raise HTTPException(401, "Invalid or inactive API key")
    import uuid as _uuid
    r = await db.execute(select(Offer).where(Offer.id == _uuid.UUID(offer_id)))
    offer = r.scalar_one_or_none()
    if not offer:
        raise HTTPException(404, "Offer not found")
    cr = await db.execute(select(AICheck).where(AICheck.offer_id == _uuid.UUID(offer_id)))
    checks = cr.scalars().all()
    return {
        "offer_id": offer_id, "title": offer.title, "category": offer.category,
        "total_net": offer.total_net, "ai_score": offer.ai_score,
        "checks": [{"type": c.check_type.value, "score": c.score,
                    "critical": c.critical_count, "warnings": c.warning_count,
                    "result": c.result} for c in checks]
    }
