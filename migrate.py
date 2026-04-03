import asyncio
import os
import asyncpg


async def run():
    url = os.environ["DATABASE_URL"].replace("postgres://", "postgresql://", 1)
    conn = await asyncpg.connect(url)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id         SERIAL PRIMARY KEY,
            name       TEXT NOT NULL,
            email      TEXT NOT NULL UNIQUE,
            api_key    TEXT NOT NULL UNIQUE,
            active     BOOLEAN NOT NULL DEFAULT TRUE,
            expires_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)

    await conn.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='users' AND column_name='expires_at'
            ) THEN
                ALTER TABLE users ADD COLUMN expires_at TIMESTAMPTZ;
            END IF;
        END$$
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_logs (
            id            SERIAL PRIMARY KEY,
            user_id       INTEGER NOT NULL REFERENCES users(id),
            input_tokens  INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_usage_logs_user_id ON usage_logs(user_id)
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS allowlist (
            id         SERIAL PRIMARY KEY,
            email      TEXT NOT NULL UNIQUE,
            added_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)

    await conn.close()


asyncio.run(run())
