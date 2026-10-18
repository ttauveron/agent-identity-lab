"""Inspect real HTTPX/FastAPI telemetry with synthetic OAuth secrets, offline."""
import asyncio
import json
import logging
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from services.common import security, telemetry
from services.primary import delegation

SECRET = "test-client-secret-marker"
SUBJECT = "test-subject-token-marker"
ACTOR = "test-actor-token-marker"
ACCESS = "test-access-token-marker"
PRIMARY = "test-primary-token-marker"
CUSTOM = "test-custom-secret-marker"
DEVICE = "test-device-code-marker"
SENSITIVE = (SECRET, SUBJECT, ACTOR, ACCESS, PRIMARY, CUSTOM, DEVICE)


@pytest.fixture
def oauth_server():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
            received.append((self.path, parse_qs(body), dict(self.headers)))
            if "/disconnect" in self.path:
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            status = 400 if "/deny" in self.path else 200
            payload = {"access_token": ACCESS, "device_code": DEVICE, "user_code":"ABCD-EFGH",
                       "expires_in":300, "interval":5} if status == 200 else {
                "error": "invalid_grant", "error_description": " ".join(SENSITIVE)}
            encoded = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", received
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


@pytest.fixture
def observed(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setenv("OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_CLIENT_REQUEST", ".*")
    monkeypatch.setenv("OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_REQUEST", ".*")
    monkeypatch.setenv("OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SANITIZE_FIELDS", "x-existing-secret")
    # Service modules were imported during collection. Isolate the real HTTPX
    # instrumentor and provider, then restore the previous instrumentation.
    instrumentor = HTTPXClientInstrumentor()
    was_instrumented = instrumentor.is_instrumented_by_opentelemetry
    if was_instrumented:
        instrumentor.uninstrument()
    monkeypatch.setattr(telemetry.trace, "set_tracer_provider", lambda _: None)
    exporter = InMemorySpanExporter()
    real_provider = telemetry.TracerProvider
    providers = []

    def provider(**kwargs):
        result = real_provider(**kwargs)
        result.add_span_processor(SimpleSpanProcessor(exporter))
        providers.append(result)
        return result

    monkeypatch.setattr(telemetry, "TracerProvider", provider)
    app = FastAPI()
    telemetry.setup(app, "oauth-secrecy-test")
    caplog.clear()
    try:
        yield app, exporter, caplog
    finally:
        FastAPIInstrumentor.uninstrument_app(app)
        instrumentor.uninstrument()
        monkeypatch.undo()
        if was_instrumented:
            instrumentor.instrument()
        for provider in providers:
            provider.shutdown()


def assert_private(exporter, caplog, errors=()):
    spans = exporter.get_finished_spans()
    assert spans, "No spans collected: secrecy assertions must observe real telemetry"
    assert caplog.records, "No logs collected"
    # to_json includes resource, attributes, status, events and exception fields.
    output = caplog.text + "\n".join(span.to_json() for span in spans) + json.dumps(list(errors))
    for marker in SENSITIVE:
        assert marker not in output, f"Sensitive marker leaked: {marker}"
    return spans


@pytest.mark.parametrize("helper", ["client", "exchange", "device-start", "device-poll"])
@pytest.mark.parametrize("outcome", ["success", "deny", "disconnect"])
def test_oauth_bodies_stay_out_of_logs_and_spans(monkeypatch, oauth_server, observed, helper, outcome):
    _, exporter, caplog = observed
    url, received = oauth_server
    path = "/" + outcome
    monkeypatch.setattr(security, "TOKEN_URL", url + path)
    errors = []
    manager = delegation.Delegations()
    monkeypatch.setenv("PRIMARY_CLIENT_SECRET", SECRET)
    monkeypatch.setattr(delegation, "TOKEN_URL", url + path + "/token")
    monkeypatch.setattr(delegation, "verify", lambda *_: {"sub":"alice", "aud":"finance-agent",
        "exp":9999999999,"may_act":{"client_id":"finance-agent","sub":"service"}})
    destination = delegation.Destination("finance-agent","finance-lab","http://unused","finance-agent-audience")
    if helper == "device-poll":
        manager.grants[("alice","chat","finance-agent")] = delegation.Grant(9999999999, device_code=DEVICE)

    async def run():
        if helper == "client": call = security.client_token("finance-agent", SECRET)
        elif helper == "exchange": call = security.delegated_token(SUBJECT, ACTOR, SECRET)
        else: call = manager.token("alice","chat",destination)
        if outcome == "success":
            result = await call
            if helper == "device-start": assert result == (None, security.ISSUER+"/device?user_code=ABCD-EFGH")
            elif helper == "device-poll": assert result == (ACCESS,None)
            else: assert result == ACCESS
        else:
            with pytest.raises(HTTPException) as caught:
                await call
            expected_status = 403 if helper in ("exchange","device-poll") and outcome == "deny" else 502
            expected_detail = ("Delegation refused or expired" if helper == "device-poll" else "Delegation refused") if expected_status == 403 else "Identity provider unavailable"
            assert (caught.value.status_code, caught.value.detail) == (expected_status, expected_detail)
            errors.append({"detail": caught.value.detail})

    asyncio.run(run())
    assert len(received) == 1
    fields = received[0][1]
    assert fields["client_secret"] == [SECRET]
    if helper == "exchange":
        assert fields["subject_token"] == [SUBJECT]
        assert fields["actor_token"] == [ACTOR]
    elif helper == "device-poll":
        assert fields["device_code"] == [DEVICE]
    elif helper == "device-start":
        assert fields["scope"] == ["openid delegation:client:finance-agent"]
    spans = assert_private(exporter, caplog, errors)
    client_spans = [span for span in spans if span.kind.name == "CLIENT"]
    assert len(client_spans) == 1
    assert path in json.dumps(dict(client_spans[0].attributes))
    if outcome == "disconnect":
        assert client_spans[0].status.status_code.name == "ERROR"
        assert any(event.name == "exception" for event in client_spans[0].events)


def test_authorization_headers_are_redacted_on_inbound_and_outbound_calls(oauth_server, observed):
    app, exporter, caplog = observed
    url, received = oauth_server

    @app.post("/forward")
    async def forward(request: Request):
        async with httpx.AsyncClient() as client:
            response = await client.post(url + "/headers", headers={
                "Authorization": request.headers["authorization"],
                "X-Primary-Authorization": request.headers["x-primary-authorization"],
                "X-Existing-Secret": request.headers["x-existing-secret"],
                "X-Request-ID": "public-correlation-id"})
        return {"upstream_status": response.status_code}

    with TestClient(app) as client:
        response = client.post("/forward", headers={
            "Authorization": "Bearer " + SUBJECT,
            "X-Primary-Authorization": "Bearer " + PRIMARY,
            "X-Existing-Secret": CUSTOM,
            "X-Request-ID": "public-correlation-id"})
    assert response.status_code == 200
    assert response.json() == {"upstream_status": 200}
    assert received[0][2]["Authorization"] == "Bearer " + SUBJECT
    assert received[0][2]["X-Primary-Authorization"] == "Bearer " + PRIMARY
    spans = assert_private(exporter, caplog, [response.json()])
    for kind in ("SERVER", "CLIENT"):
        matching = [span for span in spans if span.kind.name == kind]
        assert matching, f"Missing {kind} span"
        attributes = matching[0].attributes
        for name in ("authorization", "x_primary_authorization", "x_existing_secret"):
            assert attributes[f"http.request.header.{name}"] == ("[REDACTED]",)
        assert attributes["http.request.header.x_request_id"] == ("public-correlation-id",)
