"""The seven authorization outcomes in docs/architecture.md, at the resource boundary."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from services.common import security
from services.finance_api import app as finance

ALICE = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
BOB = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
AGENT = "finance-agent-service-sub"
SCOPES = "invoices.read invoices.write payments.read payments.approve"


class Database:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def execute(self, query, params):
        if "FROM invoices" in query:
            invoice = {1: (1, "finance", "Laptop fleet", 1200, "CH11-SECRET"),
                       2: (2, "hr", "Recruiting", 500, "CH22-SECRET")}.get(params[0])
            return SimpleNamespace(fetchone=lambda: (invoice[1],) if invoice and query.startswith("SELECT department") else invoice)
        if "FROM payments" in query:
            payment = {1: (1, 1, "finance", 1200, "pending")}.get(params[0])
            return SimpleNamespace(fetchone=lambda: (payment[2],) if payment and query.startswith("SELECT department") else payment)
        if "FROM employees" in query:
            row = {("alice", ALICE): ("finance",), ("bob", BOB): ("hr",)}.get(tuple(params))
            return SimpleNamespace(fetchone=lambda: row)
        if "FROM user_permissions" in query:
            user, capability = params
            permissions = {"alice": {"invoices.read", "invoices.write", "payments.read", "payments.approve"},
                           "bob": {"payments.read"}}
            return SimpleNamespace(fetchone=lambda: (1,) if capability in permissions.get(user, set()) else None)
        if "FROM agent_permissions" in query:
            agent, capability = params
            return SimpleNamespace(fetchone=lambda: (1,) if agent == "finance-agent" and capability in
                                   {"invoices.read", "invoices.write", "payments.read"} else None)
        return SimpleNamespace(fetchone=lambda: None)


@pytest.fixture
def api(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(security._jwks, "get_signing_key_from_jwt", lambda _: SimpleNamespace(key=key.public_key()))
    monkeypatch.setattr(finance, "db", Database)

    def token(user="alice", actor="finance-agent", audience="finance-api", scopes=SCOPES,
              delegated=True, subject=None, expired=False, actor_subject=AGENT):
        payload = {"iss": security.ISSUER, "aud": audience,
                   "sub": subject or (ALICE if user == "alice" else BOB),
                   "preferred_username": user, "scope": scopes,
                   "exp": datetime.now(timezone.utc) + timedelta(minutes=-5 if expired else 5)}
        if delegated:
            payload["act"] = {"client_id": actor, "sub": actor_subject}
        return jwt.encode(payload, key, algorithm="RS256")

    return TestClient(finance.app), token


def get(client, token, path):
    return client.get(path, headers={"Authorization": "Bearer " + token})


def test_user_and_agent_authorized_allow_and_hide_sensitive_field(api):
    client, token = api
    response = get(client, token(), "/invoices/1")
    assert response.status_code == 200
    assert response.json()["amount"] == 1200
    assert "bank_account" not in response.json()
    assert "CH11-SECRET" not in response.text


def test_user_authorized_agent_not_authorized_deny(api):
    client, token = api
    assert client.post("/payments/1/approve", headers={"Authorization": "Bearer " + token()}).status_code == 403


def test_agent_authorized_user_not_authorized_deny(api):
    client, token = api
    assert get(client, token(user="bob"), "/invoices/2").status_code == 403


def test_department_restriction_survives_delegation(api):
    client, token = api
    assert get(client, token(), "/invoices/2").status_code == 403


def test_user_token_alone_deny(api):
    client, token = api
    assert get(client, token(delegated=False), "/invoices/1").status_code == 403


def test_agent_token_alone_deny(api):
    client, token = api
    assert get(client, token(user="service-account-finance-agent", subject=AGENT, delegated=False),
               "/invoices/1").status_code == 403


@pytest.mark.parametrize("change", [{"actor": "other-agent"}, {"audience": "other-api"}])
def test_wrong_actor_or_audience_deny(api, change):
    client, token = api
    assert get(client, token(**change), "/invoices/1").status_code in {401, 403}


def test_missing_token_scope_deny(api):
    client, token = api
    assert get(client, token(scopes="payments.read"), "/invoices/1").status_code == 403


def test_forged_username_with_other_subject_deny(api):
    client, token = api
    assert get(client, token(user="alice", subject=BOB), "/invoices/1").status_code == 403


RESOURCE_ROUTES = [
    ("GET", "/invoices/{id}", None),
    ("PUT", "/invoices/{id}", {"description": "Updated", "amount": 42}),
    ("GET", "/payments/{id}", None),
    ("POST", "/payments/{id}/approve", None),
]


@pytest.mark.parametrize("method,path,payload", RESOURCE_ROUTES)
@pytest.mark.parametrize("credential", ["missing", "malformed", "signature", "expired", "audience"])
def test_authenticate_before_resource_lookup(api, monkeypatch, method, path, payload, credential):
    client, token = api
    headers = {}
    if credential != "missing":
        value = {
            "malformed": "invalid",
            "signature": jwt.encode({"sub": ALICE}, "wrong-key", algorithm="HS256"),
            "expired": token(expired=True),
            "audience": token(audience="other-api"),
        }[credential]
        headers["Authorization"] = "Bearer " + value

    def forbidden_db():
        pytest.fail("Invalid credentials must not access the database")

    monkeypatch.setattr(finance, "db", forbidden_db)
    responses = [client.request(method, path.format(id=id), headers=headers, json=payload)
                 for id in (1, 999)]
    assert [response.status_code for response in responses] == [401, 401]
    assert responses[0].json() == responses[1].json()


@pytest.mark.parametrize("method,path,payload", RESOURCE_ROUTES)
def test_valid_token_missing_resource_returns_not_found(api, method, path, payload):
    client, token = api
    response = client.request(method, path.format(id=999), json=payload,
                              headers={"Authorization": "Bearer " + token()})
    assert response.status_code == 404


def test_authorized_invoice_write(api):
    client, token = api
    response = client.put("/invoices/1", json={"description": "Updated", "amount": 42},
                          headers={"Authorization": "Bearer " + token()})
    assert response.status_code == 200
    assert response.json() == {"id": 1, "updated": True}


def test_invoice_write_department_denied(api):
    client, token = api
    response = client.put("/invoices/2", json={"description": "Updated", "amount": 42},
                          headers={"Authorization": "Bearer " + token()})
    assert response.status_code == 403


def test_authorized_payment_read(api):
    client, token = api
    assert get(client, token(), "/payments/1").status_code == 200


def test_payment_read_department_denied(api):
    client, token = api
    assert get(client, token(user="bob"), "/payments/1").status_code == 403


@pytest.mark.parametrize("method,path,payload", RESOURCE_ROUTES)
@pytest.mark.parametrize("change", [
    {"user": "service-account-finance-agent", "subject": AGENT, "delegated": False},
    {"delegated": False},
    {"actor": "other-agent"},
    {"actor_subject": ""},
    {"user": "service-account-finance-agent", "subject": AGENT},
    {"user": ""},
    {"scopes": ""},
    {"scopes": "unrelated.read"},
])
def test_delegation_required_before_resource_lookup(api, monkeypatch, method, path, payload, change):
    client, token = api

    def forbidden_db():
        pytest.fail("Invalid delegation must not access the database")

    monkeypatch.setattr(finance, "db", forbidden_db)
    headers = {"Authorization": "Bearer " + token(**change)}
    responses = [client.request(method, path.format(id=id), headers=headers, json=payload)
                 for id in (1, 999)]
    assert [response.status_code for response in responses] == [403, 403]
    assert responses[0].json() == responses[1].json()
