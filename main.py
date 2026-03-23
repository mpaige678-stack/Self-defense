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
# -----------------------------
def require_env(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Missing env: {name}")
    return v

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

stripe.api_key = STRIPE_SECRET_KEY

# -----------------------------
# TIER CONFIG (UPDATED)
# -----------------------------
TIER_CONFIG = {
    "premium": {"price_id": "price_PREMIUM", "role_id": ROLE_PREMIUM},
    "elite": {"price_id": "price_ELITE", "role_id": ROLE_ELITE},
}

PAID_ROLE_IDS = [ROLE_PREMIUM, ROLE_ELITE]

# -----------------------------
# DB
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
                "INSERT INTO bot_jobs (job_type, payload) VALUES (%s, %s)",
                (job_type, json.dumps(payload)),
            )
        conn.commit()

def fetch_next_job():
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE bot_jobs
                SET status='processing'
                WHERE id = (
                    SELECT id FROM bot_jobs
                    WHERE status='pending'
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING id, job_type, payload;
            """)
            row = cur.fetchone()
        conn.commit()
    return row

def mark_done(job_id):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE bot_jobs SET status='done' WHERE id=%s", (job_id,))
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
# ROLE LOGIC (UPDATED)
# -----------------------------
async def apply_roles(discord_id: str, tier: Optional[str]):
    guild = bot.get_guild(GUILD_ID) or await bot.fetch_guild(GUILD_ID)
    member = guild.get_member(int(discord_id)) or await guild.fetch_member(int(discord_id))

    # remove paid roles
    for rid in PAID_ROLE_IDS:
        role = guild.get_role(rid)
        if role and role in member.roles:
            await member.remove_roles(role)

    # always keep verified if paid
    if tier in TIER_CONFIG:
        verified = guild.get_role(ROLE_VERIFIED)
        if verified and verified not in member.roles:
            await member.add_roles(verified)

        role = guild.get_role(TIER_CONFIG[tier]["role_id"])
        if role:
            await member.add_roles(role)
    else:
        # fallback to free
        free = guild.get_role(ROLE_FREE)
        if free and free not in member.roles:
            await member.add_roles(free)

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

        job_id, job_type, payload = job

        try:
            if job_type == "sync":
                await apply_roles(payload["discord_id"], payload.get("tier"))

            mark_done(job_id)
        except Exception as e:
            log.exception("Job failed")
            await asyncio.sleep(2)

# -----------------------------
# FASTAPI
# -----------------------------
app = FastAPI()

@app.on_event("startup")
async def startup():
    init_db()
    asyncio.create_task(bot.start(DISCORD_TOKEN))
    asyncio.create_task(worker())
    log.info("Started bot + worker")

@app.get("/health")
def health():
    return {"ok": True}

# -----------------------------
# CHECKOUT
# -----------------------------
@app.get("/checkout")
def checkout(discord_id: str, tier: str):
    if tier not in TIER_CONFIG:
        raise HTTPException(400, "Invalid tier")

    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": TIER_CONFIG[tier]["price_id"], "quantity": 1}],
        success_url=CHECKOUT_SUCCESS_URL,
        cancel_url=CHECKOUT_CANCEL_URL,
        metadata={"discord_id": discord_id, "tier": tier},
    )
    return {"url": session.url}

# -----------------------------
# WEBHOOK
# -----------------------------
@app.post("/stripe/webhook")
async def webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception:
        raise HTTPException(400, "Invalid webhook")

    if event["type"] == "checkout.session.completed":
        data = event["data"]["object"]

        discord_id = data["metadata"]["discord_id"]
        tier = data["metadata"]["tier"]

        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO users (discord_id, tier, status)
                    VALUES (%s, %s, 'active')
                    ON CONFLICT (discord_id)
                    DO UPDATE SET tier=%s, status='active';
                """, (discord_id, tier, tier))
            conn.commit()

        enqueue_job("sync", {"discord_id": discord_id, "tier": tier})

    if event["type"] in ["customer.subscription.deleted"]:
        sub = event["data"]["object"]
        sub_id = sub["id"]

        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT discord_id FROM users WHERE stripe_subscription_id=%s", (sub_id,))
                row = cur.fetchone()
            conn.commit()

        if row:
            enqueue_job("sync", {"discord_id": row["discord_id"], "tier": None})

    return JSONResponse({"ok": True})