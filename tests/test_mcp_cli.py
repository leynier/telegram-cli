from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from typer.testing import CliRunner

import clitg.cli as cli
from clitg.errors import ClitgError
from clitg.mcp_app import ServeOptions
from clitg.models import CommandResult, ErrorCode, Profile
from clitg.service import ClitgService
from clitg.storage import Paths

runner = CliRunner()


class FakeService:
    instances: ClassVar[list[FakeService]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.profiles = SimpleNamespace(resolve=lambda requested: SimpleNamespace(name="demo"))
        self.paths = Paths(Path("config"), Path("data"))
        self.instances.append(self)

    def __getattr__(self, name: str) -> Any:
        def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, args, kwargs))
            return CommandResult(data={"method": name}, items=[{"method": name}])

        return call


class FakeModule:
    def __init__(self) -> None:
        self.logging: list[bool] = []
        self.stdio: list[Any] = []
        self.options: list[ServeOptions] = []
        module = self

        class Runner:
            def __init__(self, profile: str, **kwargs: Any) -> None:
                self.profile = profile
                self.kwargs = kwargs

        class Serve:
            def __init__(self, options: ServeOptions, runner: Any, paths: Paths) -> None:
                module.options.append(options)
                self.runner = runner

            async def events(self) -> Any:
                yield {"event": "server.started", "lock_wait": self.runner.kwargs}
                yield {"event": "server.stopped"}

        self.CliRunner = Runner
        self.HttpServe = Serve
        self.ServeOptions = ServeOptions

    def configure_logging(self, verbose: bool) -> None:
        self.logging.append(verbose)

    async def serve_stdio(self, runner: Any) -> None:
        self.stdio.append(runner)


@pytest.fixture
def fake_module(monkeypatch: pytest.MonkeyPatch) -> FakeModule:
    FakeService.instances.clear()
    monkeypatch.setattr(cli, "SERVICE_FACTORY", FakeService)
    module = FakeModule()
    monkeypatch.setattr(cli, "_mcp_module", lambda: module)
    return module


def lines(output: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in output.splitlines() if line.strip()]


@pytest.mark.parametrize(
    ("arguments", "method", "kwargs"),
    [
        (["mcp", "pair", "--deliver", "terminal"], "mcp_pair", {"delivery": "terminal"}),
        (["mcp", "clients", "list"], "mcp_clients", {}),
        (
            ["mcp", "clients", "revoke", "--client-id", "c1", "--confirm", "mcp.clients.revoke"],
            "mcp_revoke_client",
            {"confirmation": "mcp.clients.revoke"},
        ),
        (
            ["mcp", "tokens", "create", "--name", "laptop", "--expires-days", "3", "--dry-run"],
            "mcp_create_token",
            {"expires_days": 3, "dry_run": True},
        ),
        (["mcp", "tokens", "list"], "mcp_tokens", {}),
        (
            ["mcp", "tokens", "revoke", "--name", "laptop", "--dry-run"],
            "mcp_revoke_token",
            {"dry_run": True},
        ),
    ],
)
def test_mcp_admin_commands(
    fake_module: FakeModule, arguments: list[str], method: str, kwargs: dict[str, Any]
) -> None:
    result = runner.invoke(cli.app, arguments)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"] == {"method": method}
    name, _, called = FakeService.instances[0].calls[0]
    assert name == method
    assert kwargs.items() <= called.items()


def test_serve_http_streams_events(fake_module: FakeModule) -> None:
    result = runner.invoke(
        cli.app,
        [
            "--output",
            "jsonl",
            "--verbose",
            "--profile",
            "demo",
            "mcp",
            "serve",
            "--transport",
            "http",
            "--host",
            "netbird",
            "--port",
            "0",
            "--public-url",
            "https://mcp.example.com",
            "--lock-wait",
            "2",
            "--pair-delivery",
            "terminal",
        ],
    )
    assert result.exit_code == 0, result.output
    records = lines(result.output)
    assert [record["record_type"] for record in records] == ["item", "item", "summary"]
    assert records[0]["data"]["lock_wait"] == {"timeout_seconds": 30, "lock_wait_seconds": 2.0}
    assert fake_module.options == [
        ServeOptions(
            profile="demo",
            host="netbird",
            port=0,
            public_url="https://mcp.example.com",
            tunnel="none",
            pair_delivery="terminal",
            verbose=True,
        )
    ]
    assert fake_module.logging == [True]


@pytest.mark.parametrize(
    "arguments",
    [
        ["--transport", "websocket"],
        ["--transport", "http", "--tunnel", "ngrok"],
        ["--transport", "http", "--pair-delivery", "email"],
        ["--transport", "http", "--tunnel", "netbird", "--public-url", "https://x.example.com"],
    ],
)
def test_serve_http_validation(fake_module: FakeModule, arguments: list[str]) -> None:
    result = runner.invoke(cli.app, ["--output", "jsonl", "mcp", "serve", *arguments])
    assert result.exit_code == 2
    assert lines(result.output)[-1]["error"]["code"] == "invalid_input"
    assert fake_module.options == []


def test_serve_http_requires_jsonl(fake_module: FakeModule) -> None:
    result = runner.invoke(cli.app, ["mcp", "serve", "--transport", "http"])
    assert result.exit_code == 2
    assert "jsonl" in json.loads(result.output)["error"]["message"]


def test_serve_stdio(fake_module: FakeModule) -> None:
    result = runner.invoke(cli.app, ["--profile", "demo", "mcp", "serve"])
    assert result.exit_code == 0
    assert result.output == ""
    assert fake_module.stdio[0].profile == "demo"
    assert FakeService.instances[0].calls[-1][0] == "record_audit"


def test_serve_stdio_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "SERVICE_FACTORY", FakeService)

    def missing() -> Any:
        raise ClitgError(ErrorCode.INVALID_INPUT, "The MCP server requires the mcp extra")

    monkeypatch.setattr(cli, "_mcp_module", missing)
    result = runner.invoke(cli.app, ["mcp", "serve"])
    assert result.exit_code == 2
    assert json.loads(result.output)["error"]["message"] == "The MCP server requires the mcp extra"


def test_mcp_module_import(monkeypatch: pytest.MonkeyPatch) -> None:
    import clitg.mcp_app

    assert cli._mcp_module() is clitg.mcp_app
    monkeypatch.setitem(sys.modules, "clitg.mcp_app", None)
    with pytest.raises(ClitgError) as error:
        cli._mcp_module()
    assert error.value.info.details == {"install": "uv tool install 'clitg[mcp]'"}


def test_real_service_mcp_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = Paths(tmp_path / "config", tmp_path / "data")
    service = ClitgService(paths)
    service.profiles.create(Profile(name="demo", api_id=1, api_hash="secret"))
    monkeypatch.setattr(cli, "SERVICE_FACTORY", lambda **kwargs: service)
    created = runner.invoke(cli.app, ["mcp", "tokens", "create", "--name", "laptop"])
    assert created.exit_code == 0, created.output
    assert json.loads(created.output)["data"]["token"].startswith("clitg_pat_")
    listed = runner.invoke(cli.app, ["--output", "jsonl", "mcp", "tokens", "list"])
    assert lines(listed.output)[0]["data"]["name"] == "laptop"
    audit = service.state.list_audit()
    assert audit[0]["command"] == "mcp.tokens.list"
    assert os.environ.get("CLITG_PROFILE") is None
