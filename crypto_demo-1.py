"""
Crypto app starter (sandbox, fake money only).

Run the demo:      python crypto_demo.py
Run the real API:  uvicorn crypto_demo:app --reload   (then open /docs)

Install first:     pip install fastapi httpx pyjwt argon2-cffi uvicorn
"""
import hashlib
import hmac
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import uuid4

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

DB_PATH = os.getenv("DB_PATH", "cryptoapp.db")
JWT_SECRET = os.getenv("JWT_SECRET", "dev-only-secret-change-before-going-live-0123456789")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "dev-webhook-secret")
ENV = os.getenv("ENV", "dev")

ASSETS = ("GBP", "BTC")
Asset = Literal["GBP", "BTC"]

app = FastAPI(title="Crypto app (sandbox)")
hasher = PasswordHasher()

# Money is stored as whole numbers: GBP in pence, BTC in satoshis. Never floats.
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  email TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  kyc_status TEXT NOT NULL DEFAULT 'none'
);
CREATE TABLE IF NOT EXISTS balances (
  user_id TEXT NOT NULL REFERENCES users(id),
  asset TEXT NOT NULL,
  available INTEGER NOT NULL DEFAULT 0 CHECK (available >= 0),
  PRIMARY KEY (user_id, asset)
);
CREATE TABLE IF NOT EXISTS ledger_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  asset TEXT NOT NULL,
  amount INTEGER NOT NULL,
  reason TEXT NOT NULL,
  ref_id TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS deposits (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  asset TEXT NOT NULL,
  amount INTEGER NOT NULL CHECK (amount > 0),
  method TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  partner_ref TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS withdrawals (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  asset TEXT NOT NULL,
  amount INTEGER NOT NULL CHECK (amount > 0),
  destination TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS webhook_events (
  event_id TEXT PRIMARY KEY,
  payload TEXT NOT NULL
);
"""


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.close()


init_db()


# ---------- database helpers ----------

def db():
    # One connection per request. Saves if the request succeeds, undoes everything if it fails.
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def credit(conn, user_id, asset, amount, reason, ref_id):
    conn.execute(
        "UPDATE balances SET available = available + ? WHERE user_id = ? AND asset = ?",
        (amount, user_id, asset),
    )
    conn.execute(
        "INSERT INTO ledger_entries (user_id, asset, amount, reason, ref_id) VALUES (?, ?, ?, ?, ?)",
        (user_id, asset, amount, reason, ref_id),
    )


def debit(conn, user_id, asset, amount, reason, ref_id):
    # Only succeeds if enough money is there, so overdrafts are impossible.
    cur = conn.execute(
        "UPDATE balances SET available = available - ? "
        "WHERE user_id = ? AND asset = ? AND available >= ?",
        (amount, user_id, asset, amount),
    )
    if cur.rowcount == 0:
        raise HTTPException(400, "Insufficient balance")
    conn.execute(
        "INSERT INTO ledger_entries (user_id, asset, amount, reason, ref_id) VALUES (?, ?, ?, ?, ?)",
        (user_id, asset, -amount, reason, ref_id),
    )


# ---------- auth ----------

def make_token(user_id: str) -> str:
    payload = {"sub": user_id, "exp": datetime.now(timezone.utc) + timedelta(hours=1)}
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def current_user(authorization: str = Header(...), conn=Depends(db)):
    try:
        data = jwt.decode(authorization.removeprefix("Bearer "), JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid or expired token")
    user = conn.execute(
        "SELECT id, email, kyc_status FROM users WHERE id = ?", (data["sub"],)
    ).fetchone()
    if not user:
        raise HTTPException(401, "Unknown user")
    return user


def verified_user(user=Depends(current_user)):
    # Money only moves after identity verification.
    if user["kyc_status"] != "approved":
        raise HTTPException(403, "Identity verification required")
    return user


class Credentials(BaseModel):
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(min_length=10)


@app.post("/auth/signup", status_code=201)
def signup(body: Credentials, conn=Depends(db)):
    user_id = str(uuid4())
    try:
        conn.execute(
            "INSERT INTO users (id, email, password_hash) VALUES (?, ?, ?)",
            (user_id, body.email.lower(), hasher.hash(body.password)),
        )
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Email already registered")
    for asset in ASSETS:
        conn.execute("INSERT INTO balances (user_id, asset) VALUES (?, ?)", (user_id, asset))
    return {"token": make_token(user_id)}


@app.post("/auth/login")
def login(body: Credentials, conn=Depends(db)):
    user = conn.execute(
        "SELECT id, password_hash FROM users WHERE email = ?", (body.email.lower(),)
    ).fetchone()
    try:
        if not user:
            raise VerifyMismatchError
        hasher.verify(user["password_hash"], body.password)
    except VerifyMismatchError:
        raise HTTPException(401, "Wrong email or password")
    return {"token": make_token(user["id"])}


# ---------- balances ----------

@app.get("/me/balances")
def balances(user=Depends(current_user), conn=Depends(db)):
    rows = conn.execute(
        "SELECT asset, available FROM balances WHERE user_id = ? ORDER BY asset", (user["id"],)
    ).fetchall()
    return {"kyc_status": user["kyc_status"], "balances": [dict(r) for r in rows]}


# ---------- deposits ----------

class DepositIn(BaseModel):
    asset: Asset
    amount: int = Field(gt=0, description="pence for GBP, satoshis for BTC")
    method: Literal["bank", "crypto"]


@app.post("/deposits", status_code=201)
def create_deposit(body: DepositIn, user=Depends(verified_user), conn=Depends(db)):
    # TODO PARTNER: call the partner sandbox here and use their id as partner_ref.
    deposit_id = str(uuid4())
    partner_ref = f"sandbox-{uuid4()}"
    conn.execute(
        "INSERT INTO deposits (id, user_id, asset, amount, method, partner_ref) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (deposit_id, user["id"], body.asset, body.amount, body.method, partner_ref),
    )
    return {"deposit_id": deposit_id, "partner_ref": partner_ref, "status": "pending"}


# ---------- partner webhooks ----------

@app.post("/webhooks/partner")
async def partner_webhook(request: Request, x_signature: str = Header(...), conn=Depends(db)):
    raw = await request.body()
    expected = hmac.new(WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, x_signature):
        raise HTTPException(401, "Bad signature")

    event = json.loads(raw)
    seen = conn.execute(
        "INSERT OR IGNORE INTO webhook_events (event_id, payload) VALUES (?, ?)",
        (event["id"], json.dumps(event)),
    )
    if seen.rowcount == 0:
        return {"status": "duplicate"}

    if event["type"] == "deposit.completed":
        ref = event["data"]["ref"]
        done = conn.execute(
            "UPDATE deposits SET status = 'completed' WHERE partner_ref = ? AND status = 'pending'",
            (ref,),
        )
        if done.rowcount:
            dep = conn.execute(
                "SELECT id, user_id, asset, amount FROM deposits WHERE partner_ref = ?", (ref,)
            ).fetchone()
            credit(conn, dep["user_id"], dep["asset"], dep["amount"], "deposit", dep["id"])
    # TODO PARTNER: handle withdrawal.completed / withdrawal.failed (refund on failure).
    return {"status": "ok"}


# ---------- withdrawals ----------

class WithdrawIn(BaseModel):
    asset: Asset
    amount: int = Field(gt=0)
    destination: str = Field(min_length=3, description="partner token for the bank account or wallet")


@app.post("/withdrawals", status_code=201)
def create_withdrawal(body: WithdrawIn, user=Depends(verified_user), conn=Depends(db)):
    withdrawal_id = str(uuid4())
    conn.execute(
        "INSERT INTO withdrawals (id, user_id, asset, amount, destination) VALUES (?, ?, ?, ?, ?)",
        (withdrawal_id, user["id"], body.asset, body.amount, body.destination),
    )
    debit(conn, user["id"], body.asset, body.amount, "withdrawal", withdrawal_id)
    # TODO PARTNER: ask the partner to send the payout.
    return {"withdrawal_id": withdrawal_id, "status": "pending"}


# ---------- dev only ----------

if ENV == "dev":

    @app.post("/dev/approve-kyc")
    def dev_approve_kyc(user=Depends(current_user), conn=Depends(db)):
        # Replace with the KYC provider's webhook before going live.
        conn.execute("UPDATE users SET kyc_status = 'approved' WHERE id = ?", (user["id"],))
        return {"kyc_status": "approved"}


# ---------- demo: runs the whole money flow, no server needed ----------

def run_demo():
    from fastapi.testclient import TestClient

    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    init_db()
    c = TestClient(app)

    def show(title, r):
        print(f"\n{title}\n   result {r.status_code}: {r.json()}")

    r = c.post("/auth/signup", json={"email": "demo@example.com", "password": "a-long-password-123"})
    show("1. Sign up", r)
    h = {"Authorization": f"Bearer {r.json()['token']}"}

    deposit = {"asset": "GBP", "amount": 10000, "method": "bank"}
    show("2. Deposit before ID check (should be blocked)", c.post("/deposits", headers=h, json=deposit))
    show("3. Fake identity approval", c.post("/dev/approve-kyc", headers=h))

    r = c.post("/deposits", headers=h, json=deposit)
    show("4. Ask for a 100.00 GBP bank deposit (10000 pence)", r)

    body = json.dumps(
        {"id": "evt_1", "type": "deposit.completed", "data": {"ref": r.json()["partner_ref"]}}
    ).encode()
    sig = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    hook = {"x-signature": sig, "Content-Type": "application/json"}
    show("5. Partner confirms the deposit", c.post("/webhooks/partner", content=body, headers=hook))
    show("6. Same confirmation again (ignored, no double credit)", c.post("/webhooks/partner", content=body, headers=hook))
    show("7. Balances", c.get("/me/balances", headers=h))

    out = {"asset": "GBP", "destination": "sandbox-bank-token"}
    show("8. Withdraw 40.00 GBP", c.post("/withdrawals", headers=h, json={**out, "amount": 4000}))
    show("9. Withdraw 500.00 GBP (not enough money)", c.post("/withdrawals", headers=h, json={**out, "amount": 50000}))
    show("10. Final balances (expect 6000 pence = 60.00 GBP)", c.get("/me/balances", headers=h))
    print("\nDone. Everything above used fake money.")


if __name__ == "__main__":
    run_demo()
