"""Test-only deterministic tool selection; all auth, HTTP and storage stay real.

Run only with tests/compose.e2e.yaml. This deliberately excludes LLM behavior.
"""
import json
import sys
from types import SimpleNamespace

import httpx
import uvicorn
from services.common.security import verify
from agent_framework import AgentResponse, AgentSession, Message

from services.finance_agent import app as finance
from services.primary import app as primary


class PrimaryDriver:
    def __init__(self, **_):
        pass

    def create_session(self):
        return AgentSession()

    async def run(self, message, *, session):
        destination = primary._destination.get()
        token = primary._subject_token.get()
        claims = verify(token, destination.client_id)
        assert claims["aud"] in (destination.client_id, [destination.client_id]), "Mixed downstream audience"
        async with httpx.AsyncClient() as http:
            replay = await http.get("http://localhost:8000/v1/models", headers={"Authorization":"Bearer " + token})
        assert replay.status_code == 401, "Downstream token replayed at Primary"
        if destination.client_id == "reporting-agent":
            # Only the token-issuance path is tested for this second destination.
            # The Finance destination below exercises the real A2A/API chain.
            return SimpleNamespace(text="REPORTING: authorized")
        return SimpleNamespace(text=await primary.ask_agent(message))


class FinanceDriver:
    def __init__(self, **_):
        pass

    async def run(self, message, *, session, stream):
        command = json.loads(message)
        tools = {"read_invoice": finance.read_invoice, "approve_payment": finance.approve_payment}
        result = await tools[command["tool"]](command["id"])
        return AgentResponse(messages=Message(role="assistant", contents=[result]))


def main():
    service = sys.argv[1]
    if service == "finance":
        @finance.app.middleware("http")
        async def check_received_token(request, call_next):
            if request.url.path == "/a2a":
                async with httpx.AsyncClient() as http:
                    replay = await http.get("http://primary-agent:8000/v1/models", headers={
                        "Authorization": request.headers.get("authorization", "")})
                assert replay.status_code == 401, "Finance-visible token replayed at Primary"
            return await call_next(request)
    module, driver = {"primary": (primary, PrimaryDriver), "finance": (finance, FinanceDriver)}[service]
    module.Agent = driver
    module.client = lambda: None
    uvicorn.run(module.app, host="0.0.0.0", port=8000, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
