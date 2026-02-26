import os
import asyncio
from datetime import datetime, timezone, timedelta

import stripe
import psycopg
import discord
from fastapi import FastAPI, Request, Header, HTTPException
import uvicorn


# =========================================================
# ENV VARS (Railway Variables)
# =========================================================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")  # not required for webhook verify, but ok to have

PORT = int(os.getenv("PORT", "8080"))

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


# =========================================================
# DISCORD CONFIG (PUT YOUR REAL IDS HERE)
# =========================================================
GUILD_ID = 1426996503880138815

# Roles (these MUST be role IDs, not server ID)
ROLE_VERIFIED_BUYER = 1476479538807439404
ROLE_RECRUIT_ACCESS = 0   # <-- PUT RECRUIT ROLE ID HERE
ROLE_ELITE_ACCESS = 1475724667493810186
ROLE_FIGHTER_ACCESS = 1476504028576743520

TIER_ROLE_MAP = {
    "recruit": ROLE_RECRUIT_ACCESS,
    "elite": ROLE_ELITE_ACCESS,
    "fighter": ROLE_FIGHTER_ACCESS,
}

ALL_TIER_ROLE_IDS = [ROLE_RECRUIT_ACCESS, ROLE_ELITE_ACCESS, ROLE_FIGHTER_ACCESS]

# Stripe price_id -> (tier, duration_days)
PRICE_MAP = {
    "price_1T50gsB9kGqOyQaKqsChMsDT": ("recruit", 14),
    "price_1T50fgB9kGqOyQaKgkZfH2XZ": ("elite", 30),
    "price_1T50dWB9kGqOyQaKddLCSgbC": ("fighter", 60),
}

# Optional: your channel IDs (NOT required for permissions)
CHANNEL_CIVILIAN = 1426274026019491951
CHANNEL_ELITE = 1426274025583411305
CHANNEL_FIGHTER = 1476334352517304596


# =========================================================
# DATABASE (Postgres)
# =========================================================
def db_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set (add Railway Postgres + set DATABASE_URL).")
    return psycopg.connect(DATABASE_URL)

def init_db():
    with db_conn() as conn:
        with conn.cursor() as cur:
            # subscriptions table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    discord_id BIGINT PRIMARY KEY,
                    tier TEXT NOT NULL,
                    expires_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)
            # processed stripe events (idempotency)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS processed_events (
                    event_id TEXT PRIMARY KEY,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)
        conn.commit()

def mark_event_processed(event_id: str) -> bool:
    """
    Returns True if it was newly inserted (not processed before).
    Returns False if it already exists.
    """
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM processed_events WHERE event_id = %s;", (event_id,))
            exists = cur.fetchone()
            if exists:
                return False
            cur.execute("INSERT INTO processed_events (event_id) VALUES (%s);", (event_id,))
        conn.commit()
    return True

def upsert_subscription(discord_id: int, tier: str, duration_days: int) -> datetime:
    """
    Extend from current expiry if still active, otherwise from now.
    Returns new expires_at.
    """
    now = datetime.now(timezone.utc)

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT tier, expires_at FROM subscriptions WHERE discord_id = %s;", (discord_id,))
            row = cur.fetchone()

            if row:
                _, current_expires = row
                base = current_expires if current_expires and current_expires > now else now
            else:
                base = now

            new_expires = base + timedelta(days=duration_days)

            cur.execute("""
                INSERT INTO subscriptions (discord_id, tier, expires_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (discord_id)
                DO UPDATE SET tier = EXCLUDED.tier,
                              expires_at = EXCLUDED.expires_at,
                              updated_at = NOW();
            """, (discord_id, tier, new_expires))
        conn.commit()

    return new_expires

def get_expired_subscriptions() -> list[tuple[int, str, datetime]]:
    now = datetime.now(timezone.utc)
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT discord_id, tier, expires_at
                FROM subscriptions
                WHERE expires_at <= %s;
            """, (now,))
            rows = cur.fetchall()
    return rows

def clear_subscription(discord_id: int):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM subscriptions WHERE discord_id = %s;", (discord_id,))
        conn.commit()


# =========================================================
# DISCORD BOT
# =========================================================
intents = discord.Intents.default()
intents.members = True

client = discord.Client(intents=intents)


async def apply_roles_for_tier(discord_id: int, tier: str):
    """
    Adds Verified Buyer + tier role.
    Removes other tier roles (so they only have 1 tier at a time).
    """
    guild = client.get_guild(GUILD_ID)
    if not guild:
        print("❌ Guild not found in cache. Is the bot in the server?")
        return

    member = guild.get_member(discord_id)
    if not member:
        # Try fetching (if not cached)
        try:
            member = await guild.fetch_member(discord_id)
        except Exception:
            print(f"❌ Member {discord_id} not found.")
            return

    verified_role = guild.get_role(ROLE_VERIFIED_BUYER)
    if verified_role:
        await member.add_roles(verified_role, reason="Stripe purchase")

    # Remove all tier roles first
    remove_roles = []
    for rid in ALL_TIER_ROLE_IDS:
        if rid and (r := guild.get_role(rid)):
            remove_roles.append(r)
    if remove_roles:
        await member.remove_roles(*remove_roles, reason="Tier update")

    # Add correct tier role
    tier_role_id = TIER_ROLE_MAP.get(tier)
    tier_role = guild.get_role(tier_role_id) if tier_role_id else None
    if tier_role:
        await member.add_roles(tier_role, reason=f"Tier set to {tier}")
        print(f"✅ Assigned tier '{tier}' to {member} ({member.id})")
    else:
        print(f"❌ Tier role missing for '{tier}'. Check ROLE IDs.")


async def expire_roles_job():
    """
    Runs forever. Every hour it checks DB for expired subs and removes tier roles.
    Verified Buyer stays.
    """
    await client.wait_until_ready()
    while not client.is_closed():
        try:
            expired = await asyncio.to_thread(get_expired_subscriptions)
            if expired:
                guild = client.get_guild(GUILD_ID)
                for discord_id, tier, expires_at in expired:
                    try:
                        member = guild.get_member(discord_id) or await guild.fetch_member(discord_id)
                        # Remove tier roles
                        remove_roles = []
                        for rid in ALL_TIER_ROLE_IDS:
                            if rid and (r := guild.get_role(rid)):
                                remove_roles.append(r)
                        if remove_roles:
                            await member.remove_roles(*remove_roles, reason="Subscription expired")

                        # Keep verified buyer role on purpose
                        print(f"⌛ Expired {discord_id} tier={tier} expired_at={expires_at}")

                        # Remove row from DB so we don't process again
                        await asyncio.to_thread(clear_subscription, discord_id)

                    except Exception as e:
                        print(f"Expire error for {discord_id}: {e}")

            # sleep 1 hour
            await asyncio.sleep(3600)

        except Exception as e:
            print("Expire job error:", e)
            await asyncio.sleep(60)


@client.event
async def on_ready():
    print(f"✅ Discord bot logged in as {client.user} (ID: {client.user.id})")


# =========================================================
# FASTAPI WEBHOOK (Stripe -> Discord)
# =========================================================
app = FastAPI()

@app.post("/stripe/webhook")
async def stripe_webhook(request: Request, stripe_signature: str = Header(None)):
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="STRIPE_WEBHOOK_SECRET missing")

    payload = await request.body()

    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=stripe_signature,
            secret=STRIPE_WEBHOOK_SECRET
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    # Only handle completed checkouts
    if event["type"] != "checkout.session.completed":
        return {"ignored": True}

    # Idempotency: Stripe may retry same event
    event_id = event.get("id")
    if event_id:
        new = await asyncio.to_thread(mark_event_processed, event_id)
        if not new:
            return {"ok": True, "duplicate": True}

    session = event["data"]["object"]

    # You MUST set this when creating checkout session:
    # metadata={"discord_id": str(user_id)}
    metadata = session.get("metadata") or {}
    discord_id_str = metadata.get("discord_id")
    if not discord_id_str:
        return {"error": "Missing metadata.discord_id"}

    try:
        discord_id = int(discord_id_str)
    except ValueError:
        return {"error": "Invalid discord_id in metadata"}

    # Getting price_id:
    # BEST: set the price_id in metadata too (recommended).
    # But we can attempt reading from session if expanded. Often line_items aren't included.
    price_id = metadata.get("price_id")

    if not price_id:
        return {"error": "Missing metadata.price_id (recommended fix: store price_id in checkout metadata)"}

    if price_id not in PRICE_MAP:
        return {"error": f"Unknown price_id {price_id}"}

    tier, duration_days = PRICE_MAP[price_id]

    # Save subscription + expiry in DB
    new_expires = await asyncio.to_thread(upsert_subscription, discord_id, tier, duration_days)

    # Apply roles immediately
    await apply_roles_for_tier(discord_id, tier)

    print(f"✅ Payment -> discord_id={discord_id} tier={tier} expires={new_expires.isoformat()}")
    return {"received": True, "tier": tier, "expires_at": new_expires.isoformat()}


# =========================================================
# RUN BOTH (Discord + API) IN ONE PROCESS
# =========================================================
async def run_api():
    config = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

async def main():
    # DB required for expiration system
    init_db()
    print("✅ Database ready")

    # Start expiration loop
    asyncio.create_task(expire_roles_job())

    # Start API server
    asyncio.create_task(run_api())

    # Start Discord bot
    await client.start(DISCORD_TOKEN)

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN missing")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL missing (add Railway Postgres + set DATABASE_URL)")
    asyncio.run(main())