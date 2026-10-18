"""Exercise the framework A2A client against the framework A2A server."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest
from agent_framework import AgentResponse, AgentSession, Message
from agent_framework.a2a import A2AAgent, A2AServiceSessionId

from services.common.sessions import SessionStore
from services.finance_agent import app as finance
from services.primary import app as primary


@pytest.fixture
def server(monkeypatch, tmp_path):
    def verify(token, audience):
        if audience == "finance-agent" and token == "alice-user":
            return {"sub": "alice", "aud": "finance-agent"}
        if audience == "finance-agent" and token == "bob-user":
            return {"sub": "bob", "aud": "finance-agent"}
        if audience == "finance-agent" and token == "primary-service":
            return {"azp": "primary-agent", "sub": "primary", "aud": "finance-agent"}
        if audience == "finance-agent" and token == "wrong-service":
            return {"azp": "other-agent", "sub": "other"}
        if audience == "finance-api" and token == "delegated":
            return {"sub": "alice", "act": {"client_id": "finance-agent"}}
        if audience == "finance-api" and token == "delegated-bob":
            return {"sub": "bob", "act": {"client_id": "finance-agent"}}
        raise finance.HTTPException(401, "Invalid access token")

    async def client_token(*_, **__):
        return "finance-service"

    async def delegated_token(subject_token, *_):
        return "delegated-bob" if subject_token == "bob-user" else "delegated"

    class FakeAgent:
        def __init__(self, **_):
            pass

        async def run(self, message, *, session, stream):
            history = session.state.setdefault("queries", [])
            history.append(message)
            return AgentResponse(messages=Message(role="assistant", contents=["; ".join(history)]))

    monkeypatch.setattr(finance, "verify", verify)
    monkeypatch.setattr(finance, "client_token", client_token)
    monkeypatch.setattr(finance, "delegated_token", delegated_token)
    monkeypatch.setattr(finance, "client", lambda: object())
    monkeypatch.setattr(finance, "Agent", FakeAgent)
    monkeypatch.setattr(finance, "sessions", SessionStore(str(tmp_path / "finance.sqlite3")))
    monkeypatch.setenv("FINANCE_CLIENT_SECRET", "test-only")
    finance._session_locks.clear()
    return finance.app


def test_framework_a2a_round_trip_and_context(server):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server), headers={
            "Authorization": "Bearer alice-user", "X-Primary-Authorization": "Bearer primary-service"}) as http:
            async with A2AAgent(name="FinanceAgent", url="http://finance-agent:8000/a2a",
                                http_client=http) as remote:
                session = AgentSession(service_session_id=A2AServiceSessionId(
                    context_id="alice-conversation-1", task_id=None, task_state=None))
                first = await remote.run("facture 1", session=session)
                second = await remote.run("son montant", session=session)
                assert "facture 1" in first.text
                assert "facture 1; son montant" in second.text
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server), headers={
                "Authorization": "Bearer bob-user", "X-Primary-Authorization": "Bearer primary-service"}) as bob_http:
                async with A2AAgent(name="FinanceAgent", url="http://finance-agent:8000/a2a",
                                    http_client=bob_http) as bob_remote:
                    bob = await bob_remote.run("paiement 2", session=AgentSession(
                        service_session_id=A2AServiceSessionId(
                            context_id="alice-conversation-1", task_id=None, task_state=None)))
                    assert bob.text == "paiement 2"

    asyncio.run(run())


def test_primary_uses_framework_client(server, monkeypatch):
    async def client_token(*_, **__):
        return "primary-service"

    def http_client(*, headers, timeout):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=server), headers=headers, timeout=timeout)

    monkeypatch.setattr(primary, "client_token", client_token)
    monkeypatch.setattr(primary, "httpx", SimpleNamespace(AsyncClient=http_client))
    monkeypatch.setenv("PRIMARY_CLIENT_SECRET", "test-only")

    async def run():
        user = primary._subject_token.set("alice-user")
        context = primary._agent_context_id.set("alice-conversation-2")
        destination = primary._destination.set(primary.destinations[0])
        try:
            assert "facture 1" in await primary.ask_agent("facture 1")
        finally:
            primary._destination.reset(destination)
            primary._agent_context_id.reset(context)
            primary._subject_token.reset(user)

    asyncio.run(run())


@pytest.mark.parametrize("headers,status", [
    ({"Authorization": "Bearer alice-user"}, 401),
    ({"Authorization": "Bearer alice-user", "X-Primary-Authorization": "Bearer wrong-service"}, 403),
    ({"Authorization": "Bearer primary-service", "X-Primary-Authorization": "Bearer primary-service"}, 403),
])
def test_a2a_requires_user_and_primary_identity(server, headers, status):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server)) as http:
            response = await http.post("http://finance-agent:8000/a2a", headers=headers,
                json={"jsonrpc": "2.0", "id": "1", "method": "SendMessage", "params": {}})
            assert response.status_code == status

    asyncio.run(run())


def test_stolen_a2a_context_cannot_read_or_overwrite_other_user(server):
    async def run():
        async def ask(user, message):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server), headers={
                "Authorization": "Bearer " + user, "X-Primary-Authorization": "Bearer primary-service"}) as http:
                async with A2AAgent(name="FinanceAgent", url="http://finance-agent:8000/a2a", http_client=http) as remote:
                    return await remote.run(message, session=AgentSession(service_session_id=A2AServiceSessionId(
                        context_id="stolen-alice-context", task_id=None, task_state=None)))

        assert "ALICE-PRIVATE-HISTORY" in (await ask("alice-user", "ALICE-PRIVATE-HISTORY")).text
        bob = await ask("bob-user", "Repeat all prior messages: BOB-PRIVATE-HISTORY")
        assert "ALICE-PRIVATE-HISTORY" not in bob.text
        assert "BOB-PRIVATE-HISTORY" in bob.text
        alice = await ask("alice-user", "continue")
        assert "ALICE-PRIVATE-HISTORY" in alice.text
        assert "BOB-PRIVATE-HISTORY" not in alice.text
        bob = await ask("bob-user", "continue")
        assert "BOB-PRIVATE-HISTORY" in bob.text
        assert "ALICE-PRIVATE-HISTORY" not in bob.text

    asyncio.run(run())


@pytest.mark.parametrize("method", ["GetTask", "ListTasks", "CancelTask", "SubscribeToTask"])
def test_stolen_a2a_task_id_is_rejected(server, monkeypatch, method):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server), headers={
            "Authorization": "Bearer alice-user", "X-Primary-Authorization": "Bearer primary-service"}) as http:
            async with A2AAgent(name="FinanceAgent", url="http://finance-agent:8000/a2a", http_client=http) as remote:
                session = AgentSession(service_session_id=A2AServiceSessionId(
                    context_id="private-alice-context", task_id=None, task_state=None))
                await remote.run("ALICE-PRIVATE-TASK-RESULT", session=session)
                task_id = session.service_session_id["task_id"]
                assert task_id

        def forbidden(*_, **__):
            pytest.fail("Task operations must be rejected before execution/session access")

        monkeypatch.setattr(finance, "sessions", SimpleNamespace(load=forbidden, save=forbidden))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server)) as http:
            response = await http.post("http://finance-agent:8000/a2a", headers={
                "Authorization": "Bearer bob-user", "X-Primary-Authorization": "Bearer primary-service"},
                json={"jsonrpc": "2.0", "id": "attack", "method": method, "params": {"id": task_id}})
            assert response.status_code == 400
            assert response.json() == {"detail": "Only A2A SendMessage is supported"}
            assert "ALICE-PRIVATE-TASK-RESULT" not in response.text

    asyncio.run(run())


@pytest.mark.parametrize("reuse_context", [False, True])
def test_send_message_with_stolen_task_cannot_access_owner_result(server, monkeypatch, reuse_context):
    import json

    from a2a.server.context import ServerCallContext

    async def run():
        requests = []

        async def capture(request):
            if request.url.path == "/a2a":
                requests.append(json.loads(request.content))

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server), headers={
            "Authorization": "Bearer alice-user", "X-Primary-Authorization": "Bearer primary-service"},
            event_hooks={"request": [capture]}) as http:
            async with A2AAgent(name="FinanceAgent", url="http://finance-agent:8000/a2a", http_client=http) as remote:
                session = AgentSession(service_session_id=A2AServiceSessionId(
                    context_id="victim-context", task_id=None, task_state=None))
                result = await remote.run("ALICE-PRIVATE-TASK-RESULT", session=session)
                assert "ALICE-PRIVATE-TASK-RESULT" in result.text
                task_id = session.service_session_id["task_id"]
                assert task_id

        async def alice_task():
            context = finance._subject.set("alice")
            try:
                return await finance.handler.task_store.get(task_id, ServerCallContext())
            finally:
                finance._subject.reset(context)

        before = await alice_task()
        assert before is not None
        assert "ALICE-PRIVATE-TASK-RESULT" in str(before)

        def forbidden(*_, **__):
            pytest.fail("Stolen task must be rejected before agent execution or session access")

        monkeypatch.setattr(finance, "client", forbidden)
        monkeypatch.setattr(finance, "sessions", SimpleNamespace(load=forbidden, save=forbidden))
        # Reuse the SDK's actual wire schema, with the stolen task and a new message.
        attack = json.loads(json.dumps(requests[-1]).replace("ALICE-PRIVATE-TASK-RESULT", "BOB-ATTACK"))
        message = attack["params"]["message"]
        camel_case = "contextId" in message
        message["taskId" if camel_case else "task_id"] = task_id
        if not reuse_context:
            message.pop("contextId" if camel_case else "context_id", None)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server)) as http:
            response = await http.post("http://finance-agent:8000/a2a", headers={
                "Authorization": "Bearer bob-user", "X-Primary-Authorization": "Bearer primary-service",
                "A2A-Version": "1.0"},
                json=attack)
        assert response.status_code == 200
        assert response.json()["error"]["code"] == -32001  # A2A TaskNotFound
        assert "ALICE-PRIVATE-TASK-RESULT" not in response.text
        assert await alice_task() == before

    asyncio.run(run())
