"""Optional tunnel providers that publish the local MCP server."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import shutil
from typing import Literal

from clitg.errors import ClitgError
from clitg.models import ErrorCode

TunnelName = Literal["none", "netbird"]

_URL_PATTERN = re.compile(r"https://[A-Za-z0-9.-]+(?::\d+)?(?:/[^\s\"']*)?")
_NETBIRD_HINT = (
    "Enable Peer Expose for this peer, or create a reverse proxy service and pass --public-url"
)


def _netbird_binary() -> str:
    binary = shutil.which("netbird")
    if binary is None:
        raise ClitgError(
            ErrorCode.INVALID_INPUT,
            "The NetBird CLI was not found",
            details={"hint": "Install NetBird or pass --public-url"},
        )
    return binary


async def netbird_ip(timeout_seconds: float = 10) -> str:
    """Return this peer's NetBird IPv4 address from `netbird status --json`."""

    process = await asyncio.create_subprocess_exec(
        _netbird_binary(),
        "status",
        "--json",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout_seconds)
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise ClitgError(ErrorCode.NETWORK, "NetBird status timed out") from exc
    try:
        address = str(json.loads(stdout)["netbirdIp"]).split("/", maxsplit=1)[0]
    except (ValueError, KeyError, TypeError) as exc:
        raise ClitgError(ErrorCode.NETWORK, "NetBird status is unavailable") from exc
    if process.returncode != 0 or not address:
        raise ClitgError(ErrorCode.NETWORK, "NetBird is not connected")
    return address


class NetbirdTunnel:
    """Supervise one ephemeral `netbird expose` session."""

    def __init__(self, port: int, name_prefix: str, *, timeout_seconds: float = 30) -> None:
        self.port = port
        self.name_prefix = name_prefix
        self.timeout_seconds = timeout_seconds
        self.process: asyncio.subprocess.Process | None = None

    async def start(self) -> str:
        """Start the expose session and return its public HTTPS URL."""

        self.process = await asyncio.create_subprocess_exec(
            _netbird_binary(),
            "expose",
            str(self.port),
            "--with-name-prefix",
            self.name_prefix,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            return await asyncio.wait_for(self._read_url(), self.timeout_seconds)
        except TimeoutError as exc:
            await self.stop()
            raise ClitgError(
                ErrorCode.NETWORK,
                "NetBird did not report a public URL",
                details={"hint": _NETBIRD_HINT},
            ) from exc
        except ClitgError:
            await self.stop()
            raise

    async def _read_url(self) -> str:
        process = self.process
        if process is None or process.stdout is None:
            raise ClitgError(ErrorCode.INTERNAL, "NetBird tunnel was not started")
        while line := await process.stdout.readline():
            match = _URL_PATTERN.search(line.decode("utf-8", errors="replace"))
            if match:
                return match.group(0).rstrip(".,;)/")
        code = await process.wait()
        raise ClitgError(
            ErrorCode.NETWORK,
            "NetBird expose exited before publishing the server",
            details={"exit_code": code, "hint": _NETBIRD_HINT},
        )

    async def stop(self) -> None:
        """Stop the expose session and remove the public endpoint."""

        process = self.process
        self.process = None
        if process is None or process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 5)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
