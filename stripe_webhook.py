import os
import stripe
from fastapi import FastAPI, Request, Header, HTTPException

app = FastAPI()

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")  # optional for verification, needed for API calls later
endpoint_secret = os.getenv("STRIPE_WEBHOOK_SECRET")

@app.get("/")
def home():
    return {"status": "ok"}

@app.post("/stripe/webhook")
async def stripe_webhook(
    request: Request,
    stripe_signature: str = Header(None, alias="Stripe-Signature")
):
    payload = await request.body()

    if not endpoint_secret:
        raise HTTPException(status_code=500, detail="STRIPE_WEBHOOK_SECRET missing")

    if not stripe_signature:
        raise HTTPException(status_code=400, detail="Missing Stripe-Signature header")

    try:
        event = stripe.Webhook.construct_event(
            payload, stripe_signature, endpoint_secret
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    print("✅ Event received:", event["type"])
    return {"received": True}