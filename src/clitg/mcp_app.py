"""Build and run the clitg MCP server over stdio or Streamable HTTP."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import socket
import threading
from collections.abc import AsyncIterator, Generator
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

import uvicorn
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from clitg import __version__
from clitg.errors import ClitgError
from clitg.mcp_auth import PAIR_PATH, SCOPE, ClitgOAuthProvider, StaticTokenVerifier
from clitg.mcp_store import McpStore, pairing_delivery_targets, pairing_message
from clitg.mcp_tools import INSTRUCTIONS, CliRunner, register_tools
from clitg.models import ErrorCode
from clitg.storage import Paths
from clitg.tunnels import NetbirdTunnel, TunnelName, netbird_ip

PairDelivery = Literal["both", "terminal", "saved-messages", "none"]
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
DEFAULT_PORT = 8765


@dataclass(frozen=True)
class ServeOptions:
    """Validated options for one HTTP server run."""

    profile: str
    host: str | None = None
    port: int = DEFAULT_PORT
    public_url: str | None = None
    tunnel: TunnelName = "none"
    pair_delivery: PairDelivery = "both"
    verbose: bool = False


def configure_logging(verbose: bool) -> None:
    """Keep stderr empty unless redacted diagnostics were requested."""

    handler: logging.Handler = logging.StreamHandler() if verbose else logging.NullHandler()
    logging.basicConfig(level=logging.WARNING, handlers=[handler], force=True)
    logging.captureWarnings(True)


def build_server(
    runner: CliRunner,
    *,
    auth: AuthSettings | None = None,
    provider: ClitgOAuthProvider | None = None,
    verifier: TokenVerifier | None = None,
) -> MCPServer:
    """Create the MCP server with every clitg tool registered."""

    server = MCPServer(
        name="clitg",
        title="Telegram (clitg)",
        instructions=INSTRUCTIONS,
        version=__version__,
        website_url="https://github.com/leynier/telegram-cli",
        auth=auth,
        auth_server_provider=provider,
        token_verifier=verifier,
        log_level="WARNING",
    )
    register_tools(server, runner)
    return server


async def serve_stdio(runner: CliRunner) -> None:
    """Serve MCP over stdin and stdout for locally spawned clients."""

    await build_server(runner).run_stdio_async()


def _url_host(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def resolve_issuer(host: str, port: int, public_url: str | None) -> tuple[str, bool]:
    """Return the OAuth issuer and whether OAuth can be offered on it."""

    if public_url is None:
        return f"http://{_url_host(host)}:{port}", host in LOOPBACK_HOSTS
    parsed = urlparse(public_url.rstrip("/"))
    if parsed.scheme != "https" and parsed.hostname not in LOOPBACK_HOSTS:
        raise ClitgError(ErrorCode.INVALID_INPUT, "--public-url must use HTTPS")
    if not parsed.hostname or parsed.path or parsed.query or parsed.fragment:
        raise ClitgError(
            ErrorCode.INVALID_INPUT,
            "--public-url must be an origin such as https://mcp.example.com",
        )
    return f"{parsed.scheme}://{parsed.netloc}", True


def transport_security(host: str, port: int, issuer: str) -> TransportSecuritySettings:
    """Allow only the bound address and the public origin in Host and Origin headers."""

    public = urlparse(issuer)
    hosts = {"127.0.0.1:*", "localhost:*", "[::1]:*", f"{_url_host(host)}:{port}", public.netloc}
    origins = {
        "http://127.0.0.1:*",
        "http://localhost:*",
        "http://[::1]:*",
        f"{public.scheme}://{public.netloc}",
    }
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=sorted(hosts),
        allowed_origins=sorted(origins),
    )


def bind_socket(host: str, port: int) -> socket.socket:
    """Bind the listening socket up front so failures use the structured error contract."""

    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError as exc:
        sock.close()
        raise ClitgError(
            ErrorCode.CONFLICT,
            "The MCP server address is unavailable",
            details={"host": host, "port": port},
        ) from exc
    sock.listen(128)
    sock.set_inheritable(True)
    return sock


class QuietServer(uvicorn.Server):
    """Uvicorn server that stops on SIGINT or SIGTERM without re-raising the signal."""

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None]:
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        handled = [signal.SIGINT, signal.SIGTERM]
        original = {sig: signal.signal(sig, self.handle_exit) for sig in handled}
        try:
            yield
        finally:
            for sig, handler in original.items():
                signal.signal(sig, handler)


class HttpServe:
    """Run the Streamable HTTP server and report lifecycle events."""

    def __init__(self, options: ServeOptions, runner: CliRunner, paths: Paths) -> None:
        self.options = options
        self.runner = runner
        self.store = McpStore(paths)
        self.server: QuietServer | None = None

    async def host(self) -> str:
        """Resolve the bind address, defaulting to NetBird's address for NetBird tunnels."""

        requested = self.options.host
        if requested == "netbird" or (requested is None and self.options.tunnel == "netbird"):
            return await netbird_ip()
        return requested or "127.0.0.1"

    def app(self, host: str, port: int, public_url: str | None) -> tuple[Any, dict[str, Any]]:
        """Build the ASGI application and describe its endpoints."""

        issuer, oauth = resolve_issuer(host, port, public_url)
        resource = f"{issuer}/mcp"
        provider: ClitgOAuthProvider | None = None
        verifier: TokenVerifier | None = None
        if oauth:
            provider = ClitgOAuthProvider(self.store, self.options.profile, issuer, resource)
            auth = AuthSettings(
                issuer_url=issuer,
                resource_server_url=resource,
                client_registration_options=ClientRegistrationOptions(
                    enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
                ),
                revocation_options=RevocationOptions(enabled=True),
                validate_token_resource=True,
            )
        else:
            verifier = StaticTokenVerifier(self.store, self.options.profile, resource)
            auth = AuthSettings(
                issuer_url=issuer, resource_server_url=resource, validate_token_resource=True
            )
        server = build_server(self.runner, auth=auth, provider=provider, verifier=verifier)
        if provider is not None:
            server.custom_route(PAIR_PATH, methods=["GET", "POST"], include_in_schema=False)(
                provider.pair
            )

        async def health(request: Request) -> Response:
            del request
            return JSONResponse({"ok": True, "service": "clitg-mcp"})

        server.custom_route("/healthz", methods=["GET"], include_in_schema=False)(health)
        app = server.streamable_http_app(
            json_response=True,
            stateless_http=True,
            transport_security=transport_security(host, port, issuer),
            host=host,
        )
        local = f"http://{_url_host(host)}:{port}/mcp"
        return app, {
            "url": resource,
            "local_url": local,
            "issuer": issuer,
            "auth": ["oauth", "bearer"] if oauth else ["bearer"],
        }

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        """Start the server, yield lifecycle events, and stop cleanly on a signal."""

        host = await self.host()
        sock = bind_socket(host, self.options.port)
        port = int(sock.getsockname()[1])
        tunnel: NetbirdTunnel | None = None
        task: asyncio.Task[None] | None = None
        try:
            public_url = self.options.public_url
            if self.options.tunnel == "netbird":
                tunnel = NetbirdTunnel(port, f"clitg-{self.options.profile}")
                public_url = await tunnel.start()
                yield {"event": "tunnel.started", "provider": "netbird", "public_url": public_url}
            app, endpoints = self.app(host, port, public_url)
            server = QuietServer(
                uvicorn.Config(
                    app,
                    log_config=None,
                    access_log=False,
                    log_level="warning" if self.options.verbose else "critical",
                    server_header=False,
                    lifespan="on",
                )
            )
            self.server = server
            task = asyncio.create_task(server.serve(sockets=[sock]))
            while not server.started and not task.done():
                await asyncio.sleep(0.05)
            if task.done():
                await task
                raise ClitgError(ErrorCode.INTERNAL, "The MCP server stopped during startup")
            yield {"event": "server.started", "profile": self.options.profile, **endpoints}
            if "oauth" in endpoints["auth"]:
                async for event in self.pairing():
                    yield event
            await task
            yield {"event": "server.stopped", "profile": self.options.profile}
        finally:
            if task is not None and not task.done() and self.server is not None:
                self.server.should_exit = True
                await task
            if tunnel is not None:
                await tunnel.stop()
            sock.close()

    async def pairing(self) -> AsyncIterator[dict[str, Any]]:
        """Issue a startup pairing code when no OAuth client has been paired yet."""

        delivery = self.options.pair_delivery
        if delivery == "none" or self.store.has_paired_clients(self.options.profile):
            return
        terminal, saved = pairing_delivery_targets(delivery)
        code, expires_at = self.store.issue_pairing_code(self.options.profile)
        event: dict[str, Any] = {
            "event": "pairing.code",
            "expires_at": expires_at,
            "delivered_to": [],
        }
        if terminal:
            event["code"] = code
            event["delivered_to"].append("terminal")
        if saved:
            result: dict[str, Any]
            try:
                result = await self.runner.run(
                    ["messages", "send", "--peer", "me", "--text-stdin"],
                    stdin=pairing_message(self.options.profile, code, expires_at),
                )
            except ClitgError as exc:
                result = {"ok": False, "error": {"code": exc.info.code}}
            if result.get("ok"):
                event["delivered_to"].append("saved-messages")
            else:
                event["saved_messages_error"] = (result.get("error") or {}).get("code", "internal")
        yield event
