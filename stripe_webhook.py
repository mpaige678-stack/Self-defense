@app.post("/stripe/webhook")
async def stripe_webhook(request: Request, stripe_signature: str = Header(None, alias="Stripe-Signature")):

    payload = await request.body()

    if not endpoint_secret:
        print("❌ STRIPE_WEBHOOK_SECRET missing")
        raise HTTPException(status_code=500, detail="Webhook secret missing")

    if not stripe_signature:
        print("❌ Missing Stripe signature header")
        raise HTTPException(status_code=400, detail="Missing signature")

    try:
        event = stripe.Webhook.construct_event(
            payload,
            stripe_signature,
            endpoint_secret
        )

    except ValueError as e:
        print("❌ Invalid payload:", str(e))
        raise HTTPException(status_code=400, detail="Invalid payload")

    except stripe.error.SignatureVerificationError as e:
        print("❌ Invalid signature:", str(e))
        raise HTTPException(status_code=400, detail="Invalid signature")

    print("✅ Event received:", event["type"])

    return {"received": True}
