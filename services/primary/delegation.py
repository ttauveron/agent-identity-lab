"""Per-user, conversation and destination consent, outside LLM state.

This single-process prototype discards refresh tokens. Expiry or restart requires
new consent; an upstream user token is never a downstream fallback.
"""
import os
import re
import time
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx
from fastapi import HTTPException

from services.common.security import ISSUER, TOKEN_URL, verify


@dataclass(frozen=True)
class Destination:
    client_id: str
    model: str
    url: str
    actor_scope: str


@dataclass
class Grant:
    expires: float
    device_code: str | None = field(default=None, repr=False)
    link: str | None = field(default=None, repr=False)
    interval: float = 5
    next_poll: float = 0
    access_token: str | None = field(default=None, repr=False)


class Delegations:
    def __init__(self, *, capacity=1024):
        self.grants: dict[tuple[str, str, str], Grant] = {}
        self.capacity = capacity

    async def _post(self, url, data):
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(url, data={
                    "client_id": "primary-agent",
                    "client_secret": os.environ["PRIMARY_CLIENT_SECRET"], **data})
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError()
            return response.status_code, body
        except (httpx.RequestError, ValueError):
            raise HTTPException(502, "Identity provider unavailable") from None

    async def token(self, subject: str, conversation: str, destination: Destination) -> tuple[str | None, str | None]:
        # The caller serializes requests for this user's conversation.
        now = time.time()
        for key, grant in list(self.grants.items()):
            if grant.expires <= now:
                del self.grants[key]
        key = (subject, conversation, destination.client_id)
        grant = self.grants.get(key)
        if grant and grant.access_token:
            return grant.access_token, None
        if not grant:
            if len(self.grants) >= self.capacity:
                raise HTTPException(503, "Delegation capacity reached")
            # Reserve before awaiting I/O so other conversations cannot all
            # pass the capacity check concurrently. Cancellation frees it.
            self.grants[key] = Grant(now + 900)
            try:
                status, body = await self._post(TOKEN_URL.removesuffix("/token") + "/auth/device", {
                    "scope": "openid delegation:client:" + destination.client_id})
                try:
                    if status != 200:
                        raise ValueError()
                    code, user_code = body["device_code"], body["user_code"]
                    expires, interval = float(body["expires_in"]), float(body.get("interval", 5))
                    if (not isinstance(code, str) or not code or not isinstance(user_code, str)
                            or not re.fullmatch(r"[A-Za-z0-9-]{4,32}", user_code)
                            or not 0 < expires <= 900 or not 0 < interval <= 60):
                        raise ValueError()
                except (KeyError, TypeError, ValueError):
                    raise HTTPException(502, "Identity provider unavailable") from None
            finally:
                self.grants.pop(key, None)
            # Use our configured issuer, never an upstream arbitrary redirect URL.
            link = ISSUER + "/device?user_code=" + quote(user_code)
            self.grants[key] = Grant(now + expires, code, link, interval, now + interval)
            return None, link
        if now < grant.next_poll:
            return None, grant.link
        status, body = await self._post(TOKEN_URL, {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": grant.device_code})
        grant.next_poll = time.time() + grant.interval
        error = body.get("error")
        if status == 400 and error in ("authorization_pending", "slow_down"):
            if error == "slow_down":
                grant.interval += 5
                grant.next_poll = time.time() + grant.interval
            return None, grant.link
        if status != 200:
            if error in ("access_denied", "expired_token", "invalid_grant"):
                self.grants.pop(key, None)
                raise HTTPException(403, "Delegation refused or expired")
            raise HTTPException(502, "Identity provider unavailable")
        # A device link can be opened by someone else. Bind the completed grant
        # to the independently authenticated chat user, not to browser hints.
        self.grants.pop(key, None)
        access = body.get("access_token")
        if not isinstance(access, str) or not access:
            raise HTTPException(502, "Identity provider unavailable")
        claims = verify(access, destination.client_id)
        may_act = claims.get("may_act")
        if (claims["sub"] != subject or claims["aud"] not in (destination.client_id, [destination.client_id])
                or not isinstance(may_act, dict) or may_act.get("client_id") != destination.client_id
                or not isinstance(may_act.get("sub"), str) or not may_act["sub"]
                or may_act["sub"] == subject or may_act.get("iss") not in (None, ISSUER)):
            raise HTTPException(403, "Invalid downstream delegation")
        self.grants[key] = Grant(float(claims["exp"]), access_token=access)
        return access, None
