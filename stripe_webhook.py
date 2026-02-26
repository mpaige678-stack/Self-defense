import os
import stripe
import asyncpg
import requests
from fastapi import FastAPI, Request, Header, HTTPException

app = FastAPI()

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
endpoint_secret = os.getenv("STRIPE_WEBHOOK_SECRET")

DATABASE_URL = os.getenv("DATABASE_URL")
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_GUILD_ID = os.getenv("DISCORD_GUILD_ID")
DISCORD_ROLE_ID = os.getenv("DISCORD_ROLE_ID")
DISCORD_LOG_WEBHOOK_URL = os.getenv("DISCORD_LOG_WEBHOOK_URL")

@app.get("/")
def home():
    return {"status": "ok"}

async def save_purchase(email, session_id):
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS purchases (
            id SERIAL PRIMARY KEY,
            email TEXT,
            session_id TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    await conn.execute(
        "INSERT INTO purchases (email, session_id) VALUES ($1, $2)",
        email,
        session_id
    )
    await conn.close()

def assign_discord_role(discord_user_id):
    url = f"https://discord.com/api/v10/guilds/{DISCORD_GUILD_ID}/members/{discord_user_id}/roles/{DISCORD_ROLE_ID}"
    headers = {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}"
    }
    requests.put(url, headers=headers)

def send_log_message(message):
    if DISCORD_LOG_WEBHOOK_URL:
        requests.post(DISCORD_LOG_WEBHOOK_URL, json={"content": message})

@app.post("/stripe/webhook")
async def stripe_webhook(
    request: Request,
    stripe_signature: str = Header(None, alias="Stripe-Signature")
):
    payload = await request.body()

    try:
        event = stripe.Webhook.construct_event(
            payload, stripe_signature, endpoint_secret
        )
    except Exception:
        raise HTTPException(status_code=400, detail="Webhook error")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]

        email = session.get("customer_details", {}).get("email")
        session_id = session.get("id")

        # Save purchase in DB
        await save_purchase(email, session_id)

        # OPTIONAL: assign Discord role if metadata includes discord_user_id
        discord_user_id = session.get("metadata", {}).get("discord_user_id")
        if discord_user_id:
            assign_discord_role(discord_user_id)

        # Send Discord log
        send_log_message(
            f"💰 New Purchase!\nEmail: {email}\nSession: {session_id}"
        )

        print("✅ Payment fully processed")

    return {"received": True}