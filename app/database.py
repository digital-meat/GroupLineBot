from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import DATABASE_URL

# asyncpg doesn't understand sslmode/channel_binding params; strip and pass ssl=True
_url = DATABASE_URL.replace("?sslmode=require", "").replace("&sslmode=require", "")
_url = _url.replace("?channel_binding=require", "").replace("&channel_binding=require", "")
engine = create_async_engine(_url, echo=False, connect_args={"ssl": True})
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


_db_initialized = False


async def init_db() -> None:
    """Create tables if they don't exist. Safe to call multiple times."""
    global _db_initialized
    if _db_initialized:
        return
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Migrate: add new columns to existing 'tasks' table (safe to re-run)
        for col_sql in (
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS completed_at TIMESTAMP",
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS tags VARCHAR(256)",
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS priority VARCHAR(8) DEFAULT 'medium'",
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS due_date DATE",
        ):
            await conn.execute(text(col_sql))
    _db_initialized = True
