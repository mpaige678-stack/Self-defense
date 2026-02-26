import os
import stripe
from fastapi import FastAPI, Request, Header, HTTPException

app = FastAPI()

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
endpoint_secret = os.getenv("STRIPE_WEBHOOK_SECRET")

@app.get("/")
def home():
    return {"status": "ok"}  # helps Railway health checks

@app.post("/stripe/webhook")
async def stripe_webhook(request: Request, stripe_signature: str = Header(None)):
    payload = await request.body()

    if not endpoint_secret:
        print("❌ STRIPE_WEBHOOK_SECRET is missing")
        raise HTTPException(status_code=500, detail="Webhook secret not set")

    if not stripe_signature:
        print("❌ Missing Stripe-Signature header")
        raise HTTPException(status_code=400, detail="Missing Stripe-Signature header")

    try:
        event = stripe.Webhook.construct_event(payload, stripe_signature, endpoint_secret)
    except ValueError as e:
        print("❌ Invalid payload:", str(e))
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError as e:
        print("❌ Invalid signature:", str(e))
        raise HTTPException(status_code=400, detail="Invalid signature")

    print("✅ Event received:", event["type"])
    return {"received": True}
