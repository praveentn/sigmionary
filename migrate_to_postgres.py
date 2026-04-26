"""
migrate_to_postgres.py — One-time script to set up the PostgreSQL schema
for Sigmionary on Railway (or any Postgres host).

Usage (run once on Railway or locally against the target DB):
    DATABASE_URL=postgresql://... python migrate_to_postgres.py

The bot itself calls database.init_db() on every startup, which is
idempotent (CREATE TABLE IF NOT EXISTS).  This script is only needed
if you want to verify the schema before starting the bot for the first time,
or if you want to optionally migrate existing SQLite data.

Fresh start:  just run this script — no old data is touched.
"""

from __future__ import annotations

import asyncio
import os
import sys

# ── Optional: set DATABASE_URL here for local testing ─────────────────────────
# (normally provided by Railway's environment)

import asyncpg
from dotenv import load_dotenv

load_dotenv()


# ── Schema (mirrors utils/database.py) ────────────────────────────────────────

_SCHEMA = [
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


async def setup_schema(dsn: str) -> None:
    print(f"Connecting to PostgreSQL…")
    conn = await asyncpg.connect(dsn=dsn)
    try:
        print("Creating tables and indexes…")
        for stmt in _SCHEMA:
            await conn.execute(stmt)
            # Show the first line of each statement as a progress indicator
            preview = stmt.strip().splitlines()[0]
            print(f"  ✓  {preview[:80]}")
        print("\n✅  Schema ready — all tables and indexes are in place.")
        print("   You can now start the bot with: python bot.py")
    finally:
        await conn.close()


# ── Optional: migrate SQLite → PostgreSQL ─────────────────────────────────────
# Uncomment and adjust the path below if you want to port existing SQLite data.
# By default the bot starts with a fresh PostgreSQL database.

async def migrate_sqlite(dsn: str, sqlite_path: str) -> None:
    """
    Copy all rows from a local SQLite database into PostgreSQL.
    Run AFTER setup_schema() so tables already exist.
    """
    try:
        import aiosqlite
    except ImportError:
        print("aiosqlite not installed — skipping SQLite migration.")
        return

    from pathlib import Path
    if not Path(sqlite_path).exists():
        print(f"SQLite file not found: {sqlite_path} — skipping migration.")
        return

    print(f"\nMigrating SQLite data from {sqlite_path}…")
    pg = await asyncpg.connect(dsn=dsn)

    async with aiosqlite.connect(sqlite_path) as sq:
        sq.row_factory = aiosqlite.Row

        # game_sessions
        rows = await (await sq.execute("SELECT * FROM game_sessions")).fetchall()
        for r in rows:
            await pg.execute(
                "INSERT INTO game_sessions (id, guild_id, started_by, started_at, ended_at, total_rounds, status) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7) ON CONFLICT (id) DO NOTHING",
                r["id"], r["guild_id"], r["started_by"], r["started_at"],
                r["ended_at"], r["total_rounds"], r["status"],
            )
        print(f"  ✓  game_sessions: {len(rows)} row(s)")

        # round_answers
        rows = await (await sq.execute("SELECT * FROM round_answers")).fetchall()
        for r in rows:
            await pg.execute(
                "INSERT INTO round_answers "
                "(id, session_id, guild_id, user_id, question_item, points, hint_level, response_time, streak, answered_at) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) ON CONFLICT (id) DO NOTHING",
                r["id"], r["session_id"], r["guild_id"], r["user_id"], r["question_item"],
                r["points"], r["hint_level"], r["response_time"], r["streak"], r["answered_at"],
            )
        print(f"  ✓  round_answers: {len(rows)} row(s)")

        # user_stats
        rows = await (await sq.execute("SELECT * FROM user_stats")).fetchall()
        for r in rows:
            await pg.execute(
                "INSERT INTO user_stats (guild_id, user_id, total_points, total_correct, games_played, best_streak) "
                "VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT (guild_id, user_id) DO NOTHING",
                r["guild_id"], r["user_id"], r["total_points"],
                r["total_correct"], r["games_played"], r["best_streak"],
            )
        print(f"  ✓  user_stats: {len(rows)} row(s)")

        # guild_question_seen
        rows = await (await sq.execute("SELECT * FROM guild_question_seen")).fetchall()
        for r in rows:
            await pg.execute(
                "INSERT INTO guild_question_seen (guild_id, question_id, seen_at) "
                "VALUES ($1,$2,$3) ON CONFLICT (guild_id, question_id) DO NOTHING",
                r["guild_id"], r["question_id"], r["seen_at"],
            )
        print(f"  ✓  guild_question_seen: {len(rows)} row(s)")

    await pg.close()
    print("✅  SQLite migration complete.")


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    dsn = os.getenv("DATABASE_URL", "").strip()
    if not dsn:
        print("ERROR: DATABASE_URL environment variable is not set.")
        print("  Set it in your .env file or Railway environment variables.")
        sys.exit(1)

    await setup_schema(dsn)

    # ── To also migrate SQLite data, uncomment the lines below ────────────────
    # sqlite_path = "sigmionary.db"   # path to your existing SQLite file
    # await migrate_sqlite(dsn, sqlite_path)


if __name__ == "__main__":
    asyncio.run(main())
