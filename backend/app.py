"""FastAPI server for The Panel.

Exposes a single WebRTC signaling endpoint (/api/offer). The browser sends an SDP
offer; we spin up a Pipecat bot for that connection and return the SDP answer.
"""

import asyncio

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from pydantic import BaseModel

from panel.bot import run_bot
from panel.config import config
from panel.voices import build_voice_pool
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection

app = FastAPI(title="The Panel")

# Dev: allow the Vite dev server (and anything else) to call us.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ICE_SERVERS = ["stun:stun.l.google.com:19302"]

# Keep references so connections aren't garbage-collected mid-call.
_connections: dict[str, SmallWebRTCConnection] = {}


class Offer(BaseModel):
    sdp: str
    type: str
    pc_id: str | None = None


@app.get("/api/health")
async def health():
    return {"ok": True, "missing_keys": config.missing_keys()}


@app.get("/api/voices")
async def voices():
    """Useful for debugging the available voice pool."""
    return await build_voice_pool()


@app.post("/api/offer")
async def offer(body: Offer):
    """WebRTC signaling: accept an SDP offer, start a bot, return the SDP answer.

    If the client reconnects (sends a pc_id we already know), renegotiate on the SAME
    connection so the existing bot — and the panel it built — survives the blip. Only a
    brand-new pc_id spins up a new bot.
    """
    if config.missing_keys():
        logger.warning(f"Missing keys: {config.missing_keys()}")

    # Reconnection: reuse the existing connection + bot.
    if body.pc_id and body.pc_id in _connections:
        connection = _connections[body.pc_id]
        logger.info(f"Renegotiating existing connection {body.pc_id} (keeping bot alive).")
        await connection.renegotiate(sdp=body.sdp, type=body.type)
        return connection.get_answer()

    connection = SmallWebRTCConnection(ice_servers=ICE_SERVERS)
    await connection.initialize(sdp=body.sdp, type=body.type)

    @connection.event_handler("closed")
    async def _on_closed(conn):
        _connections.pop(conn.pc_id, None)
        logger.info(f"Connection {conn.pc_id} closed.")

    # Run the bot for this connection in the background.
    asyncio.create_task(run_bot(connection))

    answer = connection.get_answer()
    _connections[answer["pc_id"]] = connection
    return answer


def main():
    import uvicorn

    missing = config.missing_keys()
    if missing:
        logger.warning(f"Starting with MISSING keys: {missing} (set them in the repo-root .env)")
    uvicorn.run(app, host="0.0.0.0", port=config.PORT)


if __name__ == "__main__":
    main()
