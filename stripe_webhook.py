import os
import stripe
from fastapi import FastAPI, Request, Header, HTTPException

app = FastAPI()

@app.get("/")
def home():
    return {"status": "webhook running"}

# Make sure this env var exists on Railway:
# STRIPE_WEBHOOK_SECRET=whsec_...
endpoint_secret = os.getenv("STRIPE_WEBHOOK_SECRET")

@app.post("/stripe/webhook")
async def stripe_webhook(
    request: Request,
    stripe_signature: str = Header(None, alias="Stripe-Signature"),
):
    payload = await request.body()

    # 1) Verify signature (required for real Stripe webhooks)
    if not endpoint_secret:
        raise HTTPException(status_code=500, detail="Missing STRIPE_WEBHOOK_SECRET")

    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=stripe_signature,
            secret=endpoint_secret,
        )
    except Exception as e:
        # Signature failed or payload invalid
        raise HTTPException(status_code=400, detail=f"Webhook error: {str(e)}")

    # 2) Route events
    event_type = event["type"]
    print("✅ Stripe event verified:", event_type)

    if event_type == "checkout.session.completed":
        session = event["data"]["object"]
        # TODO: put your action here (discord role, db update, etc.)
        print("🎉 Checkout completed:", session.get("id"), session.get("customer_email"))

    elif event_type == "invoice.payment_failed":
        invoice = event["data"]["object"]
        print("⚠️ Payment failed:", invoice.get("id"))

    elif event_type == "customer.subscription.deleted":
        sub = event["data"]["object"]
        print("🧹 Subscription deleted:", sub.get("id"))

    return {"received": True}
