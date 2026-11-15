from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import signal
import threading
from collections.abc import Sequence
from typing import Any, Literal
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
from mcp.server.auth.provider import AuthorizationParams, RegistrationError, TokenError
from mcp.server.mcpserver import MCPServer
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl
from starlette.applications import Starlette
from starlette.routing import Route

import clitg.mcp_app as mcp_app
import clitg.mcp_store as mcp_store
from clitg.errors import ClitgError
from clitg.mcp_app import (
    HttpServe,
    QuietServer,
    ServeOptions,
    bind_socket,
    build_server,
    configure_logging,
    resolve_issuer,
    serve_stdio,
    transport_security,
)
from clitg.mcp_auth import PAIR_PATH, ClitgOAuthProvider, StaticTokenVerifier
from clitg.mcp_store import McpStore
from clitg.mcp_tools import CliRunner
from clitg.models import ErrorCode
from clitg.storage import Paths

ISSUER = "http://127.0.0.1:9"
RESOURCE = f"{ISSUER}/mcp"
REDIRECT = "https://chatgpt.com/connector/oauth/abc"


class FakeRunner(CliRunner):
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        super().__init__("demo")
        self.response = {"ok": True, "data": {}} if response is None else response
        self.error = error
        self.calls: list[tuple[list[str], str | None]] = []

    async def run(
        self,
        args: Sequence[str],
        *,
        stdin: str | None = None,
        output: Literal["json", "jsonl"] = "json",
        timeout_seconds: int | None = None,
    ) -> Any:
        del output, timeout_seconds
        self.calls.append((list(args), stdin))
        if self.error is not None:
            raise self.error
        return self.response


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def client_info(client_id: str = "c1", name: str | None = "ChatGPT") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        client_name=name,
        redirect_uris=[AnyUrl(REDIRECT)],
        token_endpoint_auth_method="none",
    )


@pytest.fixture
def store(paths: Paths) -> McpStore:
    return McpStore(paths)


@pytest.fixture
def provider(store: McpStore) -> ClitgOAuthProvider:
    return ClitgOAuthProvider(store, "demo", ISSUER + "/", RESOURCE)


def pair_client(provider: ClitgOAuthProvider) -> httpx2.AsyncClient:
    app = Starlette(routes=[Route(PAIR_PATH, provider.pair, methods=["GET", "POST"])])
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=ISSUER)


async def start_request(provider: ClitgOAuthProvider, name: str | None = "ChatGPT") -> str:
    await provider.register_client(client_info(name=name))
    client = await provider.get_client("c1")
    assert client is not None
    _, challenge = pkce()
    url = await provider.authorize(
        client,
        AuthorizationParams(
            state="xyz",
            scopes=None,
            code_challenge=challenge,
            redirect_uri=AnyUrl(REDIRECT),
            redirect_uri_provided_explicitly=True,
            resource=None,
        ),
    )
    assert url.startswith(f"{ISSUER}{PAIR_PATH}?request=")
    return parse_qs(urlparse(url).query)["request"][0]


async def test_register_client_errors(provider: ClitgOAuthProvider, monkeypatch: Any) -> None:
    info = client_info().model_copy(update={"client_id": None})
    with pytest.raises(RegistrationError):
        await provider.register_client(info)
    monkeypatch.setattr(mcp_store, "MAX_CLIENTS", 0)
    with pytest.raises(RegistrationError):
        await provider.register_client(client_info())
    assert await provider.get_client("missing") is None


async def test_pair_page_and_deny(provider: ClitgOAuthProvider) -> None:
    request_id = await start_request(provider, name=None)
    async with pair_client(provider) as http:
        missing = await http.get(PAIR_PATH, params={"request": "nope"})
        assert missing.status_code == 400
        assert "expired" in missing.text
        page = await http.get(PAIR_PATH, params={"request": request_id})
        assert page.status_code == 200
        assert "Unnamed MCP client" in page.text
        assert "chatgpt.com" in page.text
        assert page.headers["x-frame-options"] == "DENY"
        denied = await http.post(PAIR_PATH, data={"request": request_id, "action": "deny"})
        assert denied.status_code == 302
        query = parse_qs(urlparse(denied.headers["location"]).query)
        assert query == {"error": ["access_denied"], "state": ["xyz"], "iss": [ISSUER]}
        gone = await http.post(PAIR_PATH, data={"request": request_id, "code": "x"})
        assert gone.status_code == 400


async def test_pair_attempts_and_lockout(
    provider: ClitgOAuthProvider, store: McpStore, monkeypatch: Any
) -> None:
    request_id = await start_request(provider)
    async with pair_client(provider) as http:
        for remaining in (4, 3, 2, 1):
            bad = await http.post(PAIR_PATH, data={"request": request_id, "code": "WRONG"})
            assert bad.status_code == 400
            assert f"{remaining} attempts left" in bad.text
        final = await http.post(PAIR_PATH, data={"request": request_id, "code": "WRONG"})
        assert final.status_code == 403
        monkeypatch.setattr(mcp_store, "PAIRING_FAILURE_LIMIT", 0)
        locked_request = await start_request(provider)
        locked = await http.post(PAIR_PATH, data={"request": locked_request, "code": "x"})
        assert locked.status_code == 429
        assert store.get_auth_request("demo", locked_request) is None


async def test_oauth_grants(provider: ClitgOAuthProvider, store: McpStore) -> None:
    request_id = await start_request(provider)
    code, _ = store.issue_pairing_code("demo")
    async with pair_client(provider) as http:
        approved = await http.post(
            PAIR_PATH, data={"request": request_id, "code": code, "action": "approve"}
        )
    assert approved.status_code == 302
    location = urlparse(approved.headers["location"])
    assert location.netloc == "chatgpt.com"
    auth_code = parse_qs(location.query)["code"][0]
    client = await provider.get_client("c1")
    assert client is not None
    assert await provider.load_authorization_code(client, "missing") is None
    loaded = await provider.load_authorization_code(client, auth_code)
    assert loaded is not None
    assert loaded.scopes == ["clitg"]
    token = await provider.exchange_authorization_code(client, loaded)
    assert token.scope == "clitg"
    with pytest.raises(TokenError):
        await provider.exchange_authorization_code(client, loaded)
    access = await provider.load_access_token(token.access_token)
    assert access is not None
    assert access.resource == RESOURCE
    assert await provider.load_access_token("nope") is None
    assert token.refresh_token is not None
    other = client_info("c2")
    assert await provider.load_refresh_token(other, token.refresh_token) is None
    assert await provider.load_refresh_token(client, "nope") is None
    refresh = await provider.load_refresh_token(client, token.refresh_token)
    assert refresh is not None
    rotated = await provider.exchange_refresh_token(client, refresh, [])
    assert rotated.scope == "clitg"
    assert await provider.load_access_token(token.access_token) is None
    with pytest.raises(TokenError):
        await provider.exchange_refresh_token(client, refresh, ["clitg"])
    current = await provider.load_access_token(rotated.access_token)
    assert current is not None
    await provider.revoke_token(current)
    assert await provider.load_access_token(rotated.access_token) is None
    await provider.revoke_token(current)
    bare = provider._issue(client, [], None)
    assert bare.scope is None


async def test_static_tokens_verifiers(provider: ClitgOAuthProvider, store: McpStore) -> None:
    token, _ = store.create_static_token("demo", "laptop", None)
    access = await provider.load_access_token(token)
    assert access is not None
    assert access.resource == RESOURCE
    verifier = StaticTokenVerifier(store, "demo", RESOURCE)
    verified = await verifier.verify_token(token)
    assert verified is not None
    assert verified.client_id == "static:laptop"
    assert await verifier.verify_token("nope") is None


def test_configure_logging() -> None:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    try:
        configure_logging(False)
        assert isinstance(root.handlers[0], logging.NullHandler)
        configure_logging(True)
        assert isinstance(root.handlers[0], logging.StreamHandler)
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)
        logging.captureWarnings(False)


def test_resolve_issuer() -> None:
    assert resolve_issuer("127.0.0.1", 9, None) == (ISSUER, True)
    assert resolve_issuer("::1", 9, None) == ("http://[::1]:9", True)
    assert resolve_issuer("100.64.0.1", 9, None) == ("http://100.64.0.1:9", False)
    assert resolve_issuer("100.64.0.1", 9, "https://mcp.example.com/") == (
        "https://mcp.example.com",
        True,
    )
    assert resolve_issuer("127.0.0.1", 9, "http://localhost:9") == ("http://localhost:9", True)
    for bad in (
        "http://mcp.example.com",
        "https://mcp.example.com/path",
        "https://",
        "https://x?a",
    ):
        with pytest.raises(ClitgError):
            resolve_issuer("127.0.0.1", 9, bad)


def test_transport_security() -> None:
    settings = transport_security("::1", 9, "https://mcp.example.com")
    assert "[::1]:9" in settings.allowed_hosts
    assert "mcp.example.com" in settings.allowed_hosts
    assert "https://mcp.example.com" in settings.allowed_origins


def test_bind_socket(monkeypatch: Any) -> None:
    sock = bind_socket("127.0.0.1", 0)
    assert sock.getsockname()[1] > 0
    sock.close()
    with pytest.raises(ClitgError) as error:
        bind_socket("203.0.113.1", 0)
    assert error.value.info.code == ErrorCode.CONFLICT
    families: list[int] = []

    class FakeSocket:
        def __init__(self, family: int, kind: int) -> None:
            del kind
            families.append(family)

        def setsockopt(self, *args: Any) -> None:
            del args

        def bind(self, address: Any) -> None:
            del address

        def listen(self, backlog: int) -> None:
            del backlog

        def set_inheritable(self, value: bool) -> None:
            del value

    monkeypatch.setattr(mcp_app.socket, "socket", FakeSocket)
    bind_socket("::1", 0)
    assert families == [mcp_app.socket.AF_INET6]


def test_quiet_server_signals() -> None:
    server = QuietServer(mcp_app.uvicorn.Config(Starlette()))
    before = signal.getsignal(signal.SIGINT)
    with server.capture_signals():
        assert signal.getsignal(signal.SIGINT) == server.handle_exit
    assert signal.getsignal(signal.SIGINT) == before
    result: list[bool] = []

    def worker() -> None:
        with server.capture_signals():
            result.append(True)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert result == [True]


async def test_build_server_and_stdio(monkeypatch: Any) -> None:
    runner = FakeRunner()
    server = build_server(runner)
    assert len(await server.list_tools()) == 15
    calls: list[bool] = []

    async def fake_stdio(self: MCPServer) -> None:
        del self
        calls.append(True)

    monkeypatch.setattr(MCPServer, "run_stdio_async", fake_stdio)
    await serve_stdio(runner)
    assert calls == [True]


async def test_host_resolution(paths: Paths, monkeypatch: Any) -> None:
    async def fake_ip() -> str:
        return "100.64.0.9"

    monkeypatch.setattr(mcp_app, "netbird_ip", fake_ip)

    def serve(**kwargs: Any) -> HttpServe:
        return HttpServe(ServeOptions(profile="demo", **kwargs), FakeRunner(), paths)

    assert await serve().host() == "127.0.0.1"
    assert await serve(host="0.0.0.0").host() == "0.0.0.0"
    assert await serve(host="netbird").host() == "100.64.0.9"
    assert await serve(tunnel="netbird").host() == "100.64.0.9"


async def test_http_app_full_oauth_flow(paths: Paths) -> None:
    serve = HttpServe(ServeOptions(profile="demo"), FakeRunner(), paths)
    app, endpoints = serve.app("127.0.0.1", 9, None)
    assert endpoints == {
        "url": RESOURCE,
        "local_url": RESOURCE,
        "issuer": ISSUER,
        "auth": ["oauth", "bearer"],
    }
    verifier, challenge = pkce()
    transport = httpx2.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(transport=transport, base_url=ISSUER) as http,
    ):
        assert (await http.get("/healthz")).json() == {"ok": True, "service": "clitg-mcp"}
        metadata = (await http.get("/.well-known/oauth-protected-resource/mcp")).json()
        assert metadata["resource"] == RESOURCE
        registered = await http.post(
            "/register",
            json={
                "client_name": "ChatGPT",
                "redirect_uris": [REDIRECT],
                "token_endpoint_auth_method": "none",
            },
        )
        client_id = registered.json()["client_id"]
        authorize = await http.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": REDIRECT,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "s",
                "resource": RESOURCE,
            },
        )
        request_id = parse_qs(urlparse(authorize.headers["location"]).query)["request"][0]
        code, _ = serve.store.issue_pairing_code("demo")
        approved = await http.post(PAIR_PATH, data={"request": request_id, "code": code})
        auth_code = parse_qs(urlparse(approved.headers["location"]).query)["code"][0]
        token = await http.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": auth_code,
                "redirect_uri": REDIRECT,
                "client_id": client_id,
                "code_verifier": verifier,
            },
        )
        access = token.json()["access_token"]
        headers = {
            "accept": "application/json, text/event-stream",
            "authorization": f"Bearer {access}",
            "mcp-protocol-version": "2025-06-18",
        }
        listed = await http.post(
            "/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )
        assert len(listed.json()["result"]["tools"]) == 15
        unauthorized = await http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "x"})
        assert unauthorized.status_code == 401


async def test_http_app_bearer_only(paths: Paths) -> None:
    serve = HttpServe(ServeOptions(profile="demo"), FakeRunner(), paths)
    app, endpoints = serve.app("100.64.0.1", 9, None)
    assert endpoints["auth"] == ["bearer"]
    token, _ = serve.store.create_static_token("demo", "mesh", None)
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://100.64.0.1:9"
        ) as http,
    ):
        assert (await http.post("/register", json={})).status_code == 404
        listed = await http.post(
            "/mcp",
            headers={
                "accept": "application/json, text/event-stream",
                "authorization": f"Bearer {token}",
                "mcp-protocol-version": "2025-06-18",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert listed.status_code == 200


async def test_events_lifecycle(paths: Paths) -> None:
    serve = HttpServe(
        ServeOptions(profile="demo", port=0, pair_delivery="terminal"),
        FakeRunner(),
        paths,
    )
    events = serve.events()
    started = await anext(events)
    assert started["event"] == "server.started"
    pairing = await anext(events)
    assert pairing["delivered_to"] == ["terminal"]
    assert len(pairing["code"]) == 9
    async with httpx2.AsyncClient() as http:
        health = await http.get(started["local_url"].removesuffix("/mcp") + "/healthz")
    assert health.json()["ok"] is True
    assert serve.server is not None
    serve.server.should_exit = True
    assert (await anext(events))["event"] == "server.stopped"
    with pytest.raises(StopAsyncIteration):
        await anext(events)


async def test_events_with_tunnel_and_saved_messages(paths: Paths, monkeypatch: Any) -> None:
    stopped: list[bool] = []

    class FakeTunnel:
        def __init__(self, port: int, prefix: str) -> None:
            assert prefix == "clitg-demo"
            assert port > 0

        async def start(self) -> str:
            return "https://clitg-demo.proxy.example.com"

        async def stop(self) -> None:
            stopped.append(True)

    async def fake_ip() -> str:
        return "127.0.0.1"

    monkeypatch.setattr(mcp_app, "NetbirdTunnel", FakeTunnel)
    monkeypatch.setattr(mcp_app, "netbird_ip", fake_ip)
    runner = FakeRunner()
    serve = HttpServe(ServeOptions(profile="demo", port=0, tunnel="netbird"), runner, paths)
    events = serve.events()
    assert (await anext(events))["public_url"] == "https://clitg-demo.proxy.example.com"
    started = await anext(events)
    assert started["issuer"] == "https://clitg-demo.proxy.example.com"
    pairing = await anext(events)
    assert pairing["delivered_to"] == ["terminal", "saved-messages"]
    assert runner.calls[0][0] == ["messages", "send", "--peer", "me", "--text-stdin"]
    assert pairing["code"] in str(runner.calls[0][1])
    assert serve.server is not None
    serve.server.should_exit = True
    assert (await anext(events))["event"] == "server.stopped"
    with pytest.raises(StopAsyncIteration):
        await anext(events)
    assert stopped == [True]


async def test_events_startup_failure(paths: Paths, monkeypatch: Any) -> None:
    async def fake_serve(self: QuietServer, sockets: Any = None) -> None:
        del self, sockets

    monkeypatch.setattr(QuietServer, "serve", fake_serve)
    serve = HttpServe(ServeOptions(profile="demo", port=0), FakeRunner(), paths)
    with pytest.raises(ClitgError) as error:
        await anext(serve.events())
    assert error.value.info.code == ErrorCode.INTERNAL


@pytest.mark.parametrize(
    ("runner", "expected"),
    [
        (FakeRunner({"ok": False, "error": {"code": "auth_required"}}), "auth_required"),
        (FakeRunner({"ok": False}), "internal"),
        (FakeRunner(error=ClitgError(ErrorCode.NETWORK, "down")), "network"),
    ],
)
async def test_pairing_saved_messages_failures(
    paths: Paths, runner: FakeRunner, expected: str
) -> None:
    serve = HttpServe(
        ServeOptions(profile="demo", pair_delivery="saved-messages"),
        runner,
        paths,
    )
    events = [event async for event in serve.pairing()]
    assert events[0]["saved_messages_error"] == expected
    assert events[0]["delivered_to"] == []
    assert "code" not in events[0]


async def test_pairing_skipped(paths: Paths) -> None:
    serve = HttpServe(ServeOptions(profile="demo", pair_delivery="none"), FakeRunner(), paths)
    assert [event async for event in serve.pairing()] == []
    paired = HttpServe(ServeOptions(profile="demo"), FakeRunner(), paths)
    paired.store.register_client("demo", "c1", {})
    paired.store.issue_token_pair("demo", "c1", ["clitg"], RESOURCE)
    assert [event async for event in paired.pairing()] == []


async def test_events_bearer_only_skips_pairing(paths: Paths) -> None:
    class BearerServe(HttpServe):
        def app(self, host: str, port: int, public_url: str | None) -> tuple[Any, dict[str, Any]]:
            app, endpoints = super().app(host, port, public_url)
            return app, {**endpoints, "auth": ["bearer"]}

    serve = BearerServe(ServeOptions(profile="demo", port=0), FakeRunner(), paths)
    events = serve.events()
    assert (await anext(events))["auth"] == ["bearer"]
    assert serve.server is not None
    serve.server.should_exit = True
    assert (await anext(events))["event"] == "server.stopped"


async def test_events_stop_server_after_failure(paths: Paths) -> None:
    serve = HttpServe(
        ServeOptions(profile="demo", port=0, pair_delivery="saved-messages"),
        FakeRunner(error=RuntimeError("boom")),
        paths,
    )
    events = serve.events()
    assert (await anext(events))["event"] == "server.started"
    with pytest.raises(RuntimeError):
        await anext(events)
    assert serve.server is not None
    assert serve.server.should_exit is True
