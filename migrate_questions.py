#!/usr/bin/env python3
"""
migrate_questions.py — One-time migration: local CSV + images → PostgreSQL + Railway Volume.

Run this once against your Railway database after deploying the new schema:

    DATABASE_URL=<railway_url> IMAGES_PATH=/sigmionary/images python migrate_questions.py

On local dev, set IMAGES_PATH to a local folder (e.g. /tmp/sigmionary-images).
The script is idempotent — re-running skips already-imported items.
"""

import asyncio
import csv
import os
import shutil
from pathlib import Path

import asyncpg
from dotenv import load_dotenv
from rapidfuzz import fuzz

load_dotenv()

QUESTIONS_DIR = Path(__file__).parent / "questions"
DATA_CSV      = QUESTIONS_DIR / "data.csv"
IMAGES_PATH   = Path(os.getenv("IMAGES_PATH", "/sigmionary/images"))
_IMG_EXTS     = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def _find_item_folder(category: str, item: str) -> Path | None:
    cat_dir = QUESTIONS_DIR / category
    if not cat_dir.is_dir():
        return None
    best_score, best_folder = 0, None
    for entry in cat_dir.iterdir():
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        score = fuzz.ratio(entry.name.lower(), item.lower())
        if score > best_score:
            best_score, best_folder = score, entry
    return best_folder if best_score >= 70 else None


def _sorted_images(folder: Path) -> list[Path]:
    images = []
    for f in folder.iterdir():
        if f.suffix.lower() in _IMG_EXTS and not f.name.startswith("."):
            try:
                prefix = int(f.stem.split("-")[0])
                images.append((prefix, f))
            except (ValueError, IndexError):
                pass
    images.sort(key=lambda t: t[0])
    return [f for _, f in images]


async def migrate() -> None:
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set")

    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=5)

    async with pool.acquire() as conn:
        # Drop old guild_question_seen (TEXT question_id) so init_db recreates it with BIGINT
        await conn.execute("DROP TABLE IF EXISTS guild_question_seen")
        print("Dropped guild_question_seen (will recreate with BIGINT question_id)")

        # Create questions + images tables
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS questions (
                id          BIGSERIAL PRIMARY KEY,
                guild_id    BIGINT,
                category    TEXT NOT NULL,
                subcategory TEXT NOT NULL,
                item        TEXT NOT NULL,
                is_active   BOOLEAN NOT NULL DEFAULT TRUE,
                created_by  BIGINT,
                created_at  DOUBLE PRECISION NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW())
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS question_images (
                id          BIGSERIAL PRIMARY KEY,
                question_id BIGINT NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
                position    INTEGER NOT NULL,
                file_path   TEXT NOT NULL,
                UNIQUE(question_id, position)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS guild_question_seen (
                guild_id    BIGINT NOT NULL,
                question_id BIGINT NOT NULL,
                seen_at     DOUBLE PRECISION NOT NULL,
                PRIMARY KEY (guild_id, question_id)
            )
        """)
        # Partial unique indexes: global questions unique by item; guild questions unique by (guild, item)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_questions_global_unique
            ON questions (item) WHERE guild_id IS NULL
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_questions_guild_unique
            ON questions (guild_id, item) WHERE guild_id IS NOT NULL
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_questions_guild
            ON questions (guild_id)
        """)
        print("Schema ready.")

    if not DATA_CSV.exists():
        print(f"data.csv not found at {DATA_CSV} — skipping question import.")
        await pool.close()
        return

    with open(DATA_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    print(f"\nFound {len(rows)} row(s) in data.csv")
    IMAGES_PATH.mkdir(parents=True, exist_ok=True)

    imported = skipped = 0

    async with pool.acquire() as conn:
        for row in rows:
            category    = (row.get("Category")     or "").strip()
            subcategory = (row.get("Sub-category") or "").strip()
            item        = (row.get("Item")         or "").strip()

            if not item or not category:
                skipped += 1
                continue

            # Skip if already imported
            existing = await conn.fetchval(
                "SELECT id FROM questions WHERE guild_id IS NULL AND item = $1", item
            )
            if existing:
                print(f"  SKIP (exists, id={existing}): {category}/{item}")
                skipped += 1
                continue

            folder = _find_item_folder(category, item)
            if folder is None:
                print(f"  SKIP (no folder): {category}/{item}")
                skipped += 1
                continue

            images = _sorted_images(folder)
            if not images:
                print(f"  SKIP (no images): {category}/{item}")
                skipped += 1
                continue

            qid = await conn.fetchval(
                """INSERT INTO questions (guild_id, category, subcategory, item)
                   VALUES (NULL, $1, $2, $3) RETURNING id""",
                category, subcategory, item,
            )

            q_dir = IMAGES_PATH / str(qid)
            q_dir.mkdir(parents=True, exist_ok=True)

            for pos, src in enumerate(images, start=1):
                dest = q_dir / f"{pos}-{src.name}"
                shutil.copy2(src, dest)
                await conn.execute(
                    """INSERT INTO question_images (question_id, position, file_path)
                       VALUES ($1, $2, $3) ON CONFLICT DO NOTHING""",
                    qid, pos, str(dest),
                )
                print(f"    [{pos}] {src.name} → {dest}")

            print(f"  OK  (id={qid}): {category}/{item}  ({len(images)} image(s))")
            imported += 1

    await pool.close()
    print(f"\nDone — {imported} imported, {skipped} skipped.")


if __name__ == "__main__":
    asyncio.run(migrate())
