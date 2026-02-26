import os
import stripe
from fastapi import FastAPI, Request, Header, HTTPException

app = FastAPI()

endpoint_secret = os.getenv("STRIPE_WEBHOOK_SECRET")

@app.post("/stripe/webhook")
async def stripe_webhook(request: Request, stripe_signature: str = Header(None)):

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

    print("✅ Event received:", event["type"])

    return {"received": True}
