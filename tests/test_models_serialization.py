from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

import clitg
from clitg.errors import EXIT_BY_CODE, ClitgError
from clitg.models import ErrorCode, ProfileView
from clitg.serialization import (
    decode_cursor,
    encode_cursor,
    json_dumps,
    payload_hash,
    redact,
    to_jsonable,
)


class ExampleModel(BaseModel):
    value: str


class RawObject:
    def to_dict(self) -> dict[str, object]:
        return {"_": "RawObject", "big_id": 9_999_999_999_999_999, "blob": b"x"}


def test_version_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.metadata

    def missing(_: str) -> str:
        raise importlib.metadata.PackageNotFoundError

    monkeypatch.setattr(importlib.metadata, "version", missing)
    reloaded = importlib.reload(clitg)
    assert reloaded.__version__ == "0.3.2"
    monkeypatch.undo()
    assert importlib.reload(clitg).__version__ == "0.3.2"


def test_error_contract_and_exit_codes() -> None:
    error = ClitgError(
        ErrorCode.RATE_LIMITED,
        "wait",
        details={"x": 1},
        retryable=True,
        retry_after_seconds=4,
    )
    assert error.exit_code == 7
    assert error.info.retryable is True
    assert set(EXIT_BY_CODE) == set(ErrorCode)


def test_json_conversion_and_redaction() -> None:
    aware = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
    naive = datetime(2026, 1, 2, 3, 4)
    value = {
        "model": ExampleModel(value="ok"),
        "raw": RawObject(),
        "list": (1, 2),
        "set": {3},
        "aware": aware,
        "naive": naive,
        "date": date(2026, 1, 2),
        "path": Path("x"),
        "id": 42,
        "api_id": 42,
        "large": 9_999_999_999_999_999,
        "bool": True,
        "other": SimpleNamespace(x=1),
        "access_hash": 123,
        "api_hash": "secret",
        "autologin_token": "token",
        "nested": [{"password": "secret"}],
    }
    converted = to_jsonable(value, raw=True)
    assert converted["id"] == "42"
    assert converted["api_id"] == 42
    assert converted["large"] == "9999999999999999"
    assert converted["aware"].endswith("Z")
    assert converted["naive"].endswith("Z")
    assert converted["date"] == "2026-01-02"
    assert converted["path"] == "x"
    assert converted["other"].startswith("namespace")
    assert redact(converted)["access_hash"] == "[REDACTED]"
    assert redact(converted)["api_hash"] == "[REDACTED]"
    assert redact(converted)["autologin_token"] == "[REDACTED]"
    assert redact(converted)["nested"][0]["password"] == "[REDACTED]"
    assert redact("plain") == "plain"
    dumped = json.loads(json_dumps(value))
    assert dumped["api_hash"] == "[REDACTED]"
    assert dumped["raw"]["blob"] == {"$bytes": "eA=="}


def test_payload_hash_is_deterministic() -> None:
    assert payload_hash({"b": 2, "a": 1}) == payload_hash({"a": 1, "b": 2})
    assert payload_hash({"a": 1}) != payload_hash({"a": 2})


def test_cursor_round_trip_and_errors() -> None:
    cursor = encode_cursor({"offset": 3})
    assert decode_cursor(cursor) == {"offset": 3}
    assert decode_cursor(None) == {}
    with pytest.raises(ValueError, match="Invalid cursor"):
        decode_cursor("not-base64!")
    unsupported = encode_cursor({"v": 2, "offset": 1})
    with pytest.raises(ValueError, match="Unsupported cursor"):
        decode_cursor(unsupported)


def test_profile_view_schema() -> None:
    schema = ProfileView.model_json_schema()
    assert schema["title"] == "ProfileView"
