import os
import stripe
import aiohttp
from fastapi import FastAPI, Request, Header, HTTPException

app = FastAPI()

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
endpoint_secret = os.getenv("STRIPE_WEBHOOK_SECRET")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = 1426996503880138815

ROLE_VERIFIED = 1476479538807439404
ROLE_RECRUIT   = 1426996503880138815
ROLE_ELITE     = 1475724667493810186
ROLE_FIGHTER   = 1476504028576743520

PRICE_MAP = {
    "price_1T50gsB9kGqOyQaKqsChMsDT": ("recruit", 14),
    "price_1T50fgB9kGqOyQaKgkZfH2XZ": ("elite", 30),
    "price_1T50dWB9kGqOyQaKddLCSgbC": ("fighter", 60),
}

DISCORD_API_BASE = "https://discord.com/api/v10"


async def discord_add_role(user_id: int, role_id: int):
    url = f"{DISCORD_API_BASE}/guilds/{GUILD_ID}/members/{user_id}/roles/{role_id}"
    headers = {"Authorization": f"Bot {DISCORD_TOKEN}"}
    async with aiohttp.ClientSession() as session:
        async with session.put(url, headers=headers) as r:
            if r.status not in (204, 200):
                text = await r.text()
                raise HTTPException(status_code=500, detail=f"Discord add role failed: {r.status} {text}")


async def discord_remove_role(user_id: int, role_id: int):
    url = f"{DISCORD_API_BASE}/guilds/{GUILD_ID}/members/{user_id}/roles/{role_id}"
    headers = {"Authorization": f"Bot {DISCORD_TOKEN}"}
    async with aiohttp.ClientSession() as session:
        async with session.delete(url, headers=headers) as r:
            # 204 is success; 404 means they didn't have it (also fine)
            if r.status not in (204, 404, 200):
                text = await r.text()
                raise HTTPException(status_code=500, detail=f"Discord remove role failed: {r.status} {text}")


@app.get("/")
def home():
    return {"status": "ok"}


@app.post("/stripe/webhook")
async def stripe_webhook(
    request: Request,
    stripe_signature: str = Header(None, alias="Stripe-Signature"),
):
    if not endpoint_secret:
        raise HTTPException(status_code=500, detail="STRIPE_WEBHOOK_SECRET missing")

    payload = await request.body()

    try:
        event = stripe.Webhook.construct_event(payload, stripe_signature, endpoint_secret)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    if event["type"] != "checkout.session.completed":
        return {"received": True}

    session = event["data"]["object"]

    # You MUST pass discord_id when creating the checkout session
    discord_id = session.get("metadata", {}).get("discord_id")
    if not discord_id:
        return {"error": "Missing metadata.discord_id"}

    # Fetch the purchased price_id properly
    line_items = stripe.checkout.Session.list_line_items(session["id"], limit=1)
    if not line_items.data:
        return {"error": "No line items found"}

    price_id = line_items.data[0].price.id

    if price_id not in PRICE_MAP:
        return {"ignored": True, "price_id": price_id}

    tier, duration_days = PRICE_MAP[price_id]
    user_id = int(discord_id)

    # Always give verified
    await discord_add_role(user_id, ROLE_VERIFIED)

    # Remove tier roles (upgrade path)
    await discord_remove_role(user_id, ROLE_RECRUIT)
    await discord_remove_role(user_id, ROLE_ELITE)
    await discord_remove_role(user_id, ROLE_FIGHTER)

    # Add the new tier
    if tier == "recruit":
        await discord_add_role(user_id, ROLE_RECRUIT)
    elif tier == "elite":
        await discord_add_role(user_id, ROLE_ELITE)
    elif tier == "fighter":
        await discord_add_role(user_id, ROLE_FIGHTER)

    print(f"✅ Assigned {tier} to user {user_id} for {duration_days} days (expiry system not added yet)")
    return {"received": True, "tier": tier}