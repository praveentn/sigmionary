"""
external_leaderboard.py — Sends earned points to one or more external leaderboard APIs.

Configuration is driven entirely by environment variables so new services can be
added without touching code:

  EXTERNAL_LEADERBOARDS=SIGMAFEUD,NAVI        # comma-separated service names
  SIGMAFEUD_ENABLED=true
  SIGMAFEUD_URL=https://sigmafeud-production.up.railway.app
  SIGMAFEUD_API_KEY=your_key_here
  SIGMAFEUD_GUILDS=976816967161892976,1096691415431516221   # empty = all guilds

  NAVI_ENABLED=false
  NAVI_URL=https://navi-api.example.com
  NAVI_API_KEY=your_key_here
  NAVI_GUILDS=                                # empty = all guilds

Each service must expose POST /api/v1/points accepting:
  { user_id, guild_id, username, points, game_id }
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

import aiohttp

log = logging.getLogger("sigmionary.external_leaderboard")


@dataclass
class _ServiceConfig:
    name: str
    url: str
    api_key: str
    guilds: set[int] = field(default_factory=set)   # empty = all guilds allowed


def _load_services() -> list[_ServiceConfig]:
    raw = os.getenv("EXTERNAL_LEADERBOARDS", "").strip()
    if not raw:
        return []

    services: list[_ServiceConfig] = []
    for name in (s.strip().upper() for s in raw.split(",") if s.strip()):
        enabled_raw = os.getenv(f"{name}_ENABLED", "false").strip().lower()
        if enabled_raw not in ("1", "true", "yes"):
            log.debug("External leaderboard %s is disabled — skipping", name)
            continue

        url = os.getenv(f"{name}_URL", "").strip()
        api_key = os.getenv(f"{name}_API_KEY", "").strip()
        if not url or not api_key:
            log.warning(
                "External leaderboard %s is enabled but %s_URL or %s_API_KEY is missing — skipping",
                name, name, name,
            )
            continue

        guilds_raw = os.getenv(f"{name}_GUILDS", "").strip()
        guilds: set[int] = set()
        if guilds_raw:
            for g in guilds_raw.split(","):
                g = g.strip()
                if g:
                    try:
                        guilds.add(int(g))
                    except ValueError:
                        log.warning("Invalid guild ID %r in %s_GUILDS — ignoring", g, name)

        services.append(_ServiceConfig(name=name, url=url, api_key=api_key, guilds=guilds))
        scope = f"guilds {guilds}" if guilds else "all guilds"
        log.info("External leaderboard %s registered (scope: %s)", name, scope)

    return services


# Module-level list — evaluated once at import time so the env is read at startup.
_SERVICES: list[_ServiceConfig] = _load_services()


async def post_points(
    user_id: int,
    guild_id: int,
    username: str,
    points: int,
    match_id: str | None = None,
) -> None:
    """Fire-and-forget: send earned points to every enabled external service."""
    if not _SERVICES:
        return

    for svc in _SERVICES:
        if svc.guilds and guild_id not in svc.guilds:
            continue

        headers = {"Authorization": f"Bearer {svc.api_key}"}
        payload = {
            "user_id":  user_id,
            "guild_id": guild_id,
            "username": username,
            "points":   points,
            "game_id":  match_id,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{svc.url}/api/v1/points",
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status >= 400:
                        body = await resp.text()
                        log.warning(
                            "External leaderboard %s returned HTTP %s: %s",
                            svc.name, resp.status, body[:200],
                        )
                    else:
                        data = await resp.json()
                        log.debug("External leaderboard %s accepted points: %s", svc.name, data)
        except Exception:
            log.exception("Failed to post points to external leaderboard %s", svc.name)
