# Crypto-app-
docker compose up -d python -m venv .venv source .venv/bin/activate        # Windows: .venv\Scripts\activate pip install -r requirements.txt export DATABASE_URL=postgresql://app:app@localhost:5432/cryptoapp export JWT_SECRET=dev-secret-change-me export WEBHOOK_SECRET=dev-webhook-secret export ENV=dev uvicorn main:app --reload
