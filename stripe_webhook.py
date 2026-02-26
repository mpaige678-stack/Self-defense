import os
from datetime import datetime, timedelta, timezone

import stripe
import psycopg
from fastapi import FastAPI, Request, Header, HTTPException

app = FastAPI()

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
endpoint_secret = os.getenv("STRIPE_WEBHOOK_SECRET")
DATABASE_URL = os.getenv("DATABASE_URL")

# PRICE_ID -> (tier_name, duration_days)
PRICE_MAP = {
    "price_1T50gsB9kGqOyQaKqsChMsDT": ("recruit", 14),
    "price_1T50fgB9kGqOyQaKgkZfH2XZ": ("elite", 30),
    "price_1T50dWB9kGqOyQaKddLCSgbC": ("fighter", 60),
}

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

@app.on_event("startup")
async def startup():
    init_db()
    print("✅ subscriptions table ready")

@app.post("/stripe/webhook")
async def stripe_webhook(request: Request, stripe_signature: str = Header(None)):
    payload = await request.body()

    try:
        event = stripe.Webhook.construct_event(payload, stripe_signature, endpoint_secret)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    # We only care about successful checkout payments
    if event["type"] != "checkout.session.completed":
        return {"ignored": True}

    session = event["data"]["object"]

    # You MUST set metadata={"discord_id": "..."} when creating the Checkout Session
    discord_id = (session.get("metadata") or {}).get("discord_id")
    if not discord_id:
        return {"error": "Missing metadata.discord_id"}

    # Stripe does NOT include line_items unless you expand them.
    # Easiest: fetch line items here.
    try:
        line_items = stripe.checkout.Session.list_line_items(session["id"], limit=1)
        price_id = line_items["data"][0]["price"]["id"]
    except Exception as e:
        return {"error": f"Could not read line_items/price: {e}"}

    if price_id not in PRICE_MAP:
        return {"ignored": True, "reason": "price_id not in PRICE_MAP", "price_id": price_id}

    tier, duration_days = PRICE_MAP[price_id]

    now = datetime.now(timezone.utc)
    add_days = timedelta(days=duration_days)

    # Renewal logic:
    # If they still have time left, extend from current expires_at
    # Otherwise start from now
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT expires_at FROM subscriptions WHERE discord_id=%s;", (int(discord_id),))
            row = cur.fetchone()

            if row:
                current_expires = row[0]
                base = current_expires if current_expires > now else now
                new_expires = base + add_days
                cur.execute("""
                    UPDATE subscriptions
                    SET tier=%s, expires_at=%s, updated_at=NOW()
                    WHERE discord_id=%s;
                """, (tier, new_expires, int(discord_id)))
            else:
                new_expires = now + add_days
                cur.execute("""
                    INSERT INTO subscriptions (discord_id, tier, expires_at)
                    VALUES (%s, %s, %s);
                """, (int(discord_id), tier, new_expires))

        conn.commit()

    print(f"✅ Saved subscription: discord_id={discord_id} tier={tier} expires={new_expires.isoformat()}")
    return {"received": True, "tier": tier, "expires_at": new_expires.isoformat()}