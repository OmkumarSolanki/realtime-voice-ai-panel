"""Voice assignment for the panel.

Agents are created dynamically per problem, so we can't pre-map voices to fixed roles.
Instead we build an ordered POOL of distinct Cartesia voices at startup, then assign one
voice per agent (by index) once the panel exists. We try the live Cartesia voice list and
fall back to a hardcoded set of known-good English voice IDs (verified on 2025-04-16).
"""

import httpx
from loguru import logger

from .config import config

# Verified-good Cartesia English voices, as an ordered pool (id, label, gender).
# Order alternates gender so a panel sounds varied.
FALLBACK_POOL: list[dict] = [
    {"id": "db6b0ed5-d5d3-463d-ae85-518a07d3c2b4", "label": "Skylar", "gender": "feminine"},
    {"id": "630ed21c-2c5c-41cf-9d82-10a7fd668370", "label": "Corey", "gender": "masculine"},
    {"id": "62ae83ad-4f6a-430b-af41-a9bede9286ca", "label": "Gemma", "gender": "feminine"},
    {"id": "5ee9feff-1265-424a-9d7f-8e4d431a12c7", "label": "Ronald", "gender": "masculine"},
]


async def fetch_voice_pool() -> list[dict]:
    """Fetch English voices from Cartesia. Returns [] on any failure."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://api.cartesia.ai/voices",
                headers={
                    "X-API-Key": config.CARTESIA_API_KEY,
                    "Cartesia-Version": config.CARTESIA_VERSION,
                },
                params={"limit": 50},
            )
        if resp.status_code != 200:
            logger.warning(f"Cartesia /voices returned {resp.status_code}; using fallback voices.")
            return []
        data = resp.json()
        items = data.get("data", data) if isinstance(data, dict) else data
        english = [v for v in items if (v.get("language") == "en")]
        return english
    except Exception as e:  # network, parse, etc.
        logger.warning(f"Could not fetch Cartesia voices ({e}); using fallback voices.")
        return []


def _gender_alternated(voices: list[dict]) -> list[dict]:
    """Interleave feminine/masculine/other so consecutive voices differ."""
    fem = [v for v in voices if v.get("gender") == "feminine"]
    masc = [v for v in voices if v.get("gender") == "masculine"]
    other = [v for v in voices if v.get("gender") not in ("feminine", "masculine")]
    ordered: list[dict] = []
    queues = [fem, masc, other]
    while any(queues):
        for q in queues:
            if q:
                ordered.append(q.pop(0))
    return ordered


async def build_voice_pool() -> list[dict]:
    """Return an ordered list of distinct voices ({id,label,gender}) for the panel.

    Prefers live Cartesia voices (gender-alternated for variety); falls back to the
    verified hardcoded pool. Always returns at least the fallback set.
    """
    pool = await fetch_voice_pool()
    if len(pool) < len(FALLBACK_POOL):
        return [dict(v) for v in FALLBACK_POOL]

    ordered = _gender_alternated(pool)
    result = [
        {
            "id": v.get("id"),
            "label": v.get("name", "Voice"),
            "gender": v.get("gender", "unknown"),
        }
        for v in ordered
        if v.get("id")
    ]
    if len(result) < len(FALLBACK_POOL):
        return [dict(v) for v in FALLBACK_POOL]
    logger.info(f"Built voice pool of {len(result)} live Cartesia voices.")
    return result


def assign_voices(expert_ids: list[str], pool: list[dict]) -> dict[str, dict]:
    """Map each agent id to a distinct voice from the pool (wraps if more agents than voices)."""
    pool = pool or [dict(v) for v in FALLBACK_POOL]
    return {eid: dict(pool[i % len(pool)]) for i, eid in enumerate(expert_ids)}
