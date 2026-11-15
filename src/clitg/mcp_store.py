"""Persist MCP OAuth clients, pairing codes, authorization codes, and bearer tokens."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from clitg.errors import ClitgError
from clitg.models import ErrorCode
from clitg.storage import Paths

PAIRING_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PAIRING_TTL_SECONDS = 600
PAIRING_FAILURE_LIMIT = 10
PAIRING_FAILURE_WINDOW_SECONDS = 900
AUTH_REQUEST_TTL_SECONDS = 600
AUTH_REQUEST_ATTEMPTS = 5
AUTH_CODE_TTL_SECONDS = 300
ACCESS_TOKEN_TTL_SECONDS = 3600
REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 3600
MAX_CLIENTS = 100
UNPAIRED_CLIENT_TTL_SECONDS = 24 * 3600


def _now() -> datetime:
    return datetime.now(UTC)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _timestamp(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def normalize_pairing_code(value: str) -> str:
    """Normalize user-entered pairing codes by removing separators and case."""

    return "".join(character for character in value.upper() if character.isalnum())


def pairing_message(profile: str, code: str, expires_at: datetime) -> str:
    """Return the Saved Messages text that delivers one pairing code."""

    return (
        f"clitg MCP pairing code for profile {profile}: {code}\n"
        f"It expires at {expires_at.isoformat(timespec='seconds')} and works once. "
        "Enter it only on the clitg pairing page opened by a connection you started."
    )


def pairing_delivery_targets(delivery: str) -> tuple[bool, bool]:
    """Return whether a delivery mode shows the code in the terminal and Saved Messages."""

    return delivery in {"both", "terminal"}, delivery in {"both", "saved-messages"}


class McpStore:
    """Store MCP authorization state with hashed secrets in a private SQLite file."""

    def __init__(self, paths: Paths) -> None:
        self.path = paths.data_dir / "mcp.sqlite3"
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS clients (
                    profile TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    info_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_used_at TEXT,
                    PRIMARY KEY (profile, client_id)
                );
                CREATE TABLE IF NOT EXISTS pairing_codes (
                    code_hash TEXT PRIMARY KEY,
                    profile TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pairing_failures (
                    profile TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auth_requests (
                    request_id TEXT PRIMARY KEY,
                    profile TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    params_json TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS auth_codes (
                    code_hash TEXT PRIMARY KEY,
                    profile TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tokens (
                    token_hash TEXT PRIMARY KEY,
                    token_id TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    family TEXT NOT NULL,
                    name TEXT,
                    scopes_json TEXT NOT NULL,
                    resource TEXT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT,
                    revoked INTEGER NOT NULL DEFAULT 0
                );
                """,
            )
        if os.name == "posix":
            self.path.chmod(0o600)

    # Clients

    def register_client(self, profile: str, client_id: str, info: dict[str, Any]) -> None:
        """Persist one dynamically registered OAuth client."""

        self._prune_clients(profile)
        now = _now().isoformat()
        with self._connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM clients WHERE profile=?", (profile,)
            ).fetchone()[0]
            if int(count) >= MAX_CLIENTS:
                raise ClitgError(ErrorCode.CONFLICT, "Too many registered MCP clients")
            connection.execute(
                "INSERT OR REPLACE INTO clients VALUES (?, ?, ?, ?, NULL)",
                (profile, client_id, json.dumps(info, separators=(",", ":")), now),
            )

    def get_client(self, profile: str, client_id: str) -> dict[str, Any] | None:
        """Load one registered OAuth client."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT info_json FROM clients WHERE profile=? AND client_id=?",
                (profile, client_id),
            ).fetchone()
        return None if row is None else json.loads(row["info_json"])

    def list_clients(self, profile: str) -> list[dict[str, Any]]:
        """List registered clients without secrets."""

        now = _now().isoformat()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT c.client_id, c.info_json, c.created_at, c.last_used_at, "
                "(SELECT COUNT(*) FROM tokens t WHERE t.profile=c.profile "
                "AND t.client_id=c.client_id AND t.kind='refresh' AND t.revoked=0 "
                "AND t.expires_at > ?) AS active_grants "
                "FROM clients c WHERE c.profile=? ORDER BY c.created_at",
                (now, profile),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            info = json.loads(row["info_json"])
            result.append(
                {
                    "client_id": row["client_id"],
                    "client_name": info.get("client_name"),
                    "redirect_uris": info.get("redirect_uris") or [],
                    "created_at": _timestamp(row["created_at"]),
                    "last_used_at": _timestamp(row["last_used_at"]),
                    "paired": int(row["active_grants"]) > 0,
                }
            )
        return result

    def revoke_client(self, profile: str, client_id: str) -> dict[str, int]:
        """Delete one client and every grant it holds."""

        with self._connect() as connection:
            tokens = connection.execute(
                "DELETE FROM tokens WHERE profile=? AND client_id=?", (profile, client_id)
            ).rowcount
            connection.execute(
                "DELETE FROM auth_codes WHERE profile=? AND client_id=?", (profile, client_id)
            )
            connection.execute(
                "DELETE FROM auth_requests WHERE profile=? AND client_id=?", (profile, client_id)
            )
            clients = connection.execute(
                "DELETE FROM clients WHERE profile=? AND client_id=?", (profile, client_id)
            ).rowcount
        if not clients:
            raise ClitgError(ErrorCode.NOT_FOUND, "MCP client was not found")
        return {"clients": clients, "tokens": tokens}

    def has_paired_clients(self, profile: str) -> bool:
        """Return whether any OAuth client holds a live refresh grant."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM tokens WHERE profile=? AND kind='refresh' "
                "AND revoked=0 AND expires_at > ?",
                (profile, _now().isoformat()),
            ).fetchone()
        return int(row[0]) > 0

    def _prune_clients(self, profile: str) -> None:
        cutoff = (_now() - timedelta(seconds=UNPAIRED_CLIENT_TTL_SECONDS)).isoformat()
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM clients WHERE profile=? AND created_at < ? AND NOT EXISTS ("
                "SELECT 1 FROM tokens t WHERE t.profile=clients.profile "
                "AND t.client_id=clients.client_id AND t.revoked=0)",
                (profile, cutoff),
            )

    # Pairing codes

    def issue_pairing_code(self, profile: str) -> tuple[str, datetime]:
        """Issue one single-use pairing code and return it with its expiry."""

        raw = "".join(secrets.choice(PAIRING_ALPHABET) for _ in range(8))
        now = _now()
        expires_at = now + timedelta(seconds=PAIRING_TTL_SECONDS)
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM pairing_codes WHERE expires_at <= ?", (now.isoformat(),)
            )
            connection.execute(
                "INSERT INTO pairing_codes VALUES (?, ?, ?, ?)",
                (_digest(raw), profile, now.isoformat(), expires_at.isoformat()),
            )
        return f"{raw[:4]}-{raw[4:]}", expires_at

    def consume_pairing_code(self, profile: str, code: str) -> bool:
        """Consume a pairing code, recording failures and enforcing a lockout."""

        now = _now()
        window = (now - timedelta(seconds=PAIRING_FAILURE_WINDOW_SECONDS)).isoformat()
        with self._connect() as connection:
            connection.execute("DELETE FROM pairing_failures WHERE occurred_at < ?", (window,))
            failures = connection.execute(
                "SELECT COUNT(*) FROM pairing_failures WHERE profile=?", (profile,)
            ).fetchone()[0]
            if int(failures) >= PAIRING_FAILURE_LIMIT:
                raise ClitgError(
                    ErrorCode.RATE_LIMITED,
                    "Too many failed pairing attempts",
                    retryable=True,
                    retry_after_seconds=PAIRING_FAILURE_WINDOW_SECONDS,
                )
            deleted = connection.execute(
                "DELETE FROM pairing_codes WHERE code_hash=? AND profile=? AND expires_at > ?",
                (_digest(normalize_pairing_code(code)), profile, now.isoformat()),
            ).rowcount
            if deleted:
                return True
            connection.execute(
                "INSERT INTO pairing_failures VALUES (?, ?)", (profile, now.isoformat())
            )
        return False

    # Authorization requests and codes

    def save_auth_request(self, profile: str, client_id: str, params: dict[str, Any]) -> str:
        """Persist a pending authorization request awaiting the pairing code."""

        request_id = secrets.token_urlsafe(24)
        now = _now()
        expires_at = now + timedelta(seconds=AUTH_REQUEST_TTL_SECONDS)
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM auth_requests WHERE expires_at <= ?", (now.isoformat(),)
            )
            connection.execute(
                "INSERT INTO auth_requests VALUES (?, ?, ?, ?, ?, 0)",
                (
                    request_id,
                    profile,
                    client_id,
                    json.dumps(params, separators=(",", ":")),
                    expires_at.isoformat(),
                ),
            )
        return request_id

    def get_auth_request(self, profile: str, request_id: str) -> dict[str, Any] | None:
        """Load a live pending authorization request."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM auth_requests WHERE request_id=? AND profile=? AND expires_at > ?",
                (request_id, profile, _now().isoformat()),
            ).fetchone()
        if row is None:
            return None
        return {
            "request_id": row["request_id"],
            "client_id": row["client_id"],
            "params": json.loads(row["params_json"]),
            "attempts": int(row["attempts"]),
        }

    def fail_auth_request(self, profile: str, request_id: str) -> int:
        """Record one failed code entry and return the remaining attempts."""

        with self._connect() as connection:
            connection.execute(
                "UPDATE auth_requests SET attempts = attempts + 1 WHERE request_id=? AND profile=?",
                (request_id, profile),
            )
            row = connection.execute(
                "SELECT attempts FROM auth_requests WHERE request_id=? AND profile=?",
                (request_id, profile),
            ).fetchone()
            attempts = AUTH_REQUEST_ATTEMPTS if row is None else int(row["attempts"])
            remaining = max(AUTH_REQUEST_ATTEMPTS - attempts, 0)
            if remaining == 0:
                connection.execute(
                    "DELETE FROM auth_requests WHERE request_id=? AND profile=?",
                    (request_id, profile),
                )
        return remaining

    def delete_auth_request(self, profile: str, request_id: str) -> None:
        """Delete a pending authorization request."""

        with self._connect() as connection:
            connection.execute(
                "DELETE FROM auth_requests WHERE request_id=? AND profile=?",
                (request_id, profile),
            )

    def save_auth_code(
        self, profile: str, client_id: str, data: dict[str, Any]
    ) -> tuple[str, float]:
        """Persist one authorization code and return it with its expiry timestamp."""

        code = secrets.token_urlsafe(32)
        expires_at = _now() + timedelta(seconds=AUTH_CODE_TTL_SECONDS)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO auth_codes VALUES (?, ?, ?, ?, ?)",
                (
                    _digest(code),
                    profile,
                    client_id,
                    json.dumps(data, separators=(",", ":")),
                    expires_at.isoformat(),
                ),
            )
        return code, expires_at.timestamp()

    def load_auth_code(self, profile: str, client_id: str, code: str) -> dict[str, Any] | None:
        """Load a live authorization code for one client."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT data_json, expires_at FROM auth_codes "
                "WHERE code_hash=? AND profile=? AND client_id=? AND expires_at > ?",
                (_digest(code), profile, client_id, _now().isoformat()),
            ).fetchone()
        if row is None:
            return None
        data = json.loads(row["data_json"])
        data["expires_at"] = datetime.fromisoformat(row["expires_at"]).timestamp()
        return data

    def delete_auth_code(self, profile: str, code: str) -> bool:
        """Consume an authorization code exactly once."""

        with self._connect() as connection:
            return bool(
                connection.execute(
                    "DELETE FROM auth_codes WHERE code_hash=? AND profile=?",
                    (_digest(code), profile),
                ).rowcount
            )

    # Tokens

    def _insert_token(
        self,
        connection: sqlite3.Connection,
        *,
        profile: str,
        kind: str,
        client_id: str,
        family: str,
        scopes: list[str],
        resource: str | None,
        ttl_seconds: int | None,
        name: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        prefix = {"access": "clitg_at_", "refresh": "clitg_rt_", "static": "clitg_pat_"}[kind]
        token = prefix + secrets.token_urlsafe(32)
        now = _now()
        expires_at = None if ttl_seconds is None else now + timedelta(seconds=ttl_seconds)
        record = {
            "token_id": secrets.token_hex(8),
            "kind": kind,
            "client_id": client_id,
            "family": family,
            "name": name,
            "scopes": scopes,
            "resource": resource,
            "created_at": now.isoformat(),
            "expires_at": None if expires_at is None else expires_at.isoformat(),
        }
        connection.execute(
            "INSERT INTO tokens VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (
                _digest(token),
                record["token_id"],
                profile,
                kind,
                client_id,
                family,
                name,
                json.dumps(scopes),
                resource,
                record["created_at"],
                record["expires_at"],
            ),
        )
        return token, record

    def issue_token_pair(
        self, profile: str, client_id: str, scopes: list[str], resource: str | None
    ) -> dict[str, Any]:
        """Issue a short-lived access token and a rotating refresh token."""

        family = secrets.token_hex(16)
        with self._connect() as connection:
            access, _ = self._insert_token(
                connection,
                profile=profile,
                kind="access",
                client_id=client_id,
                family=family,
                scopes=scopes,
                resource=resource,
                ttl_seconds=ACCESS_TOKEN_TTL_SECONDS,
            )
            refresh, _ = self._insert_token(
                connection,
                profile=profile,
                kind="refresh",
                client_id=client_id,
                family=family,
                scopes=scopes,
                resource=resource,
                ttl_seconds=REFRESH_TOKEN_TTL_SECONDS,
            )
            connection.execute(
                "UPDATE clients SET last_used_at=? WHERE profile=? AND client_id=?",
                (_now().isoformat(), profile, client_id),
            )
        return {
            "access_token": access,
            "refresh_token": refresh,
            "expires_in": ACCESS_TOKEN_TTL_SECONDS,
            "scopes": scopes,
        }

    def load_token(self, profile: str, token: str, kinds: tuple[str, ...]) -> dict[str, Any] | None:
        """Load a live token of one of the given kinds."""

        placeholders = ",".join("?" for _ in kinds)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM tokens WHERE token_hash=? AND profile=? "
                f"AND kind IN ({placeholders}) AND revoked=0 "
                "AND (expires_at IS NULL OR expires_at > ?)",
                (_digest(token), profile, *kinds, _now().isoformat()),
            ).fetchone()
        if row is None:
            return None
        expires_at = row["expires_at"]
        return {
            "token_id": row["token_id"],
            "kind": row["kind"],
            "client_id": row["client_id"],
            "family": row["family"],
            "scopes": json.loads(row["scopes_json"]),
            "resource": row["resource"],
            "expires_at": None
            if expires_at is None
            else int(datetime.fromisoformat(expires_at).timestamp()),
        }

    def revoke_family(self, profile: str, family: str) -> int:
        """Revoke every token issued together with one grant."""

        with self._connect() as connection:
            return connection.execute(
                "UPDATE tokens SET revoked=1 WHERE profile=? AND family=? AND revoked=0",
                (profile, family),
            ).rowcount

    def create_static_token(
        self, profile: str, name: str, ttl_seconds: int | None
    ) -> tuple[str, dict[str, Any]]:
        """Create one named static bearer token for header-based MCP clients."""

        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM tokens WHERE profile=? AND kind='static' AND name=? AND revoked=0 "
                "AND (expires_at IS NULL OR expires_at > ?)",
                (profile, name, _now().isoformat()),
            ).fetchone()
            if exists:
                raise ClitgError(ErrorCode.CONFLICT, f"Token '{name}' already exists")
            token, record = self._insert_token(
                connection,
                profile=profile,
                kind="static",
                client_id=f"static:{name}",
                family=secrets.token_hex(16),
                scopes=["clitg"],
                resource=None,
                ttl_seconds=ttl_seconds,
                name=name,
            )
        return token, self._public_token(record)

    def list_static_tokens(self, profile: str) -> list[dict[str, Any]]:
        """List static bearer tokens without secrets."""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tokens WHERE profile=? AND kind='static' ORDER BY created_at",
                (profile,),
            ).fetchall()
        now = _now().isoformat()
        return [
            {
                **self._public_token(
                    {
                        "token_id": row["token_id"],
                        "name": row["name"],
                        "created_at": row["created_at"],
                        "expires_at": row["expires_at"],
                    }
                ),
                "active": not row["revoked"]
                and (row["expires_at"] is None or row["expires_at"] > now),
            }
            for row in rows
        ]

    def revoke_static_token(self, profile: str, name: str) -> dict[str, int]:
        """Revoke one named static bearer token."""

        with self._connect() as connection:
            count = connection.execute(
                "UPDATE tokens SET revoked=1 WHERE profile=? AND kind='static' AND name=? "
                "AND revoked=0",
                (profile, name),
            ).rowcount
        if not count:
            raise ClitgError(ErrorCode.NOT_FOUND, f"Token '{name}' was not found")
        return {"tokens": count}

    @staticmethod
    def _public_token(record: dict[str, Any]) -> dict[str, Any]:
        return {
            "token_id": record["token_id"],
            "name": record["name"],
            "created_at": _timestamp(record["created_at"]),
            "expires_at": _timestamp(record["expires_at"]),
        }
