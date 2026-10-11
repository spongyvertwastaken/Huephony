"""The local HTTP API: the FastAPI app and its routers.

Spotify access lives in spotify_client.py, the playback daemon and the lights
in playback.py, saved settings in settings.py, and the routes in routers/.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from playback import playback_coordinator
from routers import auth, lights, player, settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = FastAPI(title="Huephony Backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://tauri.localhost",
        "tauri://localhost",
        "https://tauri.localhost",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(settings.router)
app.include_router(auth.router)
app.include_router(lights.router)
app.include_router(player.router)
app.add_event_handler("startup", playback_coordinator.start)
app.add_event_handler("shutdown", playback_coordinator.stop)
