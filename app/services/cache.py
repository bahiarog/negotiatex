"""
Redis cache for expensive AI analysis results.
Key: SHA256(offer_text + category) - avoids re-running analysis on duplicate uploads.
TTL: 24h for analysis results, 1h for benchmark data.
"""
import hashlib, json, logging
import redis.asyncio as aioredis

logger = logging.getLogger(__name__)
_redis = None

async def get_redis():
    global _redis
    if _redis is None:
        import os
        url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        try:
            _redis = aioredis.from_url(url, decode_responses=True)
            await _redis.ping()
        except Exception as e:
            logger.warning(f"Redis unavailable: {e}")
            _redis = None
    return _redis

def _analysis_key(offer_text: str, category: str, total_net: float) -> str:
    h = hashlib.sha256(f"{offer_text[:2000]}{category}{total_net:.2f}".encode()).hexdigest()
    return f"nx:analysis:{h}"

async def get_cached_analysis(offer_text: str, category: str, total_net: float):
    r = await get_redis()
    if not r:
        return None
    try:
        val = await r.get(_analysis_key(offer_text, category, total_net))
        return json.loads(val) if val else None
    except Exception as e:
        logger.warning(f"Cache get error: {e}")
        return None

async def set_cached_analysis(offer_text: str, category: str, total_net: float, result: dict):
    r = await get_redis()
    if not r:
        return
    try:
        key = _analysis_key(offer_text, category, total_net)
        await r.setex(key, 86400, json.dumps(result))
    except Exception as e:
        logger.warning(f"Cache set error: {e}")

async def invalidate_analysis(offer_text: str, category: str, total_net: float):
    r = await get_redis()
    if not r:
        return
    try:
        await r.delete(_analysis_key(offer_text, category, total_net))
    except Exception:
        pass
