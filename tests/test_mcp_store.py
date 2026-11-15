from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import clitg.mcp_store as mcp_store
from clitg.errors import ClitgError
from clitg.mcp_store import (
    AUTH_REQUEST_ATTEMPTS,
    MAX_CLIENTS,
    PAIRING_FAILURE_LIMIT,
    McpStore,
    normalize_pairing_code,
    pairing_delivery_targets,
    pairing_message,
)
from clitg.models import ErrorCode
from clitg.storage import Paths


@pytest.fixture
def store(paths: Paths) -> McpStore:
    return McpStore(paths)


def shift(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    moment = datetime.now(UTC) + timedelta(seconds=seconds)
    monkeypatch.setattr(mcp_store, "_now", lambda: moment)


def test_helpers() -> None:
    assert normalize_pairing_code(" ab-cd 23 ") == "ABCD23"
    assert pairing_delivery_targets("both") == (True, True)
    assert pairing_delivery_targets("terminal") == (True, False)
    assert pairing_delivery_targets("saved-messages") == (False, True)
    assert pairing_delivery_targets("none") == (False, False)
    message = pairing_message("demo", "ABCD-EFGH", datetime(2026, 1, 1, tzinfo=UTC))
    assert "ABCD-EFGH" in message
    assert "demo" in message


def test_clients_lifecycle(store: McpStore, monkeypatch: pytest.MonkeyPatch) -> None:
    store.register_client("demo", "c1", {"client_name": "App", "redirect_uris": ["https://x/cb"]})
    store.register_client("demo", "c2", {})
    assert store.get_client("demo", "c1") == {
        "client_name": "App",
        "redirect_uris": ["https://x/cb"],
    }
    assert store.get_client("other", "c1") is None
    listed = store.list_clients("demo")
    assert [item["client_id"] for item in listed] == ["c1", "c2"]
    assert listed[1]["client_name"] is None
    assert listed[1]["redirect_uris"] == []
    assert listed[0]["paired"] is False
    assert isinstance(listed[0]["created_at"], datetime)
    assert listed[0]["last_used_at"] is None
    assert store.has_paired_clients("demo") is False
    store.issue_token_pair("demo", "c1", ["clitg"], "https://x/mcp")
    assert store.has_paired_clients("demo") is True
    assert store.list_clients("demo")[0]["paired"] is True
    assert isinstance(store.list_clients("demo")[0]["last_used_at"], datetime)
    shift(monkeypatch, 2 * 24 * 3600)
    store.register_client("demo", "c3", {})
    assert [item["client_id"] for item in store.list_clients("demo")] == ["c1", "c3"]
    assert store.revoke_client("demo", "c1") == {"clients": 1, "tokens": 2}
    with pytest.raises(ClitgError) as missing:
        store.revoke_client("demo", "c1")
    assert missing.value.info.code == ErrorCode.NOT_FOUND


def test_client_limit(store: McpStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_store, "MAX_CLIENTS", 2)
    store.register_client("demo", "a", {})
    store.register_client("demo", "b", {})
    with pytest.raises(ClitgError) as error:
        store.register_client("demo", "c", {})
    assert error.value.info.code == ErrorCode.CONFLICT
    assert MAX_CLIENTS == 100


def test_pairing_codes(store: McpStore, monkeypatch: pytest.MonkeyPatch) -> None:
    code, expires_at = store.issue_pairing_code("demo")
    assert len(code) == 9 and code[4] == "-"
    assert expires_at > datetime.now(UTC)
    assert store.consume_pairing_code("other", code) is False
    assert store.consume_pairing_code("demo", code.lower().replace("-", " ")) is True
    assert store.consume_pairing_code("demo", code) is False
    expired, _ = store.issue_pairing_code("demo")
    shift(monkeypatch, 601)
    assert store.consume_pairing_code("demo", expired) is False
    store.issue_pairing_code("demo")


def test_pairing_lockout(store: McpStore, monkeypatch: pytest.MonkeyPatch) -> None:
    code, _ = store.issue_pairing_code("demo")
    for _ in range(PAIRING_FAILURE_LIMIT):
        assert store.consume_pairing_code("demo", "WRONG") is False
    with pytest.raises(ClitgError) as locked:
        store.consume_pairing_code("demo", code)
    assert locked.value.info.code == ErrorCode.RATE_LIMITED
    assert locked.value.info.retryable is True
    shift(monkeypatch, 901)
    store.issue_pairing_code("demo")
    assert store.consume_pairing_code("demo", "WRONG") is False


def test_auth_requests(store: McpStore, monkeypatch: pytest.MonkeyPatch) -> None:
    request_id = store.save_auth_request("demo", "c1", {"state": "s"})
    assert store.get_auth_request("demo", request_id) == {
        "request_id": request_id,
        "client_id": "c1",
        "params": {"state": "s"},
        "attempts": 0,
    }
    assert store.get_auth_request("other", request_id) is None
    for remaining in range(AUTH_REQUEST_ATTEMPTS - 1, 0, -1):
        assert store.fail_auth_request("demo", request_id) == remaining
    assert store.fail_auth_request("demo", request_id) == 0
    assert store.get_auth_request("demo", request_id) is None
    assert store.fail_auth_request("demo", request_id) == 0
    deleted = store.save_auth_request("demo", "c1", {})
    store.delete_auth_request("demo", deleted)
    assert store.get_auth_request("demo", deleted) is None
    expired = store.save_auth_request("demo", "c1", {})
    shift(monkeypatch, 601)
    assert store.get_auth_request("demo", expired) is None
    store.save_auth_request("demo", "c1", {})


def test_auth_codes(store: McpStore, monkeypatch: pytest.MonkeyPatch) -> None:
    code, expires_at = store.save_auth_code("demo", "c1", {"scopes": ["clitg"]})
    assert expires_at > datetime.now(UTC).timestamp()
    loaded = store.load_auth_code("demo", "c1", code)
    assert loaded is not None
    assert loaded["scopes"] == ["clitg"]
    assert isinstance(loaded["expires_at"], float)
    assert store.load_auth_code("demo", "c2", code) is None
    assert store.delete_auth_code("demo", code) is True
    assert store.delete_auth_code("demo", code) is False
    expired, _ = store.save_auth_code("demo", "c1", {})
    shift(monkeypatch, 301)
    assert store.load_auth_code("demo", "c1", expired) is None


def test_tokens(store: McpStore, monkeypatch: pytest.MonkeyPatch) -> None:
    grant = store.issue_token_pair("demo", "c1", ["clitg"], "https://x/mcp")
    assert grant["access_token"].startswith("clitg_at_")
    assert grant["refresh_token"].startswith("clitg_rt_")
    access = store.load_token("demo", grant["access_token"], ("access",))
    assert access is not None
    assert access["kind"] == "access"
    assert access["resource"] == "https://x/mcp"
    assert isinstance(access["expires_at"], int)
    assert store.load_token("demo", grant["access_token"], ("refresh",)) is None
    assert store.load_token("other", grant["access_token"], ("access",)) is None
    assert store.revoke_family("demo", access["family"]) == 2
    assert store.load_token("demo", grant["refresh_token"], ("refresh",)) is None
    short = store.issue_token_pair("demo", "c1", [], None)
    shift(monkeypatch, 3601)
    assert store.load_token("demo", short["access_token"], ("access",)) is None
    assert store.load_token("demo", short["refresh_token"], ("refresh",)) is not None


def test_static_tokens(store: McpStore, monkeypatch: pytest.MonkeyPatch) -> None:
    token, record = store.create_static_token("demo", "laptop", None)
    assert token.startswith("clitg_pat_")
    assert record["name"] == "laptop"
    assert record["expires_at"] is None
    assert isinstance(record["created_at"], datetime)
    loaded = store.load_token("demo", token, ("static",))
    assert loaded is not None
    assert loaded["expires_at"] is None
    assert loaded["client_id"] == "static:laptop"
    with pytest.raises(ClitgError) as duplicate:
        store.create_static_token("demo", "laptop", None)
    assert duplicate.value.info.code == ErrorCode.CONFLICT
    _, expiring = store.create_static_token("demo", "short", 60)
    assert isinstance(expiring["expires_at"], datetime)
    listed = store.list_static_tokens("demo")
    assert [(item["name"], item["active"]) for item in listed] == [
        ("laptop", True),
        ("short", True),
    ]
    assert store.revoke_static_token("demo", "laptop") == {"tokens": 1}
    with pytest.raises(ClitgError) as missing:
        store.revoke_static_token("demo", "laptop")
    assert missing.value.info.code == ErrorCode.NOT_FOUND
    shift(monkeypatch, 61)
    assert [item["active"] for item in store.list_static_tokens("demo")] == [False, False]
    store.create_static_token("demo", "short", None)


def test_store_skips_chmod_off_posix(paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_store.os, "name", "nt")
    assert McpStore(paths).path.exists()
