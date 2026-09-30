"""
Outbound webhook system -- notify external systems (ERP, Slack, etc.)
when key events happen in NegotiateX.
"""
import os, logging, httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()
logger = logging.getLogger(__name__)

WEBHOOK_URLS = [u.strip() for u in os.getenv("WEBHOOK_URL", "").split(",") if u.strip()]

async def fire_webhook(event: str, payload: dict):
    """Send webhook to all configured URLs. Fire-and-forget."""
    if not WEBHOOK_URLS:
        return
    import datetime as _dt
    body = {"event": event, "timestamp": _dt.datetime.utcnow().isoformat(), "data": payload}
    async with httpx.AsyncClient(timeout=10) as client:
        for url in WEBHOOK_URLS:
            try:
                r = await client.post(url, json=body, headers={"Content-Type": "application/json", "X-NegotiateX-Event": event})
                logger.info(f"Webhook {event} -> {url}: {r.status_code}")
            except Exception as e:
                logger.warning(f"Webhook {event} -> {url} failed: {e}")

class WebhookTest(BaseModel):
    url: str

@router.post("/test")
async def test_webhook(req: WebhookTest):
    """Test a webhook URL with a sample payload."""
    import datetime as _dt
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(req.url, json={
                "event": "test",
                "timestamp": _dt.datetime.utcnow().isoformat(),
                "data": {"message": "NegotiateX webhook test successful"}
            })
        return {"status": "sent", "response_code": r.status_code}
    except Exception as e:
        raise HTTPException(400, f"Webhook test failed: {str(e)}")

@router.get("/status")
async def webhook_status():
    return {"configured_webhooks": len(WEBHOOK_URLS), "urls": [u[:30] + "..." if len(u) > 30 else u for u in WEBHOOK_URLS]}
