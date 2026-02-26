import os
import asyncio
from datetime import datetime, timedelta, timezone

import stripe
import psycopg
import discord
from fastapi import FastAPI, Request, Header, HTTPException

# -----------------------------
# ENV
# -----------------------------
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")  # Must exist on Railway (Postgres)

stripe.api_key = STRIPE_SECRET_KEY

app = FastAPI()

# -----------------------------
# DISCORD CONFIG
# -----------------------------
GUILD_ID = 1426996503880138815

ROLE_VERIFIED = 1476479538807439404
ROLE_RECRUIT  = 1426996503880138815  # <-- IMPORTANT: THIS LOOKS WRONG (it matches your GUILD_ID)
ROLE_ELITE    = 1475724667493810186
ROLE_FIGHTER  = 1476504028576743520

# Price -> (tier, days, role_id)
PRICE_MAP = {
    "price_1T50gsB9kGqOyQaKqsChMsDT": ("recruit", 14, ROLE_RECRUIT),
    "price_1T50fgB9kGqOyQaKgkZfH2XZ": ("elite", 30, ROLE_ELITE),
    "price_1T50dWB9kGqOyQaKddLCSgbC": ("fighter", 60, ROLE_FIGHTER),
}

TIER_ROLE_IDS = [ROLE_RECRUIT, ROLE_ELITE, ROLE_FIGHTER]

# -----------------------------
# DISCORD CLIENT
# -----------------------------
intents = discord.Intents.default()
intents.members = True
client = discord.Client(intents=intents)

# -----------------------------
# DATABASE
# -----------------------------
def db_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg.connect(DATABASE_URL)

def init_db():
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    discord_id BIGINT PRIMARY KEY,
                    tier TEXT NOT NULL,
                    expires_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)
        conn.commit()

def upsert_subscription(discord_id: int, tier: str, add_days: int) -> datetime:
    now = datetime.now(timezone.utc)
    add_delta = timedelta(days=add_days)

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT expires_at FROM subscriptions WHERE discord_id=%s;", (discord_id,))
            row = cur.fetchone()

            if row:
                current_expires = row[0]
                base = current_expires if current_expires > now else now
                new_expires = base + add_delta
                cur.execute("""
                    UPDATE subscriptions
                    SET tier=%s, expires_at=%s, updated_at=NOW()
                    WHERE discord_id=%s;
                """, (tier, new_expires, discord_id))
            else:
                new_expires = now + add_delta
                cur.execute("""
                    INSERT INTO subscriptions (discord_id, tier, expires_at)
                    VALUES (%s, %s, %s);
                """, (discord_id, tier, new_expires))

        conn.commit()

    return new_expires

def get_expired_subscriptions(limit: int = 50):
    now = datetime.now(timezone.utc)
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT discord_id, tier, expires_at
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
# ROLE HELPERS
# -----------------------------
async def fetch_member(discord_id: int) -> discord.Member | None:
    guild = client.get_guild(GUILD_ID)
    if guild is None:
        # If cache isn't ready, fetch the guild then get member
        try:
            guild = await client.fetch_guild(GUILD_ID)
        except:
            return None

    try:
        member = await guild.fetch_member(discord_id)
        return member
    except:
        return None

async def set_roles_for_tier(member: discord.Member, tier_role_id: int):
    guild = member.guild

    verified_role = guild.get_role(ROLE_VERIFIED)
    tier_role = guild.get_role(tier_role_id)

    if verified_role:
        await member.add_roles(verified_role, reason="Stripe purchase: verified buyer")

    # Remove other tier roles first (so they only have ONE tier role)
    roles_to_remove = []
    for rid in TIER_ROLE_IDS:
        if rid == tier_role_id:
            continue
        r = guild.get_role(rid)
        if r and r in member.roles:
            roles_to_remove.append(r)

    if roles_to_remove:
        await member.remove_roles(*roles_to_remove, reason="Tier change / upgrade")

    if tier_role:
        await member.add_roles(tier_role, reason="Stripe purchase: tier access")

async def remove_all_tier_roles(member: discord.Member):
    guild = member.guild
    roles_to_remove = []
    for rid in TIER_ROLE_IDS:
        r = guild.get_role(rid)
        if r and r in member.roles:
            roles_to_remove.append(r)
    if roles_to_remove:
        await member.remove_roles(*roles_to_remove, reason="Subscription expired")

# -----------------------------
# EXPIRATION LOOP
# -----------------------------
async def expiration_loop():
    await client.wait_until_ready()
    while True:
        try:
            expired = get_expired_subscriptions()
            if expired:
                print(f"⏳ Found {len(expired)} expired subscriptions")
            for discord_id, tier, expires_at in expired:
                member = await fetch_member(int(discord_id))
                if member:
                    await remove_all_tier_roles(member)
                    # Keep verified badge if you want, OR remove it too:
                    # verified = member.guild.get_role(ROLE_VERIFIED)
                    # if verified and verified in member.roles:
                    #     await member.remove_roles(verified, reason="Expired - removing verified")
                clear_subscription(int(discord_id))
        except Exception as e:
            print("Expiration loop error:", e)

        # check every 5 minutes
        await asyncio.sleep(300)

# -----------------------------
# STARTUP
# -----------------------------
@app.on_event("startup")
async def startup():
    init_db()
    print("✅ subscriptions table ready")

    # Start discord client in background
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set")

    asyncio.create_task(client.start(DISCORD_TOKEN))
    asyncio.create_task(expiration_loop())
    print("✅ Discord client starting + expiration loop running")

# -----------------------------
# STRIPE WEBHOOK
# -----------------------------
@app.post("/stripe/webhook")
async def stripe_webhook(request: Request, stripe_signature: str = Header(None)):
    payload = await request.body()

    try:
        event = stripe.Webhook.construct_event(payload, stripe_signature, STRIPE_WEBHOOK_SECRET)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    if event["type"] != "checkout.session.completed":
        return {"ignored": True}

    session = event["data"]["object"]

    # You MUST set metadata={"discord_id": "..."} when creating the Checkout Session
    discord_id = (session.get("metadata") or {}).get("discord_id")
    if not discord_id:
        return {"error": "Missing metadata.discord_id"}

    # Fetch line item price_id (Stripe doesn't include it unless expanded)
    try:
        line_items = stripe.checkout.Session.list_line_items(session["id"], limit=1)
        price_id = line_items["data"][0]["price"]["id"]
    except Exception as e:
        return {"error": f"Could not read line_items/price: {e}"}

    if price_id not in PRICE_MAP:
        return {"ignored": True, "reason": "price_id not in PRICE_MAP", "price_id": price_id}

    tier, duration_days, role_id = PRICE_MAP[price_id]

    # Save/extend subscription
    new_expires = upsert_subscription(int(discord_id), tier, duration_days)

    # Assign roles in Discord
    member = await fetch_member(int(discord_id))
    if not member:
        print("⚠️ Member not found in guild:", discord_id)
        return {"received": True, "warning": "member not found", "tier": tier, "expires_at": new_expires.isoformat()}

    await set_roles_for_tier(member, role_id)

    print(f"✅ Assigned roles: discord_id={discord_id} tier={tier} expires={new_expires.isoformat()}")
    return {"received": True, "tier": tier, "expires_at": new_expires.isoformat()}