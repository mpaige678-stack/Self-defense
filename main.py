# main.py
"""
✅ One file that supports BOTH Railway services:

1) Web service (FastAPI)
   Start command:
     uvicorn main:app --host 0.0.0.0 --port $PORT

2) Worker service (Discord role giver)
   Start command:
     python main.py worker

---------------------------------------
REQUIRED ENV VARS (Web service):
- STRIPE_SECRET_KEY              (sk_test_... or sk_live_...)
- STRIPE_WEBHOOK_SECRET          (whsec_...)
- CHECKOUT_SUCCESS_URL           (ex: https://your-site.com/success OR https://discord.com)
- CHECKOUT_CANCEL_URL            (ex: https://your-site.com/cancel  OR https://discord.com)
- DATABASE_URL                   (postgresql://user:pass@host:port/db)

OPTIONAL ENV VARS (Web service):
- PRICE_ID_CIVILIAN              default uses your IDs below
- PRICE_ID_FIGHTER
- PRICE_ID_ELITE

---------------------------------------
REQUIRED ENV VARS (Worker service):
- DISCORD_TOKEN
- DISCORD_GUILD_ID               (server/guild ID)
- DATABASE_URL

REQUIRED ROLE ENV VARS (Worker service):
- ROLE_ID_CIVILIAN
- ROLE_ID_FIGHTER
- ROLE_ID_ELITE

OPTIONAL (Worker service):
- WORKER_POLL_SECONDS            default 5

---------------------------------------
How it works:
- Web: /create-checkout-session creates a Stripe Checkout session with metadata (discord_id, tier).
- Web: /stripe/webhook verifies Stripe signature and writes "paid" rows to Postgres.
- Worker: polls Postgres for unprocessed paid rows and assigns the right Discord role.
"""

import os
import json
import time
import asyncio
import logging
from typing import Optional, Dict, Any, Tuple

import stripe
import psycopg
from psycopg.rows import dict_row

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

# Discord worker deps
import discord

# -------------------------
# Logging
# -------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("genuine-flow")


# -------------------------
# Helpers
# -------------------------
def env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    return v if v not in (None, "") else default


def must_env(name: str) -> str:
    v = env(name)
    if not v:
        raise RuntimeError(f"Missing required env var: {name}")
    return v


def get_price_map() -> Dict[str, str]:
    # Your provided price IDs (defaults)
    default_civilian = "price_1T5GP1B9kGqOyQaKAHcpccxx"
    default_fighter  = "price_1T5GRRB9kGqOyQaKN5YoT1LU"
    default_elite    = "price_1T5GRjB9kGqOyQaKLPQ8gswA"

    return {
        "civilian": env("PRICE_ID_CIVILIAN", default_civilian),
        "fighter":  env("PRICE_ID_FIGHTER",  default_fighter),
        "elite":    env("PRICE_ID_ELITE",    default_elite),
    }


def db_conn() -> psycopg.Connection:
    # Prefer DATABASE_URL (Railway sets this for Postgres)
    dsn = env("DATABASE_URL")
    if not dsn:
        # Some Railway DB templates expose PGHOST, PGUSER, etc.
        pghost = env("PGHOST")
        pguser = env("PGUSER")
        pgpass = env("PGPASSWORD") or env("PGPASS")
        pgport = env("PGPORT", "5432")
        pgdb   = env("PGDATABASE")
        if all([pghost, pguser, pgpass, pgdb]):
            dsn = f"postgresql://{pguser}:{pgpass}@{pghost}:{pgport}/{pgdb}"
        else:
            raise RuntimeError("DATABASE_URL not set (and PG* vars incomplete).")

    # sslmode required for many hosted PGs
    if "sslmode=" not in dsn:
        joiner = "&" if "?" in dsn else "?"
        dsn = dsn + f"{joiner}sslmode=require"

    return psycopg.connect(dsn, row_factory=dict_row)


def ensure_tables() -> None:
    """
    payments:
      - one row per successful checkout session
      - processed=false until worker assigns role
    """
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                    id BIGSERIAL PRIMARY KEY,
                    session_id TEXT UNIQUE NOT NULL,
                    discord_id TEXT NOT NULL,
                    tier TEXT NOT NULL,
                    amount_total BIGINT,
                    currency TEXT,
                    status TEXT NOT NULL,
                    processed BOOLEAN NOT NULL DEFAULT FALSE,
                    processed_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)
            conn.commit()


def insert_paid_payment(
    session_id: str,
    discord_id: str,
    tier: str,
    amount_total: Optional[int],
    currency: Optional[str],
    status: str
) -> None:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO payments (session_id, discord_id, tier, amount_total, currency, status, processed)
                VALUES (%s, %s, %s, %s, %s, %s, FALSE)
                ON CONFLICT (session_id) DO UPDATE
                SET status = EXCLUDED.status
                """,
                (session_id, discord_id, tier, amount_total, currency, status)
            )
            conn.commit()


def fetch_unprocessed(limit: int = 10) -> list[dict]:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM payments
                WHERE processed = FALSE
                  AND status IN ('paid', 'complete', 'succeeded', 'completed')
                ORDER BY created_at ASC
                LIMIT %s
                """,
                (limit,)
            )
            return cur.fetchall()


def mark_processed(payment_id: int) -> None:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE payments
                SET processed = TRUE,
                    processed_at = NOW()
                WHERE id = %s
                """,
                (payment_id,)
            )
            conn.commit()


def role_id_for_tier(tier: str) -> Optional[int]:
    tier = (tier or "").lower().strip()
    key = {
        "civilian": "ROLE_ID_CIVILIAN",
        "fighter":  "ROLE_ID_FIGHTER",
        "elite":    "ROLE_ID_ELITE",
    }.get(tier)

    if not key:
        return None
    v = env(key)
    return int(v) if v and v.isdigit() else None


# -------------------------
# FastAPI (Web Service)
# -------------------------
app = FastAPI(title="Genuine Flow", version="0.1.0")


@app.on_event("startup")
async def startup_event():
    # Never crash on import/startup for missing envs — just warn.
    try:
        ensure_tables()
        log.info("DB tables ensured ✅")
    except Exception as e:
        log.exception(f"DB init failed: {e}")

    sk = env("STRIPE_SECRET_KEY")
    if sk:
        stripe.api_key = sk
        log.info("Stripe secret key loaded ✅")
    else:
        log.warning("STRIPE_SECRET_KEY not set (checkout creation will fail until you add it).")


@app.get("/")
async def root():
    return {"ok": True, "service": "genuine-flow", "hint": "Open /docs for endpoints"}


@app.get("/health")
async def health():
    # lightweight DB ping
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")
                _ = cur.fetchone()
        return {"ok": True, "db": True}
    except Exception as e:
        return {"ok": False, "db": False, "error": str(e)}


@app.get("/create-checkout-session")
async def create_checkout_session(discord_id: str, tier: str):
    """
    Call like:
      /create-checkout-session?discord_id=123456789&tier=elite

    Returns JSON:
      {"url": "https://checkout.stripe.com/..."}
    """
    if not env("STRIPE_SECRET_KEY"):
        raise HTTPException(status_code=500, detail="STRIPE_SECRET_KEY is missing")
    stripe.api_key = must_env("STRIPE_SECRET_KEY")

    price_map = get_price_map()
    tier_norm = (tier or "").lower().strip()

    if tier_norm not in price_map:
        raise HTTPException(status_code=400, detail=f"Invalid tier. Use one of: {list(price_map.keys())}")

    success_url = env("CHECKOUT_SUCCESS_URL")
    cancel_url = env("CHECKOUT_CANCEL_URL")
    if not success_url or not cancel_url:
        raise HTTPException(status_code=500, detail="CHECKOUT_SUCCESS_URL or CHECKOUT_CANCEL_URL missing")

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{"price": price_map[tier_norm], "quantity": 1}],
            mode="payment",
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={
                "discord_id": str(discord_id),
                "tier": tier_norm,
            },
        )
        return {"url": session.url}
    except Exception as e:
        log.exception(f"Stripe checkout create failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    """
    Stripe sends events here.
    We verify signature, then on checkout.session.completed we write a row to DB.
    Worker later assigns the role.
    """
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    whsec = env("STRIPE_WEBHOOK_SECRET")

    if not whsec:
        raise HTTPException(status_code=500, detail="STRIPE_WEBHOOK_SECRET missing")
    if not sig_header:
        raise HTTPException(status_code=400, detail="Missing stripe-signature header")

    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=whsec,
        )
    except Exception as e:
        log.warning(f"Webhook signature verification failed: {e}")
        raise HTTPException(status_code=400, detail="Invalid signature")

    event_type = event.get("type")
    data_object = (event.get("data") or {}).get("object") or {}

    # Handle the event
    if event_type == "checkout.session.completed":
        # session object
        session_id = data_object.get("id")
        payment_status = data_object.get("payment_status") or "completed"
        amount_total = data_object.get("amount_total")
        currency = data_object.get("currency")
        metadata = data_object.get("metadata") or {}
        discord_id = metadata.get("discord_id")
        tier = metadata.get("tier")

        if not session_id or not discord_id or not tier:
            log.warning(f"checkout.session.completed missing metadata/session_id: {session_id}, {metadata}")
            return JSONResponse({"received": True, "ignored": True})

        # Stripe uses payment_status="paid" for paid sessions
        status = "paid" if str(payment_status).lower() == "paid" else str(payment_status).lower()

        try:
            insert_paid_payment(
                session_id=session_id,
                discord_id=str(discord_id),
                tier=str(tier).lower().strip(),
                amount_total=amount_total,
                currency=currency,
                status=status
            )
            log.info(f"Saved payment ✅ session={session_id} discord={discord_id} tier={tier} status={status}")
        except Exception as e:
            log.exception(f"DB insert failed: {e}")
            raise HTTPException(status_code=500, detail="DB insert failed")

    # You can add more event types if you want:
    # elif event_type == "payment_intent.succeeded": ...

    return JSONResponse({"received": True})


# -------------------------
# Discord Worker
# -------------------------
class RoleWorker(discord.Client):
    def __init__(self):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.members = True  # needed to fetch members / assign roles
        super().__init__(intents=intents)

        self.guild_id = int(must_env("DISCORD_GUILD_ID"))
        self.poll_seconds = int(env("WORKER_POLL_SECONDS", "5"))

    async def on_ready(self):
        log.info(f"Worker logged in as {self.user} ✅")
        await self.worker_loop()

    async def worker_loop(self):
        while True:
            try:
                # fetch some unprocessed payments
                rows = fetch_unprocessed(limit=10)
                if rows:
                    log.info(f"Worker found {len(rows)} unprocessed payments...")
                for row in rows:
                    await self.process_row(row)
            except Exception as e:
                log.exception(f"Worker loop error: {e}")

            await asyncio.sleep(self.poll_seconds)

    async def process_row(self, row: Dict[str, Any]):
        payment_id = row["id"]
        discord_id = int(row["discord_id"])
        tier = row["tier"]

        role_id = role_id_for_tier(tier)
        if not role_id:
            log.warning(f"No role mapping for tier={tier}. Marking processed to avoid loop.")
            mark_processed(payment_id)
            return

        guild = self.get_guild(self.guild_id)
        if guild is None:
            try:
                guild = await self.fetch_guild(self.guild_id)
            except Exception as e:
                log.warning(f"Cannot access guild {self.guild_id}: {e}")
                return

        # get member
        member = guild.get_member(discord_id)
        if member is None:
            try:
                member = await guild.fetch_member(discord_id)
            except Exception as e:
                log.warning(f"Cannot fetch member {discord_id}: {e}")
                return

        role = guild.get_role(role_id)
        if role is None:
            log.warning(f"Role id {role_id} not found in guild. Check ROLE_ID_* env vars.")
            return

        try:
            if role in member.roles:
                log.info(f"Member {discord_id} already has role {tier}. Marking processed.")
                mark_processed(payment_id)
                return

            await member.add_roles(role, reason="Stripe payment confirmed")
            log.info(f"✅ Assigned role {tier} to member {discord_id}")
            mark_processed(payment_id)

        except Exception as e:
            log.warning(f"Failed to add role: {e}")
            # Don't mark processed so it can retry later.


def run_worker():
    token = must_env("DISCORD_TOKEN")
    # Ensure DB schema exists
    ensure_tables()
    client = RoleWorker()
    client.run(token)


# -------------------------
# Entrypoint (Worker mode)
# -------------------------
if __name__ == "__main__":
    # Run worker with: python main.py worker
    # (Web runs with uvicorn main:app ...)
    if len(os.sys.argv) >= 2 and os.sys.argv[1].lower() == "worker":
        run_worker()
    else:
        print("This file is meant to be run as:")
        print("  Web:    uvicorn main:app --host 0.0.0.0 --port $PORT")
        print("  Worker: python main.py worker")