import os
import psycopg
from psycopg.rows import dict_row
from datetime import datetime, timedelta, timezone

DATABASE_URL = os.getenv("DATABASE_URL")

def conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL not set (Railway Postgres).")
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

def init_db():
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                discord_id BIGINT PRIMARY KEY,
                tier TEXT NOT NULL DEFAULT 'free',
                joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """)
            cur.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                discord_id BIGINT PRIMARY KEY,
                tier TEXT NOT NULL DEFAULT 'free',
                expires_at TIMESTAMPTZ NULL
            );
            """)
            cur.execute("""
            CREATE TABLE IF NOT EXISTS submissions (
                id BIGSERIAL PRIMARY KEY,
                discord_id BIGINT NOT NULL,
                message_url TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                coach_note TEXT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """)
            cur.execute("""
            CREATE TABLE IF NOT EXISTS completions (
                id BIGSERIAL PRIMARY KEY,
                discord_id BIGINT NOT NULL,
                done_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                source TEXT NOT NULL DEFAULT 'done'
            );
            """)
        c.commit()

def ensure_user(discord_id: int):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO users (discord_id) VALUES (%s)
                ON CONFLICT (discord_id) DO NOTHING;
            """, (discord_id,))
        c.commit()

def set_tier(discord_id: int, tier: str):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO users (discord_id, tier)
                VALUES (%s, %s)
                ON CONFLICT (discord_id) DO UPDATE SET tier = EXCLUDED.tier;
            """, (discord_id, tier))
            cur.execute("""
                INSERT INTO subscriptions (discord_id, tier)
                VALUES (%s, %s)
                ON CONFLICT (discord_id) DO UPDATE SET tier = EXCLUDED.tier;
            """, (discord_id, tier))
        c.commit()

def get_tier(discord_id: int) -> str:
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT tier FROM users WHERE discord_id=%s;", (discord_id,))
            row = cur.fetchone()
            return row["tier"] if row else "free"

def set_subscription_expiry(discord_id: int, expires_at):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO subscriptions (discord_id, tier, expires_at)
                VALUES (%s, 'premium', %s)
                ON CONFLICT (discord_id) DO UPDATE SET expires_at = EXCLUDED.expires_at;
            """, (discord_id, expires_at))
        c.commit()

def get_expiring(now_utc: datetime):
    # returns (discord_id, tier, expires_at)
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
                SELECT discord_id, tier, expires_at
                FROM subscriptions
                WHERE expires_at IS NOT NULL;
            """)
            return cur.fetchall()

def add_submission(discord_id: int, message_url: str):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO submissions (discord_id, message_url)
                VALUES (%s, %s)
                RETURNING id;
            """, (discord_id, message_url))
            sid = cur.fetchone()["id"]
        c.commit()
    return sid

def set_submission_status(submission_id: int, status: str, coach_note: str | None):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
                UPDATE submissions
                SET status=%s, coach_note=%s
                WHERE id=%s
                RETURNING discord_id, message_url;
            """, (status, coach_note, submission_id))
            row = cur.fetchone()
        c.commit()
    return row  # dict with discord_id, message_url

def add_done(discord_id: int, source: str = "done"):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO completions (discord_id, source)
                VALUES (%s, %s);
            """, (discord_id, source))
        c.commit()

def count_done_in_window(discord_id: int, since_utc: datetime) -> int:
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) AS n
                FROM completions
                WHERE discord_id=%s AND done_at >= %s;
            """, (discord_id, since_utc))
            return int(cur.fetchone()["n"])

def weekly_leaderboard(since_utc: datetime, limit: int = 10):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
                SELECT discord_id, COUNT(*) AS n
                FROM completions
                WHERE done_at >= %s
                GROUP BY discord_id
                ORDER BY n DESC
                LIMIT %s;
            """, (since_utc, limit))
            return cur.fetchall()

def user_progress(discord_id: int):
    now = datetime.now(timezone.utc)
    since7 = now - timedelta(days=7)
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) AS last7
                FROM completions
                WHERE discord_id=%s AND done_at >= %s;
            """, (discord_id, since7))
            last7 = int(cur.fetchone()["last7"])
            cur.execute("""
                SELECT COUNT(*) AS total
                FROM completions
                WHERE discord_id=%s;
            """, (discord_id,))
            total = int(cur.fetchone()["total"])
    return {"last7": last7, "total": total}
