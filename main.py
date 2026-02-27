# main.py
# ============================================================
# Stripe Checkout -> Discord Roles + Expiration System (Railway)
# One service: FastAPI webhook + Discord bot in same async loop
# ============================================================

import os
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

import stripe
import psycopg
import discord
from fastapi import FastAPI, Request, Header, HTTPException
import uvicorn

# -----------------------------
# REQUIRED ENV VARS (Railway)
# -----------------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")                 # Discord Bot Token
DATABASE_URL = os.getenv("DATABASE_URL")                   # Railway Postgres URL
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")         # Stripe Secret Key (sk_live / sk_test)
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET") # Stripe Webhook signing secret (whsec_...)
CHECKOUT_SUCCESS_URL = os.getenv("CHECKOUT_SUCCESS_URL", "https://example.com/success")
CHECKOUT_CANCEL_URL = os.getenv("CHECKOUT_CANCEL_URL", "https://example.com/cancel")
PORT = int(os.getenv("PORT", "8080"))

# Stripe setup
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

# -----------------------------
# YOUR DISCORD SERVER IDS
# -----------------------------
GUILD_ID = 1426996503880138815

# Verified buyer role (keep after expiry)
ROLE_VERIFIED_BUYER = 0  # <-- PUT your verified buyer role ID here (or leave 0 to disable)

# Tier roles (YOU PROVIDED THESE)
ROLE_CIVILIAN = 1426274026019491951
ROLE_ELITE = 1426274025583411305
ROLE_FIGHTER = 1476334352517304596

TIER_ROLE_IDS = [ROLE_CIVILIAN, ROLE_ELITE, ROLE_FIGHTER]

# -----------------------------
# PRICES (Stripe price_id mapping)
# IMPORTANT:
# Put your REAL Stripe price IDs here.
# Your amounts are 7 / 19 / 49, but Stripe uses PRICE IDs not amounts.
# -----------------------------
PRICE_MAP = {
    # "price_xxx": ("civilian", 30, ROLE_CIVILIAN),
    # "price_xxx": ("elite",    30, ROLE_ELITE),
    # "price_xxx": ("fighter",  30, ROLE_FIGHTER),

    # TEMP placeholders (replace these 3 with your real price IDs)
    "price_CIVILIAN_REPLACE_ME": ("civilian", 30, ROLE_CIVILIAN),
    "price_ELITE_REPLACE_ME": ("elite", 30, ROLE_ELITE),
    "price_FIGHTER_REPLACE_ME": ("fighter", 30, ROLE_FIGHTER),
}

# -----------------------------
# FASTAPI APP
# -----------------------------
app = FastAPI()

# -----------------------------
# DISCORD CLIENT
# -----------------------------
intents = discord.Intents.default()
intents.members = True
client = discord.Client(intents=intents)

# -----------------------------
# DATABASE HELPERS
# -----------------------------
def db_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set (add Railway Postgres and set DATABASE_URL).")
    return psycopg.connect(DATABASE_URL)

def init_db():
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    discord_id BIGINT PRIMARY KEY,
                    tier TEXT NOT NULL,
                    role_id BIGINT NOT NULL,
                    expires_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS processed_events (
                    event_id TEXT PRIMARY KEY,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)
        conn.commit()

def mark_event_processed(event_id: str) -> bool:
    """True if new event, False if already processed."""
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM processed_events WHERE event_id=%s;", (event_id,))
            if cur.fetchone():
                return False
            cur.execute("INSERT INTO processed_events (event_id) VALUES (%s);", (event_id,))
        conn.commit()
    return True

def upsert_subscription(discord_id: int, tier: str, role_id: int, duration_days: int) -> datetime:
    """
    Renewal logic:
    - if user already has time left, extend from current expires_at
    - else start from now
    """
    now = datetime.now(timezone.utc)
    add_delta = timedelta(days=duration_days)

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT expires_at FROM subscriptions WHERE discord_id=%s;", (discord_id,))
            row = cur.fetchone()

            if row:
                current_expires = row[0]
                base = current_expires if current_expires and current_expires > now else now
            else:
                base = now

            new_expires = base + add_delta

            cur.execute("""
                INSERT INTO subscriptions (discord_id, tier, role_id, expires_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (discord_id)
                DO UPDATE SET
                    tier=EXCLUDED.tier,
                    role_id=EXCLUDED.role_id,
                    expires_at=EXCLUDED.expires_at,
                    updated_at=NOW();
            """, (discord_id, tier, role_id, new_expires))

        conn.commit()

    return new_expires

def get_expired_subscriptions():
    now = datetime.now(timezone.utc)
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT discord_id, tier, role_id, expires_at
                FROM subscriptions
                WHERE expires_at <= %s;
            """, (now,))
            return cur.fetchall()

def clear_subscription(discord_id: int):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM subscriptions WHERE discord_id=%s;", (discord_id,))
        conn.commit()

# -----------------------------
# DISCORD ROLE HELPERS
# -----------------------------
async def get_guild() -> Optional[discord.Guild]:
    guild = client.get_guild(GUILD_ID)
    if guild:
        return guild
    try:
        return await client.fetch_guild(GUILD_ID)
    except Exception:
        return None

async def fetch_member(guild: discord.Guild, discord_id: int) -> Optional[discord.Member]:
    m = guild.get_member(discord_id)
    if m:
        return m
    try:
        return await guild.fetch_member(discord_id)
    except Exception:
        return None

async def apply_roles(member: discord.Member, tier_role_id: int):
    guild = member.guild

    # Verified buyer role (optional)
    if ROLE_VERIFIED_BUYER and ROLE_VERIFIED_BUYER != 0:
        vr = guild.get_role(ROLE_VERIFIED_BUYER)
        if vr:
            await member.add_roles(vr, reason="Stripe purchase: verified")

    # Remove all tier roles first
    to_remove = []
    for rid in TIER_ROLE_IDS:
        r = guild.get_role(rid)
        if r and r in member.roles:
            to_remove.append(r)

    if to_remove:
        await member.remove_roles(*to_remove, reason="Tier change")

    # Add the new tier role
    tier_role = guild.get_role(tier_role_id)
    if tier_role:
        await member.add_roles(tier_role, reason="Stripe purchase: tier access")

async def remove_tier_roles(member: discord.Member):
    guild = member.guild
    to_remove = []
    for rid in TIER_ROLE_IDS:
        r = guild.get_role(rid)
        if r and r in member.roles:
            to_remove.append(r)
    if to_remove:
        await member.remove_roles(*to_remove, reason="Subscription expired")

# -----------------------------
# EXPIRATION LOOP
# -----------------------------
async def expiration_loop():
    await client.wait_until_ready()
    while True:
        try:
            expired = await asyncio.to_thread(get_expired_subscriptions)
            if expired:
                print(f"⏳ Found {len(expired)} expired subscriptions")

            guild = await get_guild()
            if not guild:
                print("⚠️ Guild not found; skipping expiration cycle")
                await asyncio.sleep(300)
                continue

            for discord_id, tier, role_id, expires_at in expired:
                member = await fetch_member(guild, int(discord_id))
                if member:
                    await remove_tier_roles(member)
                    # keep verified buyer role by design
                    print(f"✅ Expired: {discord_id} tier={tier} expired_at={expires_at}")
                else:
                    print(f"⚠️ Could not find member to expire: {discord_id}")

                await asyncio.to_thread(clear_subscription, int(discord_id))

        except Exception as e:
            print("Expiration loop error:", e)

        # check every 5 minutes
        await asyncio.sleep(300)

# -----------------------------
# DISCORD EVENTS
# -----------------------------
@client.event
async def on_ready():
    print(f"✅ Discord logged in as {client.user} (ID: {client.user.id})")

# -----------------------------
# STRIPE WEBHOOK
# -----------------------------
@app.post("/stripe/webhook")
async def stripe_webhook(request: Request, stripe_signature: str = Header(None)):
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="STRIPE_WEBHOOK_SECRET missing")

    payload = await request.body()

    try:
        event = stripe.Webhook.construct_event(payload, stripe_signature, STRIPE_WEBHOOK_SECRET)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    # Only care about successful checkouts
    if event["type"] != "checkout.session.completed":
        return {"ignored": True}

    # idempotency (Stripe retries events)
    event_id = event.get("id")
    if event_id:
        is_new = await asyncio.to_thread(mark_event_processed, event_id)
        if not is_new:
            return {"ok": True, "duplicate": True}

    session = event["data"]["object"]

    # REQUIRED: discord_id must be set on checkout session metadata
    discord_id_str = (session.get("metadata") or {}).get("discord_id")
    if not discord_id_str:
        return {"error": "Missing metadata.discord_id (set this when creating Checkout Session)"}

    try:
        discord_id = int(discord_id_str)
    except ValueError:
        return {"error": "Invalid metadata.discord_id"}

    # Determine price_id from line items (safe approach)
    try:
        line_items = stripe.checkout.Session.list_line_items(session["id"], limit=1)
        price_id = line_items["data"][0]["price"]["id"]
    except Exception as e:
        return {"error": f"Could not read price_id from line_items: {e}"}

    if price_id not in PRICE_MAP:
        return {"error": f"Unknown price_id {price_id}. Add it to PRICE_MAP."}

    tier, duration_days, role_id = PRICE_MAP[price_id]

    # Save subscription in DB
    new_expires = await asyncio.to_thread(upsert_subscription, discord_id, tier, role_id, duration_days)

    # Apply roles in Discord
    guild = await get_guild()
    if not guild:
        print("⚠️ Guild not found for role assign")
        return {"received": True, "warning": "guild not found", "tier": tier, "expires_at": new_expires.isoformat()}

    member = await fetch_member(guild, discord_id)
    if not member:
        print("⚠️ Member not found in guild:", discord_id)
        return {"received": True, "warning": "member not found", "tier": tier, "expires_at": new_expires.isoformat()}

    await apply_roles(member, role_id)

    print(f"✅ Payment processed: discord_id={discord_id} tier={tier} expires={new_expires.isoformat()}")
    return {"received": True, "tier": tier, "expires_at": new_expires.isoformat()}

# -----------------------------
# OPTIONAL: HEALTH CHECK
# -----------------------------
@app.get("/")
async def root():
    return {"ok": True, "service": "stripe-discord-webhook"}

# -----------------------------
# STARTUP: DB + DISCORD + EXPIRATION
# -----------------------------
@app.on_event("startup")
async def startup():
    # DB init
    if not DATABASE_URL:
        print("❌ DATABASE_URL missing")
        raise RuntimeError("DATABASE_URL missing (add Railway Postgres + set DATABASE_URL)")

    init_db()
    print("✅ Database ready")

    # Start Discord bot inside this same async loop (NO client.run())
    if not DISCORD_TOKEN:
        print("❌ DISCORD_TOKEN missing")
        raise RuntimeError("DISCORD_TOKEN is missing")

    async def bot_runner():
        while True:
            try:
                await client.start(DISCORD_TOKEN)
            except Exception as e:
                print("Discord bot crashed, restarting in 5s:", e)
                await asyncio.sleep(5)

    asyncio.create_task(bot_runner())
    asyncio.create_task(expiration_loop())

    print("✅ Discord starting + expiration loop running")

# -----------------------------
# RUN (Railway)
# -----------------------------
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)