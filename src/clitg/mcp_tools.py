"""MCP tools that execute clitg commands with the unchanged CLI safety contract."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Sequence
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from clitg.errors import ClitgError
from clitg.models import ErrorCode
from clitg.serialization import json_dumps

SESSION_BUSY_MESSAGE = "The profile session is already in use"
BLOCKED_GROUPS = frozenset({"mcp"})
READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
ANY = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)
INSTRUCTIONS = (
    "Operate one real Telegram user account through clitg. Results use the clitg JSON envelope. "
    "Listing, searching, and reading context never marks messages read. Preview every mutation "
    "with dry_run=true first. Destructive commands need the exact --confirm value and critical "
    "commands also need the --confirmation-token returned by their dry run. Ambiguous peers "
    "return candidates; ask the user instead of guessing. Use clitg_command to inspect a command "
    "before calling clitg_run."
)


class CliRunner:
    """Run clitg as an isolated subprocess, serializing access to the profile session."""

    def __init__(
        self,
        profile: str,
        *,
        timeout_seconds: int = 30,
        lock_wait_seconds: float = 15,
        command: Sequence[str] | None = None,
    ) -> None:
        self.profile = profile
        self.timeout_seconds = timeout_seconds
        self.lock_wait_seconds = lock_wait_seconds
        self.command = list(command or (sys.executable, "-m", "clitg"))
        self.retry_delay_seconds = 0.5
        self._lock = asyncio.Lock()

    async def run(
        self,
        args: Sequence[str],
        *,
        stdin: str | None = None,
        output: Literal["json", "jsonl"] = "json",
        timeout_seconds: int | None = None,
    ) -> Any:
        """Run one command, waiting a bounded time while another process holds the session."""

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.lock_wait_seconds
        async with self._lock:
            while True:
                result = await self._invoke(args, stdin, output, timeout_seconds)
                if not self._session_busy(result) or loop.time() >= deadline:
                    return result
                await asyncio.sleep(self.retry_delay_seconds)

    async def _invoke(
        self,
        args: Sequence[str],
        stdin: str | None,
        output: str,
        timeout_seconds: int | None,
    ) -> Any:
        telegram_timeout = timeout_seconds or self.timeout_seconds
        process = await asyncio.create_subprocess_exec(
            *self.command,
            "--profile",
            self.profile,
            "--output",
            output,
            "--timeout-seconds",
            str(telegram_timeout),
            *args,
            stdin=asyncio.subprocess.DEVNULL if stdin is None else asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        payload = None if stdin is None else stdin.encode("utf-8")
        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(payload), telegram_timeout * 4 + 30
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise ClitgError(ErrorCode.NETWORK, "The clitg command timed out") from exc
        text = stdout.decode("utf-8", errors="replace")
        try:
            if output == "json":
                return json.loads(text)
            return [json.loads(line) for line in text.splitlines() if line.strip()]
        except json.JSONDecodeError as exc:
            raise ClitgError(
                ErrorCode.INTERNAL, "The clitg command returned invalid output"
            ) from exc

    @staticmethod
    def _session_busy(result: Any) -> bool:
        record = result[-1] if isinstance(result, list) and result else result
        if not isinstance(record, dict) or record.get("ok") is True:
            return False
        error = record.get("error") or {}
        return (
            error.get("code") == ErrorCode.CONFLICT and error.get("message") == SESSION_BUSY_MESSAGE
        )


def flags(**options: Any) -> list[str]:
    """Convert keyword options into explicit CLI flags."""

    args: list[str] = []
    for name, value in options.items():
        if value is None or value is False:
            continue
        flag = "--from" if name == "from_peer" else "--" + name.replace("_", "-")
        args.append(flag)
        if value is not True:
            args.append(str(value))
    return args


def tool_result(value: Any) -> dict[str, Any]:
    """Return a successful envelope or raise a tool error carrying the failure envelope."""

    if isinstance(value, list):
        if value and value[-1].get("record_type") == "error":
            raise ToolError(json_dumps(value[-1]))
        return {"records": value}
    if not value.get("ok"):
        raise ToolError(json_dumps(value))
    return value


def validate_run_args(args: list[str]) -> None:
    """Reject arguments that would escape the server's profile or manage the server."""

    if not args:
        raise ToolError("args must contain a command group and command")
    if args[0].startswith("-"):
        raise ToolError("Global options are fixed by the server; start args with a command group")
    if args[0] in BLOCKED_GROUPS:
        raise ToolError("The mcp command group is not available through MCP")


def register_tools(server: MCPServer, runner: CliRunner) -> None:
    """Register curated tools and the generic command runner."""

    async def call(args: list[str], stdin: str | None = None) -> dict[str, Any]:
        try:
            return tool_result(await runner.run(args, stdin=stdin))
        except ClitgError as exc:
            raise ToolError(json_dumps(exc.info)) from exc

    async def dialogs_list(limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
        """List chats, groups, and channels, newest first."""

        return await call(["dialogs", "list", *flags(limit=limit, cursor=cursor)])

    async def dialogs_search(
        query: str, limit: int = 50, cursor: str | None = None
    ) -> dict[str, Any]:
        """Search chats, groups, and channels by title or username."""

        return await call(["dialogs", "search", *flags(query=query, limit=limit, cursor=cursor)])

    async def contacts_resolve(peer: str) -> dict[str, Any]:
        """Resolve a username, phone, ID, or name; ambiguous input returns candidates."""

        return await call(["contacts", "resolve", *flags(peer=peer)])

    async def inbox_list(
        view: Literal["messages", "dialogs"] = "messages",
        peer: str | None = None,
        from_peer: str | None = None,
        folder_id: int | None = None,
        after: str | None = None,
        before: str | None = None,
        media_only: bool = False,
        include_archived: bool = False,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List unread messages or dialog summaries without marking anything read.

        after and before use RFC 3339 with a UTC offset.
        """

        return await call(
            [
                "inbox",
                "list",
                *flags(
                    view=view,
                    peer=peer,
                    from_peer=from_peer,
                    folder_id=folder_id,
                    after=after,
                    before=before,
                    media_only=media_only,
                    include_archived=include_archived,
                    limit=limit,
                    cursor=cursor,
                ),
            ]
        )

    async def messages_list(
        peer: str,
        limit: int = 50,
        cursor: str | None = None,
        topic_id: int | None = None,
        from_peer: str | None = None,
        after: str | None = None,
        before: str | None = None,
        media_only: bool = False,
    ) -> dict[str, Any]:
        """List a conversation's messages, newest first, without marking them read.

        after and before use RFC 3339 with a UTC offset.
        """

        return await call(
            [
                "messages",
                "list",
                *flags(
                    peer=peer,
                    limit=limit,
                    cursor=cursor,
                    topic_id=topic_id,
                    from_peer=from_peer,
                    after=after,
                    before=before,
                    media_only=media_only,
                ),
            ]
        )

    async def messages_get(peer: str, message_id: int) -> dict[str, Any]:
        """Get one message by ID."""

        return await call(["messages", "get", *flags(peer=peer, message_id=message_id)])

    async def messages_search(
        query: str,
        peer: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
        from_peer: str | None = None,
        after: str | None = None,
        before: str | None = None,
        media_only: bool = False,
    ) -> dict[str, Any]:
        """Search one conversation, or the whole account when peer is omitted.

        after and before use RFC 3339 with a UTC offset.
        """

        return await call(
            [
                "messages",
                "search",
                *flags(
                    query=query,
                    peer=peer,
                    limit=limit,
                    cursor=cursor,
                    from_peer=from_peer,
                    after=after,
                    before=before,
                    media_only=media_only,
                ),
            ]
        )

    async def messages_context(
        peer: str, message_id: int, before: int = 10, after: int = 10
    ) -> dict[str, Any]:
        """Get messages surrounding one message."""

        return await call(
            [
                "messages",
                "context",
                *flags(peer=peer, message_id=message_id, before=before, after=after),
            ]
        )

    async def messages_replies(
        peer: str, message_id: int, limit: int = 50, cursor: str | None = None
    ) -> dict[str, Any]:
        """List replies to one message."""

        return await call(
            [
                "messages",
                "replies",
                *flags(peer=peer, message_id=message_id, limit=limit, cursor=cursor),
            ]
        )

    async def messages_send(
        peer: str,
        text: str,
        parse_mode: Literal["plain", "markdown", "html"] = "plain",
        topic_id: int | None = None,
        schedule_at: str | None = None,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Send a text message. Preview with dry_run=true before sending.

        schedule_at uses RFC 3339 with a UTC offset.
        """

        return await call(
            [
                "messages",
                "send",
                "--text-stdin",
                *flags(
                    peer=peer,
                    parse_mode=parse_mode,
                    topic_id=topic_id,
                    schedule_at=schedule_at,
                    idempotency_key=idempotency_key,
                    dry_run=dry_run,
                ),
            ],
            stdin=text,
        )

    async def messages_reply(
        peer: str,
        message_id: int,
        text: str,
        parse_mode: Literal["plain", "markdown", "html"] = "plain",
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Reply to one message with text. Preview with dry_run=true before sending."""

        return await call(
            [
                "messages",
                "reply",
                "--text-stdin",
                *flags(
                    peer=peer,
                    message_id=message_id,
                    parse_mode=parse_mode,
                    idempotency_key=idempotency_key,
                    dry_run=dry_run,
                ),
            ],
            stdin=text,
        )

    async def messages_read(
        peer: str, max_id: int | None = None, dry_run: bool = False
    ) -> dict[str, Any]:
        """Mark a conversation read up to an optional message ID."""

        return await call(["messages", "read", *flags(peer=peer, max_id=max_id, dry_run=dry_run)])

    async def clitg_commands() -> dict[str, Any]:
        """List registered operations with their risk and Telegram method."""

        return await call(["commands", "list"])

    async def clitg_command(command: str) -> dict[str, Any]:
        """Describe one registered operation, such as stories.publish, before clitg_run."""

        return await call(["commands", "get", *flags(command=command)])

    async def clitg_run(
        args: list[str],
        stdin: str | None = None,
        output: Literal["json", "jsonl"] = "json",
        timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        """Run any clitg command as CLI arguments after the global options.

        Example: ["chats", "participants", "--params", "{...}", "--dry-run"]. The server fixes
        the profile. Safety checks, policies, confirmations, and critical tokens still apply.
        Streaming commands such as updates watch need output="jsonl" and bounded limits.
        """

        validate_run_args(args)
        try:
            return tool_result(
                await runner.run(
                    args,
                    stdin=stdin,
                    output=output,
                    timeout_seconds=min(max(timeout_seconds, 1), 600),
                )
            )
        except ClitgError as exc:
            raise ToolError(json_dumps(exc.info)) from exc

    for function, hints in (
        (dialogs_list, READ),
        (dialogs_search, READ),
        (contacts_resolve, READ),
        (inbox_list, READ),
        (messages_list, READ),
        (messages_get, READ),
        (messages_search, READ),
        (messages_context, READ),
        (messages_replies, READ),
        (messages_send, WRITE),
        (messages_reply, WRITE),
        (messages_read, WRITE),
        (clitg_commands, READ),
        (clitg_command, READ),
        (clitg_run, ANY),
    ):
        server.add_tool(function, annotations=hints)
