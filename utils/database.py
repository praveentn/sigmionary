"""
database.py — PostgreSQL (asyncpg) data layer for Sigmionary.

All Discord IDs are stored as BIGINT. Every query is scoped to guild_id
so no user data leaks between servers.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import date

import asyncpg

log = logging.getLogger("sigmionary")

_pool: asyncpg.Pool | None = None

# ── Connection pool ────────────────────────────────────────────────────────────

async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        dsn = os.getenv("DATABASE_URL")
        if not dsn:
            raise RuntimeError("DATABASE_URL environment variable is not set")
        _pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=10)
    return _pool


# ── Schema ─────────────────────────────────────────────────────────────────────

_SCHEMA_STMTS = [
    """CREATE TABLE IF NOT EXISTS game_sessions (
        id           BIGSERIAL PRIMARY KEY,
        guild_id     BIGINT NOT NULL,
        started_by   BIGINT NOT NULL,
        started_at   DOUBLE PRECISION NOT NULL,
        ended_at     DOUBLE PRECISION,
        total_rounds INTEGER DEFAULT 0,
        status       TEXT DEFAULT 'active'
    )""",
    """CREATE TABLE IF NOT EXISTS round_answers (
        id            BIGSERIAL PRIMARY KEY,
        session_id    BIGINT NOT NULL REFERENCES game_sessions(id),
        guild_id      BIGINT NOT NULL,
        user_id       BIGINT NOT NULL,
        question_item TEXT NOT NULL,
        points        INTEGER NOT NULL,
        hint_level    INTEGER NOT NULL,
        response_time DOUBLE PRECISION NOT NULL,
        streak        INTEGER NOT NULL DEFAULT 1,
        answered_at   DOUBLE PRECISION NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS user_stats (
        guild_id      BIGINT NOT NULL,
        user_id       BIGINT NOT NULL,
        total_points  INTEGER DEFAULT 0,
        total_correct INTEGER DEFAULT 0,
        games_played  INTEGER DEFAULT 0,
        best_streak   INTEGER DEFAULT 0,
        PRIMARY KEY (guild_id, user_id)
    )""",
    """CREATE TABLE IF NOT EXISTS guild_question_seen (
        guild_id    BIGINT NOT NULL,
        question_id TEXT NOT NULL,
        seen_at     DOUBLE PRECISION NOT NULL,
        PRIMARY KEY (guild_id, question_id)
    )""",
    """CREATE TABLE IF NOT EXISTS guild_reminder_config (
        guild_id         BIGINT PRIMARY KEY,
        channel_id       BIGINT NOT NULL DEFAULT 0,
        timezone         TEXT NOT NULL DEFAULT 'UTC',
        enabled          BOOLEAN NOT NULL DEFAULT TRUE,
        last_reminded_on DATE
    )""",
    "CREATE INDEX IF NOT EXISTS idx_round_guild ON round_answers (guild_id, session_id)",
    "CREATE INDEX IF NOT EXISTS idx_stats_guild ON user_stats (guild_id, total_points DESC)",
]


async def init_db() -> None:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        for stmt in _SCHEMA_STMTS:
            await conn.execute(stmt)
    log.info("Database ready (PostgreSQL)")


# ── Game sessions ──────────────────────────────────────────────────────────────

async def create_session(guild_id: int, started_by: int) -> int:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO game_sessions (guild_id, started_by, started_at) "
            "VALUES ($1, $2, $3) RETURNING id",
            guild_id, started_by, time.time(),
        )
        return row["id"]


async def end_session(session_id: int, total_rounds: int) -> None:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE game_sessions SET ended_at=$1, status='ended', total_rounds=$2 WHERE id=$3",
            time.time(), total_rounds, session_id,
        )


# ── Answers & stats ────────────────────────────────────────────────────────────

async def record_answer(
    session_id: int,
    guild_id: int,
    user_id: int,
    question_item: str,
    points: int,
    hint_level: int,
    response_time: float,
    streak: int,
) -> None:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """INSERT INTO round_answers
                   (session_id, guild_id, user_id, question_item,
                    points, hint_level, response_time, streak, answered_at)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)""",
                session_id, guild_id, user_id, question_item,
                points, hint_level, response_time, streak, time.time(),
            )
            await conn.execute(
                """INSERT INTO user_stats
                       (guild_id, user_id, total_points, total_correct, games_played, best_streak)
                   VALUES ($1, $2, $3, 1, 0, $4)
                   ON CONFLICT (guild_id, user_id) DO UPDATE SET
                       total_points  = user_stats.total_points  + EXCLUDED.total_points,
                       total_correct = user_stats.total_correct + 1,
                       best_streak   = GREATEST(user_stats.best_streak, EXCLUDED.best_streak)""",
                guild_id, user_id, points, streak,
            )


async def increment_games_played(guild_id: int, user_ids: list[int]) -> None:
    if not user_ids:
        return
    pool = await _get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for uid in user_ids:
                await conn.execute(
                    """INSERT INTO user_stats
                           (guild_id, user_id, total_points, total_correct, games_played, best_streak)
                       VALUES ($1, $2, 0, 0, 1, 0)
                       ON CONFLICT (guild_id, user_id) DO UPDATE
                           SET games_played = user_stats.games_played + 1""",
                    guild_id, uid,
                )


# ── Leaderboard & stats ────────────────────────────────────────────────────────

async def get_leaderboard(guild_id: int, limit: int = 10) -> list[dict]:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT user_id, total_points, total_correct, best_streak, games_played
               FROM user_stats
               WHERE guild_id = $1
               ORDER BY total_points DESC
               LIMIT $2""",
            guild_id, limit,
        )
    return [dict(r) for r in rows]


async def get_user_stats(guild_id: int, user_id: int) -> dict | None:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT user_id, total_points, total_correct, best_streak, games_played
               FROM user_stats
               WHERE guild_id = $1 AND user_id = $2""",
            guild_id, user_id,
        )
    return dict(row) if row else None


async def get_user_rank(guild_id: int, user_id: int) -> int | None:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT COUNT(*) + 1 AS rank FROM user_stats
               WHERE guild_id = $1 AND total_points > (
                   SELECT COALESCE(total_points, -1) FROM user_stats
                   WHERE guild_id = $2 AND user_id = $3
               )""",
            guild_id, guild_id, user_id,
        )
    return int(row["rank"]) if row else None


async def get_player_ids(guild_id: int) -> list[int]:
    """Return all user IDs who have any record in this guild."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT user_id FROM user_stats WHERE guild_id = $1",
            guild_id,
        )
    return [r["user_id"] for r in rows]


# ── Question rotation ──────────────────────────────────────────────────────────

async def get_seen_question_ids(guild_id: int) -> set[str]:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT question_id FROM guild_question_seen WHERE guild_id = $1",
            guild_id,
        )
    return {r["question_id"] for r in rows}


async def mark_question_seen(guild_id: int, question_id: str) -> None:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO guild_question_seen (guild_id, question_id, seen_at)
               VALUES ($1, $2, $3)
               ON CONFLICT (guild_id, question_id) DO NOTHING""",
            guild_id, question_id, time.time(),
        )


async def reset_seen_questions(guild_id: int) -> None:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM guild_question_seen WHERE guild_id = $1",
            guild_id,
        )


# ── Reminder config ────────────────────────────────────────────────────────────

async def get_reminder_config(guild_id: int) -> dict | None:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT guild_id, channel_id, timezone, enabled, last_reminded_on
               FROM guild_reminder_config
               WHERE guild_id = $1""",
            guild_id,
        )
    return dict(row) if row else None


async def set_reminder_channel(guild_id: int, channel_id: int) -> None:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO guild_reminder_config (guild_id, channel_id, timezone, enabled)
               VALUES ($1, $2, 'UTC', TRUE)
               ON CONFLICT (guild_id) DO UPDATE SET
                   channel_id = EXCLUDED.channel_id,
                   enabled    = TRUE""",
            guild_id, channel_id,
        )


async def set_reminder_timezone(guild_id: int, tz_name: str) -> None:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO guild_reminder_config (guild_id, channel_id, timezone, enabled)
               VALUES ($1, 0, $2, FALSE)
               ON CONFLICT (guild_id) DO UPDATE SET timezone = EXCLUDED.timezone""",
            guild_id, tz_name,
        )


async def enable_reminder(guild_id: int) -> None:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE guild_reminder_config SET enabled = TRUE WHERE guild_id = $1",
            guild_id,
        )


async def disable_reminder(guild_id: int) -> None:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE guild_reminder_config SET enabled = FALSE WHERE guild_id = $1",
            guild_id,
        )


async def mark_reminder_sent(guild_id: int, sent_date: date) -> None:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE guild_reminder_config SET last_reminded_on = $1 WHERE guild_id = $2",
            sent_date, guild_id,
        )


async def get_all_reminder_configs() -> list[dict]:
    """Return all enabled reminder configs that have a valid channel set."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT guild_id, channel_id, timezone, enabled, last_reminded_on
               FROM guild_reminder_config
               WHERE enabled = TRUE AND channel_id > 0""",
        )
    return [dict(r) for r in rows]
