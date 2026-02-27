import os
import asyncio
from datetime import datetime, timezone, timedelta

import stripe
import psycopg
import discord
from fastapi import FastAPI, Request, Header, HTTPException
import uvicorn

# ================= ENV =================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
PORT = int(os.getenv("PORT", "8080"))

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN missing")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL missing")

stripe.api_key = STRIPE_SECRET_KEY

# ================= DISCORD =================
GUILD_ID = 1426996503880138815

ROLE_VERIFIED = 1476479538807439404
ROLE_CIVILIAN = 1426274026019491951
ROLE_ELITE = 1426274025583411305
ROLE_FIGHTER = 1476504028576743520

TIER_ROLES = {
    "civilian": ROLE_CIVILIAN,
    "elite": ROLE_ELITE,
    "fighter": ROLE_FIGHTER
}

ALL_ROLES = list(TIER_ROLES.values())

# STRIPE PRICE MAP
PRICE_MAP = {
    "price_1T5GP1B9kGqOyQaKAHcpccxx": ("civilian", 14),
    "price_1T5GRjB9kGqOyQaKLPQ8gswA": ("elite", 30),
    "price_1T5GRRB9kGqOyQaKN5YoT1LU": ("fighter", 60)
}

# ================= DATABASE =================
def db():
    return psycopg.connect(DATABASE_URL)

def init_db():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS subs(
                discord_id BIGINT PRIMARY KEY,
                tier TEXT,
                expires TIMESTAMP
            )
            """)
        conn.commit()

def save_sub(user, tier, days):
    now = datetime.now(timezone.utc)

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT expires FROM subs WHERE discord_id=%s",(user,))
            row = cur.fetchone()

            base = now
            if row and row[0] and row[0] > now:
                base = row[0]

            new_exp = base + timedelta(days=days)

            cur.execute("""
            INSERT INTO subs VALUES(%s,%s,%s)
            ON CONFLICT(discord_id)
            DO UPDATE SET tier=%s, expires=%s
            """,(user,tier,new_exp,tier,new_exp))

        conn.commit()

    return new_exp

def expired_users():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT discord_id FROM subs WHERE expires <= NOW()")
            return [r[0] for r in cur.fetchall()]

def remove_sub(uid):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM subs WHERE discord_id=%s",(uid,))
        conn.commit()

# ================= DISCORD BOT =================
intents = discord.Intents.default()
intents.members = True
client = discord.Client(intents=intents)

async def set_roles(uid, tier):
    guild = client.get_guild(GUILD_ID)
    member = guild.get_member(uid) or await guild.fetch_member(uid)

    verified = guild.get_role(ROLE_VERIFIED)
    if verified:
        await member.add_roles(verified)

    remove = [guild.get_role(r) for r in ALL_ROLES if guild.get_role(r) in member.roles]
    if remove:
        await member.remove_roles(*remove)

    role = guild.get_role(TIER_ROLES[tier])
    if role:
        await member.add_roles(role)

async def expire_loop():
    await client.wait_until_ready()

    while True:
        for uid in await asyncio.to_thread(expired_users):
            guild = client.get_guild(GUILD_ID)
            member = guild.get_member(uid) or await guild.fetch_member(uid)

            for r in ALL_ROLES:
                role = guild.get_role(r)
                if role in member.roles:
                    await member.remove_roles(role)

            await asyncio.to_thread(remove_sub, uid)

        await asyncio.sleep(3600)

@client.event
async def on_ready():
    print("BOT READY")

# ================= FASTAPI =================
app = FastAPI()

@app.post("/stripe/webhook")
async def webhook(req: Request, stripe_signature: str = Header(None)):
    payload = await req.body()

    event = stripe.Webhook.construct_event(
        payload,
        stripe_signature,
        STRIPE_WEBHOOK_SECRET
    )

    if event["type"] != "checkout.session.completed":
        return {"ok":True}

    session = event["data"]["object"]
    meta = session.get("metadata",{})

    uid = int(meta["discord_id"])
    price = meta["price_id"]

    tier, days = PRICE_MAP[price]

    exp = await asyncio.to_thread(save_sub, uid, tier, days)
    await set_roles(uid,tier)

    return {"ok":True,"tier":tier,"expires":exp.isoformat()}

# ================= START =================
async def main():
    init_db()

    asyncio.create_task(client.start(DISCORD_TOKEN))
    asyncio.create_task(expire_loop())

    config = uvicorn.Config(app, host="0.0.0.0", port=PORT)
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == "__main__":
    asyncio.run(main())