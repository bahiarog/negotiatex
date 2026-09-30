"""
WebSocket router — real-time analysis status updates.
Clients connect to /api/ws/offer/{offer_id} and receive JSON push messages
when the offer status changes (analyzing → analyzed).
"""
import asyncio, json, logging
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from typing import Dict, Set

router = APIRouter()
logger = logging.getLogger(__name__)

# In-memory connection registry: offer_id → set of WebSocket connections
_connections: Dict[str, Set[WebSocket]] = {}

async def broadcast(offer_id: str, message: dict):
    """Push message to all clients watching this offer."""
    sockets = _connections.get(offer_id, set()).copy()
    dead = set()
    for ws in sockets:
        try:
            await ws.send_text(json.dumps(message))
        except Exception:
            dead.add(ws)
    for ws in dead:
        _connections.get(offer_id, set()).discard(ws)

def register(offer_id: str, ws: WebSocket):
    _connections.setdefault(offer_id, set()).add(ws)

def unregister(offer_id: str, ws: WebSocket):
    _connections.get(offer_id, set()).discard(ws)

@router.websocket("/ws/offer/{offer_id}")
async def offer_ws(websocket: WebSocket, offer_id: str):
    await websocket.accept()
    register(offer_id, websocket)
    logger.info(f"WS connected for offer {offer_id}")
    try:
        while True:
            # Keep alive — client can send "ping", we reply "pong"
            data = await asyncio.wait_for(websocket.receive_text(), timeout=30)
            if data == "ping":
                await websocket.send_text("pong")
    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    finally:
        unregister(offer_id, websocket)
        logger.info(f"WS disconnected for offer {offer_id}")
