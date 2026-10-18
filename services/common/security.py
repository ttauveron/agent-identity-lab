"""JWT and OAuth helpers. Never log token values or upstream response bodies."""
import os

import httpx
import jwt
from fastapi import HTTPException

ISSUER = os.getenv("OIDC_ISSUER", "https://keycloak.localhost:8443/realms/lab")
KEYCLOAK_INTERNAL = os.getenv("KEYCLOAK_INTERNAL", "http://keycloak:8080")
JWKS_URL = f"{KEYCLOAK_INTERNAL}/realms/lab/protocol/openid-connect/certs"
TOKEN_URL = f"{KEYCLOAK_INTERNAL}/realms/lab/protocol/openid-connect/token"
_jwks = jwt.PyJWKClient(JWKS_URL)


def bearer(value: str | None) -> str:
    if not value or not value.startswith("Bearer ") or not value[7:]:
        raise HTTPException(401, "Bearer token required")
    return value[7:]


def verify(token: str, audience: str) -> dict:
    try:
        key = _jwks.get_signing_key_from_jwt(token).key
        return jwt.decode(token, key, algorithms=["RS256"], issuer=ISSUER, audience=audience,
                          options={"require": ["exp", "iss", "sub", "aud"]})
    except (jwt.PyJWTError, ValueError):
        raise HTTPException(401, "Invalid access token") from None


async def client_token(client_id: str, secret: str, *, scope: str | None = None) -> str:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(TOKEN_URL, data={"grant_type": "client_credentials",
                "client_id": client_id, "client_secret": secret, **({"scope": scope} if scope else {})})
    except httpx.RequestError:
        raise HTTPException(502, "Identity provider unavailable") from None
    if response.status_code != 200:
        raise HTTPException(502, "Identity provider unavailable")
    return response.json()["access_token"]


async def delegated_token(subject_token: str, actor_token: str, secret: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(TOKEN_URL, data={
                "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                "client_id": "finance-agent", "client_secret": secret,
                "subject_token": subject_token,
                "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
                "actor_token": actor_token,
                "actor_token_type": "urn:ietf:params:oauth:token-type:access_token",
                "audience": "finance-api",
            })
    except httpx.RequestError:
        raise HTTPException(502, "Identity provider unavailable") from None
    if response.status_code != 200:
        raise HTTPException(403, "Delegation refused")
    return response.json()["access_token"]
