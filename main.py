import os
import json
import logging
from typing import Optional, Dict, Any

import stripe
import psycopg
from psycopg.rows import dict_row

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

# -----------------------------
# Logging
# -----------------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("api")

# -----------------------------
# Required ENV
# -----------------------------
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
DATABASE_URL = os.getenv("DATABASE_URL")
CHECKOUT_SUCCESS_URL = os.getenv("CHECKOUT_SUCCESS_URL")
CHECKOUT_CANCEL_URL = os.getenv("CHECKOUT_CANCEL_URL")

missing = []
for k, v in [
    ("STRIPE_SECRET_KEY", STRIPE_SECRET_KEY),
    ("STRIPE_WEBHOOK_SECRET", STRIPE_WEBHOOK_SECRET),
    ("DATABASE_URL", DATABASE_URL),
    ("CHECKOUT_SUCCESS_URL", CHECKOUT_SUCCESS_URL),
    ("CHECKOUT_CANCEL_URL", CHECKOUT_CANCEL_URL),
]:
    if not v:
        missing.append(k)
if missing:
    raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

stripe.api_key = STRIPE_SECRET_KEY

# -----------------------------
# Your tiers / price IDs
# -----------------------------
# IMPORTANT: use ONLY the Stripe price id part (no "-civilian" suffix text)
TIER_CONFIG = {
    "civilian": {"price_id": "price_1T5GP1B9kGqOyQaKAHcpccxx"},
    "fighter":  {"price_id": "price_1T5GRRB9kGqOyQaKN5YoT1LU"},
    "elite":    {"price_id": "price_1T5GRjB9kGqOyQaKLPQ8gswA"},
}

def normalize_tier(t: str) -> str:
    return (t or "").strip().lower()

# -----------------------------
# DB helpers
# -----------------------------
def db_conn():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

def init_db():
    with db_conn() as conn:
        with conn.cursor() as cur:
            # Users: latest tier state
            cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                discord_id TEXT PRIMARY KEY,
                stripe_customer_id TEXT,
                stripe_subscription_id TEXT,
                tier TEXT,
                status TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
            """)

            # Stripe events log (optional but helpful)
            cur.execute("""
            CREATE TABLE IF NOT EXISTS stripe_events (
                event_id TEXT PRIMARY KEY,
                type TEXT,
                payload JSONB,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            """)

            # Job queue for the bot (role updates, announcements, repost jobs if needed)
            cur.execute("""
            CREATE TABLE IF NOT EXISTS bot_jobs (
                id BIGSERIAL PRIMARY KEY,
                job_type TEXT NOT NULL,
                payload JSONB NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMPTZ DEFAULT NOW(),
                processed_at TIMESTAMPTZ
            );
            """)

            # Weight logs
            cur.execute("""
            CREATE TABLE IF NOT EXISTS weight_logs (
                id BIGSERIAL PRIMARY KEY,
                discord_id TEXT NOT NULL,
                weight REAL NOT NULL,
                unit TEXT NOT NULL DEFAULT 'lb',
                note TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            """)

        conn.commit()

def enqueue_job(job_type: str, payload: Dict[str, Any]):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO bot_jobs (job_type, payload) VALUES (%s, %s)",
                (job_type, json.dumps(payload)),
            )
        conn.commit()

# -----------------------------
# FastAPI App
# -----------------------------
app = FastAPI(title="Stripe + Discord Membership API", version="1.0.0")

@app.on_event("startup")
def on_startup():
    init_db()
    log.info("DB initialized.")

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/pricing")
def pricing():
    return {
        "tiers": {
            k: {"price_id": v["price_id"]} for k, v in TIER_CONFIG.items()
        }
    }

# -----------------------------
# Create Checkout Session
# -----------------------------
# Example:
# /create-checkout-session?discord_id=123456789&tier=civilian
@app.get("/create-checkout-session")
def create_checkout_session(discord_id: str, tier: str):
    tier = normalize_tier(tier)
    if tier not in TIER_CONFIG:
        raise HTTPException(status_code=400, detail=f"Invalid tier. Use one of: {list(TIER_CONFIG.keys())}")

    if not discord_id or not discord_id.isdigit():
        raise HTTPException(status_code=400, detail="discord_id must be numeric (copy user ID from Discord Developer Mode).")

    price_id = TIER_CONFIG[tier]["price_id"]

    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=f"{CHECKOUT_SUCCESS_URL}?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=CHECKOUT_CANCEL_URL,
            metadata={
                "discord_id": discord_id,
                "tier": tier,
            },
        )
        return {"url": session.url}
    except Exception as e:
        log.exception("Failed creating checkout session")
        raise HTTPException(status_code=500, detail=str(e))

# -----------------------------
# Stripe Webhook
# -----------------------------
@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Webhook error: {str(e)}")

    event_id = event.get("id")
    event_type = event.get("type")

    # Save event (optional)
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO stripe_events (event_id, type, payload) VALUES (%s, %s, %s) ON CONFLICT (event_id) DO NOTHING",
                    (event_id, event_type, json.dumps(event)),
                )
            conn.commit()
    except Exception:
        log.exception("Failed saving stripe event (non-fatal)")

    # Handle events
    try:
        if event_type == "checkout.session.completed":
            session = event["data"]["object"]
            discord_id = (session.get("metadata") or {}).get("discord_id")
            tier = (session.get("metadata") or {}).get("tier")
            customer_id = session.get("customer")
            subscription_id = session.get("subscription")

            if discord_id and tier:
                with db_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            INSERT INTO users (discord_id, stripe_customer_id, stripe_subscription_id, tier, status, updated_at)
                            VALUES (%s, %s, %s, %s, %s, NOW())
                            ON CONFLICT (discord_id) DO UPDATE SET
                                stripe_customer_id = EXCLUDED.stripe_customer_id,
                                stripe_subscription_id = EXCLUDED.stripe_subscription_id,
                                tier = EXCLUDED.tier,
                                status = EXCLUDED.status,
                                updated_at = NOW();
                        """, (discord_id, customer_id, subscription_id, tier, "active"))
                    conn.commit()

                # Tell the bot to assign role
                enqueue_job("assign_role", {"discord_id": discord_id, "tier": tier})
                log.info(f"Enqueued role assignment: {discord_id} -> {tier}")

        elif event_type in ("customer.subscription.deleted", "customer.subscription.updated"):
            sub = event["data"]["object"]
            subscription_id = sub.get("id")
            status = sub.get("status")  # canceled, active, past_due, etc.

            # Find user by subscription_id
            with db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT discord_id, tier FROM users WHERE stripe_subscription_id = %s", (subscription_id,))
                    row = cur.fetchone()
                conn.commit()

            if row:
                discord_id = row["discord_id"]
                tier = row["tier"]

                # Update status
                with db_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute("UPDATE users SET status=%s, updated_at=NOW() WHERE discord_id=%s", (status, discord_id))
                    conn.commit()

                # Tell bot to refresh roles (remove if canceled/unpaid)
                enqueue_job("sync_roles", {"discord_id": discord_id})
                log.info(f"Enqueued role sync: {discord_id} (status={status})")

    except Exception:
        log.exception("Webhook handler failed (non-fatal to Stripe)")

    return JSONResponse({"received": True})
