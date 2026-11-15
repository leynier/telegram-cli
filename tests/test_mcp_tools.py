from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal, cast

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, ToolAnnotations

import clitg.mcp_tools as mcp_tools
from clitg.errors import ClitgError
from clitg.mcp_tools import CliRunner, flags, register_tools, tool_result, validate_run_args
from clitg.models import ErrorCode

FAKE_CLI = """
import json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
mode = os.environ.get("FAKE_CLI_MODE", "ok")
state = Path(os.environ["FAKE_CLI_STATE"])
count = int(state.read_text()) if state.exists() else 0
state.write_text(str(count + 1))
record = {"args": args, "stdin": sys.stdin.read()}
busy = {"ok": False, "error": {"code": "conflict",
        "message": "The profile session is already in use"}}
if (mode == "busy" and count < 2) or mode == "always-busy":
    print(json.dumps(busy))
elif mode == "jsonl":
    print(json.dumps({"record_type": "item", "data": record}))
    print()
    print(json.dumps({"record_type": "summary", "data": {"count": 1}}))
elif mode == "invalid":
    print("not json")
elif mode == "slow":
    time.sleep(5)
else:
    print(json.dumps({"ok": True, "data": record}))
"""


@pytest.fixture
def fake_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    script = tmp_path / "fake_cli.py"
    script.write_text(FAKE_CLI, encoding="utf-8")
    monkeypatch.setenv("FAKE_CLI_STATE", str(tmp_path / "count"))
    runner = CliRunner("demo", timeout_seconds=7, command=[sys.executable, str(script)])
    runner.retry_delay_seconds = 0.01
    return runner


async def test_runner_passes_profile_output_and_stdin(fake_cli: CliRunner) -> None:
    result = await fake_cli.run(["messages", "send", "--text-stdin"], stdin="héllo")
    assert result["data"]["args"] == [
        "--profile",
        "demo",
        "--output",
        "json",
        "--timeout-seconds",
        "7",
        "messages",
        "send",
        "--text-stdin",
    ]
    assert result["data"]["stdin"] == "héllo"
    plain = await fake_cli.run(["dialogs", "list"], timeout_seconds=3)
    assert plain["data"]["stdin"] == ""
    assert "3" in plain["data"]["args"]


async def test_runner_jsonl(fake_cli: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_CLI_MODE", "jsonl")
    records = await fake_cli.run(["updates", "watch"], output="jsonl")
    assert [record["record_type"] for record in records] == ["item", "summary"]
    assert "jsonl" in records[0]["data"]["args"]


async def test_runner_retries_busy_session(
    fake_cli: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FAKE_CLI_MODE", "busy")
    result = await fake_cli.run(["dialogs", "list"])
    assert result["ok"] is True
    assert (tmp_path / "count").read_text() == "3"


async def test_runner_gives_up_after_lock_wait(
    fake_cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CLI_MODE", "always-busy")
    fake_cli.lock_wait_seconds = 0
    result = await fake_cli.run(["dialogs", "list"])
    assert result["error"]["code"] == "conflict"


async def test_runner_invalid_output(fake_cli: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_CLI_MODE", "invalid")
    with pytest.raises(ClitgError) as error:
        await fake_cli.run(["dialogs", "list"])
    assert error.value.info.code == ErrorCode.INTERNAL


async def test_runner_timeout(fake_cli: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_CLI_MODE", "slow")
    real = asyncio.wait_for

    async def short(awaitable: Any, timeout: float | None) -> Any:
        del timeout
        return await real(awaitable, 0.5)

    monkeypatch.setattr(mcp_tools.asyncio, "wait_for", short)
    with pytest.raises(ClitgError) as error:
        await fake_cli.run(["dialogs", "list"])
    assert error.value.info.code == ErrorCode.NETWORK


def test_default_command_and_busy_detection() -> None:
    runner = CliRunner("demo")
    assert runner.command == [sys.executable, "-m", "clitg"]
    busy = {"ok": False, "error": {"code": "conflict", "message": mcp_tools.SESSION_BUSY_MESSAGE}}
    assert CliRunner._session_busy(busy) is True
    assert CliRunner._session_busy([{"record_type": "item"}, busy]) is True
    assert CliRunner._session_busy([]) is False
    assert CliRunner._session_busy({"ok": True}) is False
    assert CliRunner._session_busy({"ok": False}) is False
    assert CliRunner._session_busy("text") is False


def test_flags_and_results() -> None:
    assert flags(
        peer="me", from_peer="@a", media_only=True, dry_run=False, cursor=None, limit=5
    ) == [
        "--peer",
        "me",
        "--from",
        "@a",
        "--media-only",
        "--limit",
        "5",
    ]
    assert tool_result({"ok": True, "data": 1}) == {"ok": True, "data": 1}
    assert tool_result([{"record_type": "summary"}]) == {"records": [{"record_type": "summary"}]}
    assert tool_result([]) == {"records": []}
    with pytest.raises(ToolError, match="not_found"):
        tool_result({"ok": False, "error": {"code": "not_found"}})
    with pytest.raises(ToolError, match="network"):
        tool_result([{"record_type": "error", "error": {"code": "network"}}])
    validate_run_args(["dialogs", "list"])
    for bad in ([], ["--profile", "x"], ["mcp", "tokens", "list"]):
        with pytest.raises(ToolError):
            validate_run_args(bad)


class FakeRunner(CliRunner):
    def __init__(self) -> None:
        super().__init__("demo")
        self.calls: list[dict[str, Any]] = []
        self.response: Any = {"ok": True, "data": {"value": 1}}
        self.error: ClitgError | None = None

    async def run(
        self,
        args: Sequence[str],
        *,
        stdin: str | None = None,
        output: Literal["json", "jsonl"] = "json",
        timeout_seconds: int | None = None,
    ) -> Any:
        self.calls.append(
            {"args": list(args), "stdin": stdin, "output": output, "timeout": timeout_seconds}
        )
        if self.error is not None:
            raise self.error
        return self.response


@pytest.fixture
def tools() -> tuple[MCPServer, FakeRunner]:
    server = MCPServer("test")
    fake = FakeRunner()
    register_tools(server, fake)
    return server, fake


TOOL_CASES = [
    ("dialogs_list", {}, ["dialogs", "list", "--limit", "50"], None),
    (
        "dialogs_search",
        {"query": "x"},
        ["dialogs", "search", "--query", "x", "--limit", "50"],
        None,
    ),
    ("contacts_resolve", {"peer": "@a"}, ["contacts", "resolve", "--peer", "@a"], None),
    (
        "inbox_list",
        {"from_peer": "@a", "media_only": True, "folder_id": 1},
        [
            *["inbox", "list", "--view", "messages", "--from", "@a", "--folder-id", "1"],
            *["--media-only", "--limit", "50"],
        ],
        None,
    ),
    (
        "messages_list",
        {"peer": "me", "topic_id": 3},
        ["messages", "list", "--peer", "me", "--limit", "50", "--topic-id", "3"],
        None,
    ),
    (
        "messages_get",
        {"peer": "me", "message_id": 4},
        ["messages", "get", "--peer", "me", "--message-id", "4"],
        None,
    ),
    (
        "messages_search",
        {"query": "q"},
        ["messages", "search", "--query", "q", "--limit", "50"],
        None,
    ),
    (
        "messages_context",
        {"peer": "me", "message_id": 4},
        [
            *["messages", "context", "--peer", "me", "--message-id", "4"],
            *["--before", "10", "--after", "10"],
        ],
        None,
    ),
    (
        "messages_replies",
        {"peer": "me", "message_id": 4},
        ["messages", "replies", "--peer", "me", "--message-id", "4", "--limit", "50"],
        None,
    ),
    (
        "messages_send",
        {"peer": "me", "text": "hi", "dry_run": True},
        ["messages", "send", "--text-stdin", "--peer", "me", "--parse-mode", "plain", "--dry-run"],
        "hi",
    ),
    (
        "messages_reply",
        {"peer": "me", "message_id": 2, "text": "ok", "idempotency_key": "k"},
        [
            *["messages", "reply", "--text-stdin", "--peer", "me", "--message-id", "2"],
            *["--parse-mode", "plain", "--idempotency-key", "k"],
        ],
        "ok",
    ),
    ("messages_read", {"peer": "me"}, ["messages", "read", "--peer", "me"], None),
    ("clitg_commands", {}, ["commands", "list"], None),
    ("clitg_command", {"command": "a.b"}, ["commands", "get", "--command", "a.b"], None),
]


@pytest.mark.parametrize(("name", "arguments", "expected", "stdin"), TOOL_CASES)
async def test_curated_tools(
    tools: tuple[MCPServer, FakeRunner],
    name: str,
    arguments: dict[str, Any],
    expected: list[str],
    stdin: str | None,
) -> None:
    server, fake = tools
    result = await server.call_tool(name, arguments)
    assert cast(CallToolResult, result).structured_content == {"ok": True, "data": {"value": 1}}
    assert fake.calls[0]["args"] == expected
    assert fake.calls[0]["stdin"] == stdin


async def test_tool_annotations(tools: tuple[MCPServer, FakeRunner]) -> None:
    server, _ = tools
    listed = {
        tool.name: cast(ToolAnnotations, tool.annotations) for tool in await server.list_tools()
    }
    assert len(listed) == 15
    assert listed["messages_list"].read_only_hint is True
    assert listed["messages_send"].destructive_hint is False
    assert listed["clitg_run"].destructive_hint is True


async def test_tool_errors(tools: tuple[MCPServer, FakeRunner]) -> None:
    server, fake = tools
    fake.response = {"ok": False, "error": {"code": "auth_required"}}
    with pytest.raises(ToolError, match="auth_required"):
        await server.call_tool("dialogs_list", {})
    fake.error = ClitgError(ErrorCode.NETWORK, "timed out")
    with pytest.raises(ToolError, match="network"):
        await server.call_tool("dialogs_list", {})
    with pytest.raises(ToolError, match="network"):
        await server.call_tool("clitg_run", {"args": ["dialogs", "list"]})


async def test_clitg_run(tools: tuple[MCPServer, FakeRunner]) -> None:
    server, fake = tools
    fake.response = [{"record_type": "summary"}]
    result = await server.call_tool(
        "clitg_run",
        {"args": ["updates", "watch"], "output": "jsonl", "stdin": "x", "timeout_seconds": 9000},
    )
    assert cast(CallToolResult, result).structured_content == {
        "records": [{"record_type": "summary"}]
    }
    assert fake.calls[0] == {
        "args": ["updates", "watch"],
        "stdin": "x",
        "output": "jsonl",
        "timeout": 600,
    }
    with pytest.raises(ToolError, match="mcp command group"):
        await server.call_tool("clitg_run", {"args": ["mcp", "serve"]})
    payload = json.loads(json.dumps(fake.calls))
    assert len(payload) == 1
