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
# LOGGING
# -----------------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("fitness-app")

# -----------------------------
# ENV
PREMIUM_PRICE_ID=require_env(price_1TECCGB9kGqOyQaK2mqIer56)
ELITE_PRICE_ID=require_env(price_1TECCXB9kGqOyQaKLHMRa4ZY)
# -----------------------------
def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing env: {name}")
    return value

STRIPE_SECRET_KEY = require_env("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = require_env("STRIPE_WEBHOOK_SECRET")
DATABASE_URL = require_env("DATABASE_URL")

CHECKOUT_SUCCESS_URL = require_env("CHECKOUT_SUCCESS_URL")
CHECKOUT_CANCEL_URL = require_env("CHECKOUT_CANCEL_URL")

DISCORD_TOKEN = require_env("DISCORD_TOKEN")
GUILD_ID = int(require_env("GUILD_ID"))

ROLE_FREE = int(require_env("ROLE_FREE"))
ROLE_VERIFIED = int(require_env("ROLE_VERIFIED"))
ROLE_PREMIUM = int(require_env("ROLE_PREMIUM"))
ROLE_ELITE = int(require_env("ROLE_ELITE"))

PREMIUM_PRICE_ID = require_env("PREMIUM_PRICE_ID")
ELITE_PRICE_ID = require_env("ELITE_PRICE_ID")

stripe.api_key = STRIPE_SECRET_KEY

# -----------------------------
# TIER CONFIG
# -----------------------------
TIER_CONFIG = {
    "premium": {
        "price_id": PREMIUM_PRICE_ID,
        "role_id": ROLE_PREMIUM,
    },
    "elite": {
        "price_id": ELITE_PRICE_ID,
        "role_id": ROLE_ELITE,
    },
}

PAID_ROLE_IDS = [ROLE_PREMIUM, ROLE_ELITE]

# -----------------------------
# DATABASE
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
                tier TEXT DEFAULT 'free',
                status TEXT DEFAULT 'inactive',
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
            """)

            cur.execute("""
            CREATE TABLE IF NOT EXISTS bot_jobs (
                id BIGSERIAL PRIMARY KEY,
                job_type TEXT NOT NULL,
                payload JSONB NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
            """)

        conn.commit()

# -----------------------------
# JOB SYSTEM
# -----------------------------
def enqueue_job(job_type: str, payload: Dict[str, Any]):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bot_jobs (job_type, payload)
                VALUES (%s, %s)
                """,
                (job_type, json.dumps(payload)),
            )
        conn.commit()

def fetch_next_job():
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE bot_jobs
                SET status='processing',
                    updated_at=NOW()
                WHERE id = (
                    SELECT id FROM bot_jobs
                    WHERE status='pending'
                    ORDER BY id ASC
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING id, job_type, payload;
            """)
            row = cur.fetchone()
        conn.commit()
    return row

def mark_done(job_id: int):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE bot_jobs
                SET status='done',
                    updated_at=NOW()
                WHERE id=%s
                """,
                (job_id,),
            )
        conn.commit()

def mark_failed(job_id: int, error: str):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE bot_jobs
                SET status='failed',
                    updated_at=NOW()
                WHERE id=%s
                """,
                (job_id,),
            )
        conn.commit()

# -----------------------------
# DISCORD BOT
# -----------------------------
intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    log.info(f"Bot ready: {bot.user}")

# -----------------------------
# ROLE LOGIC
# -----------------------------
async def apply_roles(discord_id: str, tier: Optional[str]):
    guild = bot.get_guild(GUILD_ID)

    if guild is None:
        guild = await bot.fetch_guild(GUILD_ID)

    member = guild.get_member(int(discord_id))

    if member is None:
        member = await guild.fetch_member(int(discord_id))

    # Remove paid roles first
    for role_id in PAID_ROLE_IDS:
        role = guild.get_role(role_id)
        if role and role in member.roles:
            await member.remove_roles(role)

    # Paid member
    if tier in TIER_CONFIG:
        verified_role = guild.get_role(ROLE_VERIFIED)
        paid_role = guild.get_role(TIER_CONFIG[tier]["role_id"])

        if verified_role and verified_role not in member.roles:
            await member.add_roles(verified_role)

        if paid_role and paid_role not in member.roles:
            await member.add_roles(paid_role)

        log.info(f"Applied {tier} role to Discord user {discord_id}")
        return

    # Free fallback
    free_role = guild.get_role(ROLE_FREE)

    if free_role and free_role not in member.roles:
        await member.add_roles(free_role)

    log.info(f"Applied free role to Discord user {discord_id}")

# -----------------------------
# WORKER LOOP
# -----------------------------
async def worker():
    await bot.wait_until_ready()

    while True:
        job = fetch_next_job()

        if not job:
            await asyncio.sleep(2)
            continue

        job_id = job["id"]
        job_type = job["job_type"]
        payload = job["payload"]

        try:
            if job_type == "sync":
                await apply_roles(
                    payload["discord_id"],
                    payload.get("tier"),
                )

            mark_done(job_id)

        except Exception as e:
            log.exception("Job failed")
            mark_failed(job_id, str(e))
            await asyncio.sleep(2)

# -----------------------------
# FASTAPI APP
# -----------------------------
app = FastAPI()

@app.on_event("startup")
async def startup():
    init_db()
    asyncio.create_task(bot.start(DISCORD_TOKEN))
    asyncio.create_task(worker())
    log.info("Started FastAPI, Discord bot, and worker")

@app.get("/health")
def health():
    return {"ok": True}

# -----------------------------
# CHECKOUT
# -----------------------------
@app.get("/checkout")
def checkout(discord_id: str, tier: str):
    if tier not in TIER_CONFIG:
        raise HTTPException(status_code=400, detail="Invalid tier")

    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[
            {
                "price": TIER_CONFIG[tier]["price_id"],
                "quantity": 1,
            }
        ],
        success_url=CHECKOUT_SUCCESS_URL,
        cancel_url=CHECKOUT_CANCEL_URL,
        metadata={
            "discord_id": discord_id,
            "tier": tier,
        },
    )

    return {"url": session.url}

# -----------------------------
# STRIPE WEBHOOK
# -----------------------------
@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(
            payload,
            sig,
            STRIPE_WEBHOOK_SECRET,
        )
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid webhook")

    # Payment completed
    if event["type"] == "checkout.session.completed":
        data = event["data"]["object"]

        discord_id = data["metadata"]["discord_id"]
        tier = data["metadata"]["tier"]
        customer_id = data.get("customer")
        subscription_id = data.get("subscription")

        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO users (
                        discord_id,
                        stripe_customer_id,
                        stripe_subscription_id,
                        tier,
                        status,
                        updated_at
                    )
                    VALUES (%s, %s, %s, %s, 'active', NOW())
                    ON CONFLICT (discord_id)
                    DO UPDATE SET
                        stripe_customer_id=%s,
                        stripe_subscription_id=%s,
                        tier=%s,
                        status='active',
                        updated_at=NOW();
                """, (
                    discord_id,
                    customer_id,
                    subscription_id,
                    tier,
                    customer_id,
                    subscription_id,
                    tier,
                ))
            conn.commit()

        enqueue_job("sync", {
            "discord_id": discord_id,
            "tier": tier,
        })

    # Subscription canceled
    if event["type"] == "customer.subscription.deleted":
        sub = event["data"]["object"]
        sub_id = sub["id"]

        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE users
                    SET tier='free',
                        status='canceled',
                        updated_at=NOW()
                    WHERE stripe_subscription_id=%s
                    RETURNING discord_id;
                """, (sub_id,))
                row = cur.fetchone()
            conn.commit()

        if row:
            enqueue_job("sync", {
                "discord_id": row["discord_id"],
                "tier": None,
            })

    return JSONResponse({"ok": True})