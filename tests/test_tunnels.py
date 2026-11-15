from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import pytest

import clitg.tunnels as tunnels
from clitg.errors import ClitgError
from clitg.models import ErrorCode
from clitg.tunnels import NetbirdTunnel, netbird_ip


class FakeStream:
    def __init__(self, lines: list[bytes], *, block: bool = False) -> None:
        self.lines = list(lines)
        self.block = block

    async def readline(self) -> bytes:
        if self.lines:
            return self.lines.pop(0)
        if self.block:
            await asyncio.sleep(3600)
        return b""


class FakeProcess:
    def __init__(
        self,
        *,
        lines: list[bytes] | None = None,
        stdout: bytes = b"",
        returncode: int | None = None,
        block: bool = False,
        communicate_delay: float = 0,
        ignore_terminate: bool = False,
        lookup_error: bool = False,
    ) -> None:
        self.stdout = FakeStream(lines or [], block=block)
        self.output = stdout
        self.returncode = returncode
        self.final_code = 0 if returncode is None else returncode
        self.communicate_delay = communicate_delay
        self.ignore_terminate = ignore_terminate
        self.lookup_error = lookup_error
        self.signals: list[str] = []
        self.done = asyncio.Event()

    async def communicate(self, payload: bytes | None = None) -> tuple[bytes, bytes]:
        del payload
        await asyncio.sleep(self.communicate_delay)
        return self.output, b""

    async def wait(self) -> int:
        if self.returncode is None:
            await self.done.wait()
        return self.final_code

    def _finish(self, code: int) -> None:
        self.returncode = code
        self.done.set()

    def terminate(self) -> None:
        self.signals.append("terminate")
        if self.lookup_error:
            raise ProcessLookupError
        if not self.ignore_terminate:
            self._finish(-15)

    def kill(self) -> None:
        self.signals.append("kill")
        self._finish(-9)


def install(monkeypatch: pytest.MonkeyPatch, process: FakeProcess) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []

    async def create(*args: Any, **kwargs: Any) -> FakeProcess:
        del kwargs
        calls.append(args)
        return process

    monkeypatch.setattr(tunnels.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(tunnels.asyncio, "create_subprocess_exec", create)
    return calls


async def test_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tunnels.shutil, "which", lambda name: None)
    with pytest.raises(ClitgError) as error:
        await netbird_ip()
    assert error.value.info.code == ErrorCode.INVALID_INPUT


async def test_netbird_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    status = json.dumps({"netbirdIp": "100.64.0.7/16"}).encode()
    calls = install(monkeypatch, FakeProcess(stdout=status, returncode=0))
    assert await netbird_ip() == "100.64.0.7"
    assert calls[0][1:] == ("status", "--json")


@pytest.mark.parametrize(
    ("process", "message"),
    [
        (FakeProcess(stdout=b"not json", returncode=0), "NetBird status is unavailable"),
        (FakeProcess(stdout=b"{}", returncode=0), "NetBird status is unavailable"),
        (FakeProcess(stdout=b'{"netbirdIp": ""}', returncode=0), "NetBird is not connected"),
        (
            FakeProcess(stdout=b'{"netbirdIp": "100.1.1.1"}', returncode=1),
            "NetBird is not connected",
        ),
    ],
)
async def test_netbird_ip_failures(
    monkeypatch: pytest.MonkeyPatch, process: FakeProcess, message: str
) -> None:
    install(monkeypatch, process)
    with pytest.raises(ClitgError) as error:
        await netbird_ip()
    assert error.value.info.message == message


async def test_netbird_ip_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    process = FakeProcess(communicate_delay=1)
    install(monkeypatch, process)
    with pytest.raises(ClitgError) as error:
        await netbird_ip(timeout_seconds=0.01)
    assert error.value.info.message == "NetBird status timed out"
    assert process.signals == ["kill"]


async def test_tunnel_reports_url_and_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    process = FakeProcess(
        lines=[b"Exposing service...\n", b"URL: https://clitg-demo-a1b2.proxy.example.com/.\n"]
    )
    calls = install(monkeypatch, process)
    tunnel = NetbirdTunnel(8765, "clitg-demo")
    assert await tunnel.start() == "https://clitg-demo-a1b2.proxy.example.com"
    assert calls[0][1:] == ("expose", "8765", "--with-name-prefix", "clitg-demo")
    await tunnel.stop()
    assert process.signals == ["terminate"]
    await tunnel.stop()


async def test_tunnel_exits_without_url(monkeypatch: pytest.MonkeyPatch) -> None:
    process = FakeProcess(lines=[b"peer expose is disabled\n"], returncode=1)
    install(monkeypatch, process)
    with pytest.raises(ClitgError) as error:
        await NetbirdTunnel(8765, "clitg-demo").start()
    assert error.value.info.details["exit_code"] == 1
    assert process.signals == []


async def test_tunnel_timeout_kills_stubborn_process(monkeypatch: pytest.MonkeyPatch) -> None:
    process = FakeProcess(block=True, ignore_terminate=True)
    install(monkeypatch, process)
    monkeypatch.setattr(tunnels.asyncio, "wait_for", _short_wait_for)
    with pytest.raises(ClitgError) as error:
        await NetbirdTunnel(8765, "clitg-demo", timeout_seconds=0.01).start()
    assert error.value.info.message == "NetBird did not report a public URL"
    assert process.signals == ["terminate", "kill"]


async def test_tunnel_stop_tolerates_exited_process(monkeypatch: pytest.MonkeyPatch) -> None:
    process = FakeProcess(lookup_error=True)
    install(monkeypatch, process)
    tunnel = NetbirdTunnel(8765, "clitg-demo")
    tunnel.process = cast(Any, process)
    monkeypatch.setattr(tunnels.asyncio, "wait_for", _short_wait_for)
    await tunnel.stop()
    assert process.signals == ["terminate", "kill"]


async def test_tunnel_requires_start() -> None:
    with pytest.raises(ClitgError) as error:
        await NetbirdTunnel(8765, "clitg-demo")._read_url()
    assert error.value.info.code == ErrorCode.INTERNAL


_real_wait_for = asyncio.wait_for


async def _short_wait_for(awaitable: Any, timeout: float | None) -> Any:
    return await _real_wait_for(awaitable, min(timeout or 0.01, 0.01))
