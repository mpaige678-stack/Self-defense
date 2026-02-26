from fastapi import FastAPI, Request

app = FastAPI()

@app.get("/")
def home():
    return {"status": "webhook running"}

@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    data = await request.json()
    print("Stripe event:", data)
    return {"received": True}
