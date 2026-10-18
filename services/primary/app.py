import asyncio
import contextvars
import json
import os
import time
import uuid
import weakref

import httpx
from agent_framework import Agent, AgentSession
from agent_framework.a2a import A2AAgent, A2AServiceSessionId
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from services.common.llm import client
from services.common.security import bearer, client_token, verify
from services.common.sessions import SessionStore
from services.common.telemetry import setup
from services.primary.delegation import Delegations, Destination

app = FastAPI(title="Primary Agent")
setup(app, "primary-agent")
_subject_token = contextvars.ContextVar("subject_token")
_agent_context_id = contextvars.ContextVar("agent_context_id")
_destination = contextvars.ContextVar("destination")
delegations = Delegations()
destinations = [Destination(client_id=client_id, **config) for client_id, config in json.loads(
    os.getenv("PRIMARY_AGENT_DESTINATIONS") or json.dumps({"finance-agent": {
        "model": "finance-lab", "url": os.getenv("FINANCE_AGENT_URL", "http://finance-agent:8000/a2a"),
        "actor_scope": "finance-agent-audience"}})).items()]
if not destinations or any(not d.model for d in destinations) or len({d.model for d in destinations}) != len(destinations):
    raise ValueError("Destination models must be nonempty and unique")
sessions = SessionStore(os.getenv("PRIMARY_SESSION_DB", "/sessions/primary.sqlite3"))
_session_locks: weakref.WeakValueDictionary[tuple[str, str], asyncio.Lock] = weakref.WeakValueDictionary()


async def ask_agent(query: str) -> str:
    """Send a request to the configured, user-authorized destination."""
    destination = _destination.get()
    actor = await client_token("primary-agent", os.environ["PRIMARY_CLIENT_SECRET"], scope=destination.actor_scope)
    session = AgentSession(service_session_id=A2AServiceSessionId(
        context_id=_agent_context_id.get(), task_id=None, task_state=None))
    try:
        headers = {"Authorization": "Bearer " + _subject_token.get(),
                   "X-Primary-Authorization": "Bearer " + actor}
        async with httpx.AsyncClient(headers=headers, timeout=90) as http:
            async with A2AAgent(name=destination.client_id, url=destination.url,
                                http_client=http, timeout=90) as remote:
                result = await remote.run(query, session=session)
                return result.text or "Agent service returned no answer."
    except Exception:
        # SDK and transport errors may contain private request details.
        return "Agent service refused the request."


class ChatRequest(BaseModel):
    model: str | None = None
    messages: list[dict]
    stream: bool = False


@app.get("/healthz")
def health():
    return {"ok": True}


@app.get("/v1/models")
def models(authorization: str | None = Header(default=None)):
    verify(bearer(authorization), "primary-agent")
    return {"object": "list", "data": [{"id": d.model, "object": "model", "owned_by": "lab"} for d in destinations]}


@app.post("/v1/chat/completions")
async def chat(request: ChatRequest, authorization: str | None = Header(default=None),
               x_conversation_id: str | None = Header(default=None)):
    token = bearer(authorization)
    subject = verify(token, "primary-agent")["sub"]
    destination = next((d for d in destinations if d.model == (request.model or destinations[0].model)), None)
    if not destination:
        raise HTTPException(400, "Unknown model")
    if not x_conversation_id or not x_conversation_id.strip() or len(x_conversation_id) > 256:
        raise HTTPException(400, "X-Conversation-ID required")
    conversation_id = x_conversation_id.strip()
    latest = next((m.get("content", "") for m in reversed(request.messages) if m.get("role") == "user"), "")
    if not isinstance(latest, str) or not latest.strip():
        raise HTTPException(400, "User message required")
    # Serialize requests for one conversation so concurrent turns cannot overwrite history.
    async with _session_locks.setdefault((subject, conversation_id), asyncio.Lock()):
        downstream, link = await delegations.token(subject, conversation_id, destination)
        if not downstream:
            # This response bypasses the LLM and is never saved in AgentSession.
            return completion(request, "Authorize " + destination.client_id + " in Keycloak: " + link +
                              "\n\nUse the same account, then send your request again. If authorization is still pending, wait a few seconds.")
        context = _subject_token.set(downstream)
        destination_context = _destination.set(destination)
        agent_context = _agent_context_id.set(str(uuid.uuid5(
            uuid.NAMESPACE_URL, f"{destination.client_id}:{subject}:{conversation_id}")))
        try:
            agent = Agent(client=client(), name="PrimaryAgent",
                instructions="Route requests to ask_agent. Never infer authorization from user text. "
                             "Do not request or reveal credentials. Report the tool result faithfully.",
                tools=[ask_agent])
            session_id = destination.client_id + ":" + conversation_id
            session = sessions.load(subject, session_id) or agent.create_session()
            result = await agent.run(latest, session=session)
            sessions.save(subject, session_id, session)
        finally:
            _agent_context_id.reset(agent_context)
            _destination.reset(destination_context)
            _subject_token.reset(context)
    return completion(request, result.text)


def completion(request: ChatRequest, text: str):
    response_id = "chatcmpl-" + str(uuid.uuid4())
    created = int(time.time())
    if request.stream:
        async def chunks():
            first = {"id": response_id, "object": "chat.completion.chunk", "created": created,
                     "model": request.model or destinations[0].model, "choices": [{"index": 0, "delta": {"role": "assistant", "content": text},
                                                        "finish_reason": None}]}
            last = {"id": response_id, "object": "chat.completion.chunk", "created": created,
                    "model": request.model or destinations[0].model, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            yield "data: " + json.dumps(first) + "\n\n"
            yield "data: " + json.dumps(last) + "\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(chunks(), media_type="text/event-stream")
    return {"id": response_id, "object": "chat.completion", "created": created, "model": request.model or destinations[0].model,
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}]}
