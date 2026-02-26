import os
import stripe
import aiohttp
from fastapi import FastAPI, Request, Header, HTTPException

# ---------------------------
# FASTAPI APP
# ---------------------------
app = FastAPI()

# ---------------------------
# ENV VARIABLES
# ---------------------------
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
endpoint_secret = os.getenv("STRIPE_WEBHOOK_SECRET")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# ---------------------------
# DISCORD CONFIG
# ---------------------------
GUILD_ID = 1426996503880138815

ROLE_VERIFIED = 1476479538807439404
ROLE_RECRUIT = 1426996503880138815
ROLE_ELITE = 1475724667493810186
ROLE_FIGHTER = 1476504028576743520

# ---------------------------
# STRIPE PRICE MAP
# ---------------------------
PRICE_MAP = {
    "price_1T50gsB9kGqOyQaKqsChMsDT": "recruit",
    "price_1T50fgB9kGqOyQaKgkZfH2XZ": "elite",
    "price_1T50dWB9kGqOyQaKddLCSgbC": "fighter",
}

DISCORD_API_BASE = "https://discord.com/api/v10"

# ---------------------------
# DISCORD ROLE FUNCTIONS
# ---------------------------
async def add_role(user_id: int, role_id: int):
    url = f"{DISCORD_API_BASE}/guilds/{GUILD_ID}/members/{user_id}/roles/{role_id}"
    headers = {"Authorization": f"Bot {DISCORD_TOKEN}"}
    async with aiohttp.ClientSession() as session:
        async with session.put(url, headers=headers) as r:
            if r.status not in (204, 200):
                text = await r.text()
                print("ADD ROLE ERROR:", text)

async def remove_role(user_id: int, role_id: int):
    url = f"{DISCORD_API_BASE}/guilds/{GUILD_ID}/members/{user_id}/roles/{role_id}"
    headers = {"Authorization": f"Bot {DISCORD_TOKEN}"}
    async with aiohttp.ClientSession() as session:
        async with session.delete(url, headers=headers) as r:
            # 404 means user didn't have role (fine)
            if r.status not in (204, 404, 200):
                text = await r.text()
                print("REMOVE ROLE ERROR:", text)

# ---------------------------
# HEALTH CHECK
# ---------------------------
@app.get("/")
def home():
    return {"status": "ok"}

# ---------------------------
# STRIPE WEBHOOK
# ---------------------------
@app.post("/stripe/webhook")
async def stripe_webhook(
    request: Request,
    stripe_signature: str = Header(None, alias="Stripe-Signature"),
):

    if not endpoint_secret:
        raise HTTPException(status_code=500, detail="Webhook secret missing")

    payload = await request.body()

    try:
        event = stripe.Webhook.construct_event(
            payload,
            stripe_signature,
            endpoint_secret
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    if event["type"] != "checkout.session.completed":
        return {"received": True}

    session = event["data"]["object"]

    # Must include this when creating checkout session
    discord_id = session.get("metadata", {}).get("discord_id")

    if not discord_id:
        return {"error": "Missing discord_id in metadata"}

    # Proper way to get purchased price
    line_items = stripe.checkout.Session.list_line_items(
        session["id"],
        limit=1
    )

    if not line_items.data:
        return {"error": "No line items found"}

    price_id = line_items.data[0].price.id

    if price_id not in PRICE_MAP:
        return {"ignored": True, "price_id": price_id}

    tier = PRICE_MAP[price_id]
    user_id = int(discord_id)

    # Always add verified badge
    await add_role(user_id, ROLE_VERIFIED)

    # Remove all tier roles first (upgrade safe)
    await remove_role(user_id, ROLE_RECRUIT)
    await remove_role(user_id, ROLE_ELITE)
    await remove_role(user_id, ROLE_FIGHTER)

    # Add correct tier
    if tier == "recruit":
        await add_role(user_id, ROLE_RECRUIT)
    elif tier == "elite":
        await add_role(user_id, ROLE_ELITE)
    elif tier == "fighter":
        await add_role(user_id, ROLE_FIGHTER)

    print(f"✅ Assigned {tier} to user {user_id}")

    return {"received": True, "tier": tier}