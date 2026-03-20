import logging
from typing import Optional

import asyncpg

from config import (
    POSTGRES_DB,
    POSTGRES_HOST,
    POSTGRES_PASSWORD,
    POSTGRES_PORT,
    POSTGRES_USER,
)

logger = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None

CREATE_USERS_TABLE = """
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    chat_id BIGINT UNIQUE NOT NULL,
    email VARCHAR(255) NOT NULL,
    password VARCHAR(255) NOT NULL,
    status VARCHAR(20) DEFAULT 'active',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
"""

CREATE_SEEN_LISTINGS_TABLE = """
CREATE TABLE IF NOT EXISTS seen_listings (
    id SERIAL PRIMARY KEY,
    chat_id BIGINT NOT NULL REFERENCES users(chat_id),
    listing_id VARCHAR(255) NOT NULL,
    seen_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(chat_id, listing_id)
);
"""


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            host=POSTGRES_HOST,
            port=POSTGRES_PORT,
            database=POSTGRES_DB,
            user=POSTGRES_USER,
            password=POSTGRES_PASSWORD,
            min_size=1,
            max_size=5,
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


async def init_db() -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(CREATE_USERS_TABLE)
        await conn.execute(CREATE_SEEN_LISTINGS_TABLE)
    logger.info("Database schema initialised")


async def upsert_user(chat_id: int, email: str, password: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (chat_id, email, password, status, updated_at)
            VALUES ($1, $2, $3, 'active', NOW())
            ON CONFLICT (chat_id) DO UPDATE
                SET email      = EXCLUDED.email,
                    password   = EXCLUDED.password,
                    status     = 'active',
                    updated_at = NOW()
            """,
            chat_id,
            email,
            password,
        )


async def set_user_status(chat_id: int, status: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET status = $1, updated_at = NOW() WHERE chat_id = $2",
            status,
            chat_id,
        )


async def get_user(chat_id: int) -> Optional[asyncpg.Record]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM users WHERE chat_id = $1", chat_id
        )


async def get_active_users() -> list[asyncpg.Record]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM users WHERE status = 'active'")


async def get_seen_listing_ids(chat_id: int) -> set[str]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT listing_id FROM seen_listings WHERE chat_id = $1", chat_id
        )
    return {row["listing_id"] for row in rows}


async def mark_listings_seen(chat_id: int, listing_ids: list[str]) -> None:
    if not listing_ids:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO seen_listings (chat_id, listing_id)
            VALUES ($1, $2)
            ON CONFLICT DO NOTHING
            """,
            [(chat_id, lid) for lid in listing_ids],
        )


async def cleanup_old_seen_listings(days: int = 30) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM seen_listings WHERE seen_at < NOW() - ($1 * INTERVAL '1 day')",
            days,
        )
    try:
        deleted = int(result.split()[-1])
    except (ValueError, IndexError):
        deleted = 0
    if deleted:
        logger.info("Cleaned up %d old seen_listings entries", deleted)
    return deleted
