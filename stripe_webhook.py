from fastapi import FastAPI, Request, Header, HTTPException
import stripe
import os

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
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    print("EVENT TYPE:", event["type"])

    return {"success": True}
