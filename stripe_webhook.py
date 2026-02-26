import os
import stripe
import discord
from fastapi import FastAPI, Request, Header, HTTPException

app = FastAPI()

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
endpoint_secret = os.getenv("STRIPE_WEBHOOK_SECRET")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = 1426274025583411301

ROLES = {
    "verified": 1476479538807439404,
    "recruit": 1426996503880138815,
    "amateur": 1476336264818196674,
    "prospect": 1476336193292472431,
    "contender": 1476336341632422051,
    "elite": 1475724667493810186,
    "champion": 1476336417339871422
}

client = discord.Client(intents=discord.Intents.all())

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
        amount = session["amount_total"] / 100

        guild = client.get_guild(GUILD_ID)
        member = guild.get_member(int(discord_id))

        await member.add_roles(guild.get_role(ROLES["verified"]))

        # assign rank based on payment
        if amount >= 50:
            role = "champion"
        elif amount >= 30:
            role = "elite"
        elif amount >= 15:
            role = "prospect"
        else:
            role = "recruit"

        await member.add_roles(guild.get_role(ROLES[role]))

        print("Assigned role:", role, "to", member)

    return {"received": True}