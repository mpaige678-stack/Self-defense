import os
import stripe
import discord
from fastapi import FastAPI, Request, Header, HTTPException
from datetime import datetime, timedelta

app = FastAPI()

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
endpoint_secret = os.getenv("STRIPE_WEBHOOK_SECRET")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

GUILD_ID = 1426996503880138815

ROLE_VERIFIED = 1476479538807439404
ROLE_RECRUIT = 1426996503880138815
ROLE_ELITE = 1475724667493810186
ROLE_FIGHTER = 1476504028576743520

PRICE_MAP = {
    "price_1T50gsB9kGqOyQaKqsChMsDT": ("recruit", 14),
    "price_1T50fgB9kGqOyQaKgkZfH2XZ": ("elite", 30),
    "price_1T50dWB9kGqOyQaKddLCSgbC": ("fighter", 60),
}

intents = discord.Intents.default()
intents.members = True
client = discord.Client(intents=intents)

@app.post("/stripe/webhook")
async def stripe_webhook(request: Request, stripe_signature: str = Header(None)):
    payload = await request.body()

    try:
        event = stripe.Webhook.construct_event(
            payload,
            stripe_signature,
            endpoint_secret
        )
    except Exception:
        raise HTTPException(status_code=400)

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]

        discord_id = session["metadata"].get("discord_id")
        price_id = session["line_items"]["data"][0]["price"]["id"]

        if price_id not in PRICE_MAP:
            return {"ignored": True}

        tier, duration_days = PRICE_MAP[price_id]

        await client.login(DISCORD_TOKEN)
        guild = client.get_guild(GUILD_ID)
        member = guild.get_member(int(discord_id))

        if not member:
            return {"error": "Member not found"}

        # Always add Verified badge
        await member.add_roles(guild.get_role(ROLE_VERIFIED))

        # Remove lower roles before upgrading
        await member.remove_roles(
            guild.get_role(ROLE_RECRUIT),
            guild.get_role(ROLE_ELITE),
            guild.get_role(ROLE_FIGHTER)
        )

        # Assign correct tier
        if tier == "recruit":
            await member.add_roles(guild.get_role(ROLE_RECRUIT))
        elif tier == "elite":
            await member.add_roles(guild.get_role(ROLE_ELITE))
        elif tier == "fighter":
            await member.add_roles(guild.get_role(ROLE_FIGHTER))

        print(f"Assigned {tier} to {member}")

    return {"received": True}