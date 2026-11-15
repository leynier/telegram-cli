from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from clitg.errors import ClitgError
from clitg.models import CommandResult, ErrorCode, Profile
from clitg.service import ClitgService
from clitg.storage import Paths


@pytest.fixture
def service(paths: Paths) -> ClitgService:
    value = ClitgService(paths)
    value.profiles.create(Profile(name="demo", api_id=1, api_hash="secret"))
    return value


def fake_send(service: ClitgService, monkeypatch: pytest.MonkeyPatch, *, fail: bool) -> list[Any]:
    sent: list[Any] = []

    async def send(requested: str | None, peer: str, **kwargs: Any) -> CommandResult:
        sent.append((requested, peer, kwargs))
        if fail:
            raise ClitgError(ErrorCode.AUTH_REQUIRED, "The profile is not authenticated")
        return CommandResult(data={"sent": True})

    monkeypatch.setattr(service, "send", send)
    return sent


def test_mcp_store_is_cached(service: ClitgService) -> None:
    assert service.mcp is service.mcp


async def test_mcp_pair(service: ClitgService, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ClitgError) as invalid:
        await service.mcp_pair(None, delivery="email", dry_run=False)
    assert invalid.value.info.code == ErrorCode.INVALID_INPUT
    preview = await service.mcp_pair(None, delivery="both", dry_run=True)
    assert preview.data["delivered_to"] == ["terminal", "saved-messages"]
    terminal = await service.mcp_pair(None, delivery="terminal", dry_run=False)
    assert terminal.data["delivered_to"] == ["terminal"]
    assert service.mcp.consume_pairing_code("demo", terminal.data["code"]) is True
    sent = fake_send(service, monkeypatch, fail=False)
    both = await service.mcp_pair("demo", delivery="both", dry_run=False)
    assert both.data["delivered_to"] == ["terminal", "saved-messages"]
    assert sent[0][1] == "me"
    assert both.data["code"] in sent[0][2]["text"]
    saved = await service.mcp_pair("demo", delivery="saved-messages", dry_run=False)
    assert saved.data["delivered_to"] == ["saved-messages"]
    assert "code" not in saved.data


async def test_mcp_pair_delivery_failure(
    service: ClitgService, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_send(service, monkeypatch, fail=True)
    both = await service.mcp_pair(None, delivery="both", dry_run=False)
    assert both.data["saved_messages_error"] == ErrorCode.AUTH_REQUIRED
    assert both.data["delivered_to"] == ["terminal"]
    with pytest.raises(ClitgError) as error:
        await service.mcp_pair(None, delivery="saved-messages", dry_run=False)
    assert error.value.info.code == ErrorCode.AUTH_REQUIRED


def test_mcp_clients(service: ClitgService) -> None:
    assert service.mcp_clients(None).items == []
    with pytest.raises(ClitgError) as missing:
        service.mcp_revoke_client(None, "c1", dry_run=True, confirmation=None)
    assert missing.value.info.code == ErrorCode.NOT_FOUND
    service.mcp.register_client("demo", "c1", {"client_name": "App"})
    assert service.mcp_clients(None).data["clients"][0]["client_name"] == "App"
    preview = service.mcp_revoke_client(None, "c1", dry_run=True, confirmation=None)
    assert preview.data["required_confirmation"] == "mcp.clients.revoke"
    with pytest.raises(ClitgError) as unconfirmed:
        service.mcp_revoke_client(None, "c1", dry_run=False, confirmation=None)
    assert unconfirmed.value.info.code == ErrorCode.CONFIRMATION_REQUIRED
    revoked = service.mcp_revoke_client(
        None, "c1", dry_run=False, confirmation="mcp.clients.revoke"
    )
    assert revoked.data == {"revoked": {"clients": 1, "tokens": 0}}


def test_mcp_tokens(service: ClitgService, tmp_path: Path) -> None:
    with pytest.raises(ClitgError):
        service.mcp_create_token(
            None, "Bad Name", expires_days=None, token_file=None, dry_run=False
        )
    with pytest.raises(ClitgError):
        service.mcp_create_token(None, "laptop", expires_days=0, token_file=None, dry_run=False)
    existing = tmp_path / "existing"
    existing.write_text("x", encoding="utf-8")
    with pytest.raises(ClitgError) as conflict:
        service.mcp_create_token(
            None, "laptop", expires_days=None, token_file=existing, dry_run=True
        )
    assert conflict.value.info.code == ErrorCode.CONFLICT
    preview = service.mcp_create_token(
        None, "laptop", expires_days=30, token_file=None, dry_run=True
    )
    assert preview.data["dry_run"] is True
    printed = service.mcp_create_token(
        None, "laptop", expires_days=30, token_file=None, dry_run=False
    )
    assert printed.data["token"].startswith("clitg_pat_")
    target = tmp_path / "token.txt"
    written = service.mcp_create_token(
        None, "mesh", expires_days=None, token_file=target, dry_run=False
    )
    assert "token" not in written.data
    assert target.read_text(encoding="utf-8").startswith("clitg_pat_")
    if os.name == "posix":
        assert target.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ClitgError) as unwritable:
        service.mcp_create_token(
            None,
            "broken",
            expires_days=None,
            token_file=tmp_path / "missing" / "token.txt",
            dry_run=False,
        )
    assert unwritable.value.info.code == ErrorCode.INVALID_INPUT
    states = {item["name"]: item["active"] for item in service.mcp_tokens(None).items or []}
    assert states == {"laptop": True, "mesh": True, "broken": False}
    with pytest.raises(ClitgError) as missing:
        service.mcp_revoke_token(None, "broken", dry_run=True, confirmation=None)
    assert missing.value.info.code == ErrorCode.NOT_FOUND
    preview_revoke = service.mcp_revoke_token(None, "laptop", dry_run=True, confirmation=None)
    assert preview_revoke.data["required_confirmation"] == "mcp.tokens.revoke"
    with pytest.raises(ClitgError):
        service.mcp_revoke_token(None, "laptop", dry_run=False, confirmation="wrong")
    revoked = service.mcp_revoke_token(
        None, "laptop", dry_run=False, confirmation="mcp.tokens.revoke"
    )
    assert revoked.data == {"revoked": {"tokens": 1}}
