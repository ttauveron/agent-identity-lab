"""Optional live Keycloak consent, exchange, and Finance API smoke test."""
import base64
import hashlib
import html
import os
import secrets
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlparse

import httpx

from services.common.security import verify

ISSUER = "https://keycloak.localhost:8443/realms/lab"
REDIRECT = "http://localhost:3080/oauth/openid/callback"


class Forms(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        data = dict(attrs)
        if tag == "form":
            self.current = {"action": html.unescape(data.get("action", "")), "inputs": {}}
            self.forms.append(self.current)
        elif tag == "input" and self.current is not None and data.get("name"):
            self.current["inputs"][data["name"]] = data.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self.current = None


def form(page, marker):
    parser = Forms()
    parser.feed(page)
    return next((item for item in parser.forms if marker in item["action"]), None)


def login(client, username, password, *, scope="openid profile email delegation:client:finance-agent", audience="finance-agent"):
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    response = client.get(ISSUER + "/protocol/openid-connect/auth", params={
        "client_id": "librechat", "redirect_uri": REDIRECT, "response_type": "code",
        "scope": scope, "state": "smoke",
        "code_challenge": challenge, "code_challenge_method": "S256"})
    login_form = form(response.text, "authenticate")
    assert login_form, "Keycloak login form missing"
    response = client.post(urljoin(ISSUER, login_form["action"]), data={"username": username, "password": password})
    for _ in range(4):
        if response.status_code == 302:
            target = response.headers["location"]
            if parse_qs(urlparse(target).query).get("code"):
                break
            response = client.get(target)
        elif response.status_code == 200:
            consent = form(response.text, "consent") or form(response.text, "required-action")
            assert consent, "Keycloak consent form missing"
            response = client.post(urljoin(ISSUER, consent["action"]),
                                   data={**consent["inputs"], "accept": "Yes"})
        else:
            break
    assert response.status_code == 302, f"Login/consent failed: {response.status_code}"
    code = parse_qs(urlparse(response.headers["location"]).query).get("code", [None])[0]
    assert code, (f"Authorization code missing after redirect to {urlparse(response.headers['location']).path}, "
                  f"execution={parse_qs(urlparse(response.headers['location']).query).get('execution')}")
    response = client.post(ISSUER + "/protocol/openid-connect/token", data={
        "grant_type": "authorization_code", "client_id": "librechat",
        "client_secret": os.environ["LIBRECHAT_CLIENT_SECRET"], "code": code,
        "code_verifier": verifier, "redirect_uri": REDIRECT})
    assert response.status_code == 200, f"Authorization code exchange failed: {response.status_code}"
    token = response.json()["access_token"]
    subject = verify(token, audience)
    assert subject["aud"] in (audience, [audience]), "Unexpected user token audience"
    if audience == "finance-agent":
        assert subject["may_act"]["client_id"] == "finance-agent"
    else:
        assert "may_act" not in subject, "Upstream token must not carry downstream delegation"
    return token, subject


def main():
    with httpx.Client(verify="/src/.local-certs/cert.pem", timeout=15) as client:
        subject_token, subject = login(client, "alice", "alice-lab")
        actor_response = client.post(ISSUER + "/protocol/openid-connect/token", data={
            "grant_type": "client_credentials", "client_id": "finance-agent",
            "client_secret": os.environ["FINANCE_CLIENT_SECRET"]})
        assert actor_response.status_code == 200
        actor_token = actor_response.json()["access_token"]
        exchange = client.post(ISSUER + "/protocol/openid-connect/token", data={
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "client_id": "finance-agent", "client_secret": os.environ["FINANCE_CLIENT_SECRET"],
            "subject_token": subject_token,
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "actor_token": actor_token,
            "actor_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "audience": "finance-api"})
        assert exchange.status_code == 200, f"Delegation failed: {exchange.status_code} {exchange.json().get('error_description', '')}"
        delegated = exchange.json()["access_token"]
        claims = verify(delegated, "finance-api")
        assert claims["sub"] == subject["sub"]
        assert claims["act"]["client_id"] == "finance-agent"
        with httpx.Client(base_url="http://finance-api:8000", timeout=10) as api:
            def read(path, token):
                return api.get(path, headers={"Authorization": "Bearer " + token})

            allowed = read("/invoices/1", delegated)
            assert allowed.status_code == 200, allowed.text
            assert "bank_account" not in allowed.json()
            assert read("/invoices/2", delegated).status_code == 403
            assert read("/invoices/1", subject_token).status_code in {401, 403}
            assert read("/invoices/1", actor_token).status_code in {401, 403}
            assert api.post("/payments/1/approve", headers={"Authorization": "Bearer " + delegated}).status_code == 403
    print("Live Keycloak delegation and Finance API checks passed")


if __name__ == "__main__":
    main()
