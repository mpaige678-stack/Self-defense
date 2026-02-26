web: uvicorn stripe_webhook:app --host 0.0.0.0 --port $PORT
worker: python tasks_runner.py
bot: python main.py
