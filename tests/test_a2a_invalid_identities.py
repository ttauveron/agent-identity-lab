"""Adversarial signed caller identities at the A2A boundary."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest

from services.finance_agent import app as finance
from test_a2a_transport import server


@pytest.mark.parametrize("case,status", [
    ("invalid-user", 401), ("expired-primary", 401), ("wrong-primary-audience", 401),
    ("wrong-delegated-subject", 403), ("wrong-delegated-actor", 403),
])
def test_invalid_a2a_identity_has_no_execution_or_session(server, monkeypatch, caplog, case, status):
    from datetime import datetime, timedelta, timezone

    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from services.common import security

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(security._jwks, "get_signing_key_from_jwt",
                        lambda _: SimpleNamespace(key=key.public_key()))
    monkeypatch.setattr(finance, "verify", security.verify)

    def signed(subject, audience="finance-agent", **extra):
        return jwt.encode({"iss": security.ISSUER, "aud": audience, "sub": subject,
                           "exp": datetime.now(timezone.utc) + timedelta(minutes=5), **extra},
                          key, algorithm="RS256")

    user = "private-invalid-user-token" if case == "invalid-user" else signed("alice")
    primary_token = signed("primary", azp="primary-agent",
                           **({"exp": datetime.now(timezone.utc) - timedelta(minutes=5)}
                              if case == "expired-primary" else {}),
                           audience="other-api" if case == "wrong-primary-audience" else "finance-agent")
    delegated = signed("bob" if case == "wrong-delegated-subject" else "alice", audience="finance-api",
                       act={"client_id": "other-agent" if case == "wrong-delegated-actor" else "finance-agent"})
    exchanges = []

    async def exchange(*_):
        exchanges.append(True)
        return delegated

    def forbidden(*_, **__):
        pytest.fail("Rejected identity must not execute an agent/tool or access sessions")

    monkeypatch.setattr(finance, "delegated_token", exchange)
    monkeypatch.setattr(finance, "client", forbidden)
    monkeypatch.setattr(finance, "Agent", forbidden)
    monkeypatch.setattr(finance, "sessions", SimpleNamespace(load=forbidden, save=forbidden))

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server)) as http:
            response = await http.post("http://finance-agent:8000/a2a", headers={
                "Authorization": "Bearer " + user, "X-Primary-Authorization": "Bearer " + primary_token},
                json={"jsonrpc": "2.0", "id": "1", "method": "SendMessage", "params": {}})
            assert response.status_code == status
            assert response.json() == {"detail": "Invalid access token" if status == 401 else "Invalid delegation"}
            for token in (user, primary_token, delegated):
                assert token not in response.text
                assert token not in caplog.text

    asyncio.run(run())
    assert len(exchanges) == (1 if case.startswith("wrong-delegated") else 0)
