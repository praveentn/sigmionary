import time
import logging
import aiosqlite
from pathlib import Path

log = logging.getLogger("sigmionary")

DB_PATH = Path(__file__).parent.parent / "sigmionary.db"

_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS game_sessions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    started_by   INTEGER NOT NULL,
    started_at   REAL    NOT NULL,
    ended_at     REAL,
    total_rounds INTEGER DEFAULT 0,
    status       TEXT    DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS round_answers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    INTEGER NOT NULL,
    guild_id      INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    question_item TEXT    NOT NULL,
    points        INTEGER NOT NULL,
    hint_level    INTEGER NOT NULL,
    response_time REAL    NOT NULL,
    streak        INTEGER NOT NULL DEFAULT 1,
    answered_at   REAL    NOT NULL,
    FOREIGN KEY (session_id) REFERENCES game_sessions(id)
);

CREATE TABLE IF NOT EXISTS user_stats (
    guild_id       INTEGER NOT NULL,
    user_id        INTEGER NOT NULL,
    total_points   INTEGER DEFAULT 0,
    total_correct  INTEGER DEFAULT 0,
    games_played   INTEGER DEFAULT 0,
    best_streak    INTEGER DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_round_guild ON round_answers (guild_id, session_id);
CREATE INDEX IF NOT EXISTS idx_stats_guild ON user_stats (guild_id, total_points DESC);
"""


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(_SCHEMA)
        await db.commit()
    log.info("Database ready: %s", DB_PATH)


async def create_session(guild_id: int, started_by: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO game_sessions (guild_id, started_by, started_at) VALUES (?, ?, ?)",
            (guild_id, started_by, time.time()),
        )
        await db.commit()
        return cur.lastrowid


async def end_session(session_id: int, total_rounds: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE game_sessions SET ended_at=?, status='ended', total_rounds=? WHERE id=?",
            (time.time(), total_rounds, session_id),
        )
        await db.commit()


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
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO round_answers
               (session_id, guild_id, user_id, question_item,
                points, hint_level, response_time, streak, answered_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id, guild_id, user_id, question_item,
                points, hint_level, response_time, streak, time.time(),
            ),
        )
        await db.execute(
            """INSERT INTO user_stats (guild_id, user_id, total_points, total_correct, games_played, best_streak)
               VALUES (?, ?, ?, 1, 0, ?)
               ON CONFLICT(guild_id, user_id) DO UPDATE SET
                   total_points  = total_points  + excluded.total_points,
                   total_correct = total_correct + 1,
                   best_streak   = MAX(best_streak, excluded.best_streak)""",
            (guild_id, user_id, points, streak),
        )
        await db.commit()


async def increment_games_played(guild_id: int, user_ids: list[int]) -> None:
    if not user_ids:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        for uid in user_ids:
            await db.execute(
                """INSERT INTO user_stats (guild_id, user_id, total_points, total_correct, games_played, best_streak)
                   VALUES (?, ?, 0, 0, 1, 0)
                   ON CONFLICT(guild_id, user_id) DO UPDATE SET games_played = games_played + 1""",
                (guild_id, uid),
            )
        await db.commit()


async def get_leaderboard(guild_id: int, limit: int = 10) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT user_id, total_points, total_correct, best_streak, games_played
               FROM user_stats
               WHERE guild_id = ?
               ORDER BY total_points DESC
               LIMIT ?""",
            (guild_id, limit),
        )
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_user_stats(guild_id: int, user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT user_id, total_points, total_correct, best_streak, games_played
               FROM user_stats WHERE guild_id = ? AND user_id = ?""",
            (guild_id, user_id),
        )
        row = await cur.fetchone()
    return dict(row) if row else None


async def get_user_rank(guild_id: int, user_id: int) -> int | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """SELECT COUNT(*) + 1 FROM user_stats
               WHERE guild_id = ? AND total_points > (
                   SELECT COALESCE(total_points, -1) FROM user_stats
                   WHERE guild_id = ? AND user_id = ?
               )""",
            (guild_id, guild_id, user_id),
        )
        row = await cur.fetchone()
    return row[0] if row else None
