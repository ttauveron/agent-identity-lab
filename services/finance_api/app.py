import logging
import os

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from services.common.security import bearer, verify
from services.common.telemetry import setup

app = FastAPI(title="Finance API")
setup(app, "finance-api")
audit = logging.getLogger("finance.audit")


def db():
    return psycopg.connect(os.environ["DATABASE_URL"])


def authenticate(capability: str):
    def dependency(authorization: str | None = Header(default=None)) -> dict:
        claims = verify(bearer(authorization), "finance-api")
        actor = claims.get("act")
        if not isinstance(actor, dict) or actor.get("client_id") != "finance-agent" or not actor.get("sub"):
            raise HTTPException(403, "Delegated finance-agent token required")
        # Neither the prompt nor the caller can supply these identities.
        username = claims.get("preferred_username")
        if not username or username.startswith("service-account-"):
            raise HTTPException(403, "Human subject required")
        scopes = set(claims.get("scope", "").split())
        if capability not in scopes:
            raise HTTPException(403, "Token scope denied")
        return claims

    return dependency


def authorize(claims: dict, capability: str, department: str | None = None):
    actor = claims["act"]
    username = claims["preferred_username"]
    with db() as conn:
        row = conn.execute("SELECT department FROM employees WHERE username=%s AND subject=%s",
                           (username, claims["sub"])).fetchone()
        user_ok = bool(row and conn.execute(
            "SELECT 1 FROM user_permissions WHERE username=%s AND capability=%s",
            (username, capability)).fetchone())
        agent_ok = bool(conn.execute(
            "SELECT 1 FROM agent_permissions WHERE agent=%s AND capability=%s",
            (actor["client_id"], capability)).fetchone())
    allowed = user_ok and agent_ok and (department is None or row[0] == department)
    audit.info("user=%s agent=%s action=%s department=%s decision=%s",
               username, actor["client_id"], capability, department or "-", "allow" if allowed else "deny")
    if not allowed:
        raise HTTPException(403, "Finance policy denied")
    return username


class InvoiceWrite(BaseModel):
    description: str
    amount: float


@app.get("/healthz")
def health():
    return {"ok": True}


@app.get("/invoices/{invoice_id}")
def read_invoice(invoice_id: int, claims: dict = Depends(authenticate("invoices.read"))):
    with db() as conn:
        row = conn.execute("SELECT id, department, description, amount, bank_account FROM invoices WHERE id=%s",
                           (invoice_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Invoice not found")
    authorize(claims, "invoices.read", row[1])
    # Sensitive bank_account is never emitted to this V1 user role.
    return {"id": row[0], "department": row[1], "description": row[2], "amount": float(row[3])}


@app.put("/invoices/{invoice_id}")
def write_invoice(invoice_id: int, payload: InvoiceWrite, claims: dict = Depends(authenticate("invoices.write"))):
    with db() as conn:
        row = conn.execute("SELECT department FROM invoices WHERE id=%s", (invoice_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Invoice not found")
        authorize(claims, "invoices.write", row[0])
        conn.execute("UPDATE invoices SET description=%s, amount=%s WHERE id=%s",
                     (payload.description, payload.amount, invoice_id))
    return {"id": invoice_id, "updated": True}


@app.get("/payments/{payment_id}")
def read_payment(payment_id: int, claims: dict = Depends(authenticate("payments.read"))):
    with db() as conn:
        row = conn.execute("SELECT id, invoice_id, department, amount, status FROM payments WHERE id=%s",
                           (payment_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Payment not found")
    authorize(claims, "payments.read", row[2])
    return {"id": row[0], "invoice_id": row[1], "department": row[2],
            "amount": float(row[3]), "status": row[4]}


@app.post("/payments/{payment_id}/approve")
def approve_payment(payment_id: int, claims: dict = Depends(authenticate("payments.approve"))):
    with db() as conn:
        row = conn.execute("SELECT department FROM payments WHERE id=%s", (payment_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Payment not found")
        authorize(claims, "payments.approve", row[0])
        conn.execute("UPDATE payments SET status='approved' WHERE id=%s", (payment_id,))
    return {"id": payment_id, "status": "approved"}
