import os
import json
import asyncio
import logging
from typing import Dict, Any, Optional

import stripe
import psycopg
from psycopg.rows import dict_row

import discord
from discord.ext import commands

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse


# -----------------------------
# Logging
# -----------------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")


# -----------------------------
# ENV
# -----------------------------
def require_env(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return v


# Stripe + DB
STRIPE_SECRET_KEY = require_env("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = require_env("STRIPE_WEBHOOK_SECRET")
DATABASE_URL = require_env("DATABASE_URL")
CHECKOUT_SUCCESS_URL = require_env("CHECKOUT_SUCCESS_URL")
CHECKOUT_CANCEL_URL = require_env("CHECKOUT_CANCEL_URL")

# Discord
DISCORD_TOKEN = require_env("DISCORD_TOKEN")
GUILD_ID = int(require_env("GUILD_ID"))

# Optional IDs (recommended)
CIVILIAN_ROLE_ID = int(require_env("CIVILIAN_ROLE_ID"))
FIGHTER_ROLE_ID = int(require_env("FIGHTER_ROLE_ID"))
ELITE_ROLE_ID = int(require_env("ELITE_ROLE_ID"))

stripe.api_key = STRIPE_SECRET_KEY


# -----------------------------
# Tier config
# -----------------------------
TIER_CONFIG = {
    "civilian": {"price_id": "price_1T5GP1B9kGqOyQaKAHcpccxx", "role_id": CIVILIAN_ROLE_ID},
    "fighter":  {"price_id": "price_1T5GRRB9kGqOyQaKN5YoT1LU", "role_id": FIGHTER_ROLE_ID},
    "elite":    {"price_id": "price_1T5GRjB9kGqOyQaKLPQ8gswA", "role_id": ELITE_ROLE_ID},
}

def normalize_tier(t: str) -> str:
    return (t or "").strip().lower()


# -----------------------------
# DB helpers
# -----------------------------
def db_conn():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

def init_db():
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                discord_id TEXT PRIMARY KEY,
                stripe_customer_id TEXT,
                stripe_subscription_id TEXT,
                tier TEXT,
                status TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
            """)

            cur.execute("""
            CREATE TABLE IF NOT EXISTS stripe_events (
                event_id TEXT PRIMARY KEY,
                type TEXT,
                payload JSONB,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            """)

            cur.execute("""
            CREATE TABLE IF NOT EXISTS bot_jobs (
                id BIGSERIAL PRIMARY KEY,
                job_type TEXT NOT NULL,
                payload JSONB NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMPTZ DEFAULT NOW(),
                processed_at TIMESTAMPTZ
            );
            """)

            cur.execute("""
            CREATE TABLE IF NOT EXISTS weight_logs (
                id BIGSERIAL PRIMARY KEY,
                discord_id TEXT NOT NULL,
                weight REAL NOT NULL,
                unit TEXT NOT NULL DEFAULT 'lb',
                note TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            """)
        conn.commit()

def enqueue_job(job_type: str, payload: Dict[str, Any]):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO bot_jobs (job_type, payload) VALUES (%s, %s)",
                (job_type, json.dumps(payload)),
            )
        conn.commit()

def fetch_next_job() -> Optional[Dict[str, Any]]:
    """
    Atomically claim one pending job (SKIP LOCKED avoids double-processing).
    """
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE bot_jobs
                SET status='processing'
                WHERE id = (
                    SELECT id FROM bot_jobs
                    WHERE status='pending'
                    ORDER BY id ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                RETURNING id, job_type, payload;
            """)
            row = cur.fetchone()
        conn.commit()

    if not row:
        return None
    return {"id": row["id"], "job_type": row["job_type"], "payload": row["payload"]}

def mark_job_done(job_id: int):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE bot_jobs SET status='done', processed_at=NOW() WHERE id=%s",
                (job_id,),
            )
        conn.commit()

def mark_job_failed(job_id: int, reason: str = "failed"):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE bot_jobs SET status=%s, processed_at=NOW() WHERE id=%s",
                (f"failed:{reason}"[:120], job_id),
            )
        conn.commit()


# -----------------------------
# Discord bot (controller)
# -----------------------------
intents = discord.Intents.default()
intents.members = True  # needed to add/remove roles
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    log.info(f"Discord bot logged in as {bot.user} (guild_id={GUILD_ID})")

async def assign_tier_role(discord_id: str, tier: str):
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        # If bot isn't ready / guild not cached yet, fetch after ready
        guild = await bot.fetch_guild(GUILD_ID)

    member = guild.get_member(int(discord_id))
    if not member:
        member = await guild.fetch_member(int(discord_id))

    # remove all tier roles, then add correct one
    tier_role_ids = [TIER_CONFIG[t]["role_id"] for t in TIER_CONFIG]
    roles_to_remove = [guild.get_role(rid) for rid in tier_role_ids]
    roles_to_remove = [r for r in roles_to_remove if r and r in member.roles]

    if roles_to_remove:
        await member.remove_roles(*roles_to_remove, reason="Tier sync")

    role = guild.get_role(TIER_CONFIG[tier]["role_id"])
    if not role:
        raise RuntimeError(f"Role not found in guild for tier={tier}")

    await member.add_roles(role, reason="Stripe purchase -> tier role")
    log.info(f"Assigned role {role.name} to {member} for tier={tier}")

async def sync_roles(discord_id: str):
    # Look up the user tier/status in DB, then apply roles
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT tier, status FROM users WHERE discord_id=%s", (discord_id,))
            row = cur.fetchone()
        conn.commit()

    if not row:
        log.warning(f"sync_roles: no DB row for discord_id={discord_id}")
        return

    tier = row["tier"]
    status = row["status"]

    guild = bot.get_guild(GUILD_ID) or await bot.fetch_guild(GUILD_ID)
    member = guild.get_member(int(discord_id)) or await guild.fetch_member(int(discord_id))

    tier_role_ids = [TIER_CONFIG[t]["role_id"] for t in TIER_CONFIG]
    roles_to_remove = [guild.get_role(rid) for rid in tier_role_ids]
    roles_to_remove = [r for r in roles_to_remove if r and r in member.roles]

    if roles_to_remove:
        await member.remove_roles(*roles_to_remove, reason="Tier sync refresh")

    if status == "active" and tier in TIER_CONFIG:
        role = guild.get_role(TIER_CONFIG[tier]["role_id"])
        if role:
            await member.add_roles(role, reason="Tier sync active")
            log.info(f"sync_roles: ensured {member} has role {role.name}")
    else:
        log.info(f"sync_roles: user {discord_id} not active (status={status}) -> removed tier roles")


async def job_worker_loop():
    await bot.wait_until_ready()
    log.info("Job worker loop started.")
    while not bot.is_closed():
        job = None
        try:
            job = fetch_next_job()
            if not job:
                await asyncio.sleep(2.0)
                continue

            job_id = job["id"]
            job_type = job["job_type"]
            payload = job["payload"] or {}

            if job_type == "assign_role":
                await assign_tier_role(payload["discord_id"], normalize_tier(payload["tier"]))
            elif job_type == "sync_roles":
                await sync_roles(payload["discord_id"])
            else:
                log.warning(f"Unknown job_type={job_type}")

            mark_job_done(job_id)

        except Exception as e:
            log.exception("Job failed")
            if job:
                mark_job_failed(job["id"], str(e))
            await asyncio.sleep(2.0)


# -----------------------------
# FastAPI app
# -----------------------------
app = FastAPI(title="Stripe + Discord Controller", version="1.0.0")

@app.on_event("startup")
async def startup():
    init_db()
    log.info("DB initialized.")

    # Start discord bot without bot.run()
    asyncio.create_task(bot.start(DISCORD_TOKEN))
    # Start the DB job worker
    asyncio.create_task(job_worker_loop())
    log.info("Startup complete (bot + worker tasks scheduled).")

@app.on_event("shutdown")
async def shutdown():
    try:
        await bot.close()
    except Exception:
        pass


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/create-checkout-session")
def create_checkout_session(discord_id: str, tier: str):
    tier = normalize_tier(tier)
    if tier not in TIER_CONFIG:
        raise HTTPException(status_code=400, detail=f"Invalid tier. Use one of: {list(TIER_CONFIG.keys())}")

    if not discord_id or not discord_id.isdigit():
        raise HTTPException(status_code=400, detail="discord_id must be numeric (copy user ID from Discord Developer Mode).")

    price_id = TIER_CONFIG[tier]["price_id"]

    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=f"{CHECKOUT_SUCCESS_URL}?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=CHECKOUT_CANCEL_URL,
            metadata={"discord_id": discord_id, "tier": tier},
        )
        return {"url": session.url}
    except Exception as e:
        log.exception("Failed creating checkout session")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Webhook error: {str(e)}")

    event_id = event.get("id")
    event_type = event.get("type")

    # Save event (optional)
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO stripe_events (event_id, type, payload) VALUES (%s, %s, %s) ON CONFLICT (event_id) DO NOTHING",
                    (event_id, event_type, json.dumps(event)),
                )
            conn.commit()
    except Exception:
        log.exception("Failed saving stripe event (non-fatal)")

    try:
        if event_type == "checkout.session.completed":
            session = event["data"]["object"]
            discord_id = (session.get("metadata") or {}).get("discord_id")
            tier = (session.get("metadata") or {}).get("tier")
            customer_id = session.get("customer")
            subscription_id = session.get("subscription")

            if discord_id and tier:
                with db_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            INSERT INTO users (discord_id, stripe_customer_id, stripe_subscription_id, tier, status, updated_at)
                            VALUES (%s, %s, %s, %s, %s, NOW())
                            ON CONFLICT (discord_id) DO UPDATE SET
                                stripe_customer_id = EXCLUDED.stripe_customer_id,
                                stripe_subscription_id = EXCLUDED.stripe_subscription_id,
                                tier = EXCLUDED.tier,
                                status = EXCLUDED.status,
                                updated_at = NOW();
                        """, (discord_id, customer_id, subscription_id, normalize_tier(tier), "active"))
                    conn.commit()

                enqueue_job("assign_role", {"discord_id": discord_id, "tier": tier})
                log.info(f"Enqueued assign_role: {discord_id} -> {tier}")

        elif event_type in ("customer.subscription.deleted", "customer.subscription.updated"):
            sub = event["data"]["object"]
            subscription_id = sub.get("id")
            status = sub.get("status")

            with db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT discord_id FROM users WHERE stripe_subscription_id=%s", (subscription_id,))
                    row = cur.fetchone()
                conn.commit()

            if row:
                discord_id = row["discord_id"]
                with db_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute("UPDATE users SET status=%s, updated_at=NOW() WHERE discord_id=%s", (status, discord_id))
                    conn.commit()

                enqueue_job("sync_roles", {"discord_id": discord_id})
                log.info(f"Enqueued sync_roles: {discord_id} (status={status})")

    except Exception:
        log.exception("Webhook handler failed (Stripe still gets 200).")

    return JSONResponse({"received": True})