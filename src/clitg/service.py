"""Application services coordinating storage, safety, and Telegram."""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from telethon.errors import FloodWaitError, RPCError

from clitg import SCHEMA_VERSION, __version__
from clitg.catalog import capability_catalog, command_catalog, schema_catalog
from clitg.credentials import CredentialStore
from clitg.errors import ClitgError
from clitg.features import (
    FEATURE_BY_COMMAND,
    OFFSET,
    FeatureCommand,
    build_feature_params,
    normalize_feature_result,
)
from clitg.mcp_store import McpStore, pairing_delivery_targets, pairing_message
from clitg.models import (
    BatchOperation,
    CommandResult,
    ErrorCode,
    FeatureResult,
    PolicyDocument,
    Profile,
)
from clitg.operations import OPERATION_BY_COMMAND, Operation, normalize_params
from clitg.policy import evaluate_policy, load_policy, require_policy
from clitg.serialization import decode_cursor, encode_cursor, json_dumps
from clitg.storage import Paths, ProfileStore, StateStore
from clitg.telegram import TelegramAdapter

T = TypeVar("T")

REPEAT_PERIODS = {
    "daily": 86_400,
    "weekly": 604_800,
    "biweekly": 1_209_600,
    "monthly": 2_592_000,
    "quarterly": 7_862_400,
    "semiannual": 15_724_800,
    "yearly": 31_536_000,
}


def repeat_period(value: str | None, schedule_at: datetime | None) -> int | None:
    """Validate and resolve a named Telegram repeat interval."""

    if value is None:
        return None
    if schedule_at is None:
        raise ClitgError(ErrorCode.INVALID_INPUT, "--repeat requires --schedule-at")
    try:
        return REPEAT_PERIODS[value]
    except KeyError as exc:
        raise ClitgError(
            ErrorCode.INVALID_INPUT,
            "Unknown repeat interval",
            details={"value": value, "allowed": list(REPEAT_PERIODS)},
        ) from exc


def feature_cursor_values(
    feature: FeatureCommand,
    command: str,
    values: dict[str, Any],
) -> dict[str, Any]:
    """Decode a feature cursor without exposing Telegram offsets to callers."""

    cursor = values.get("cursor")
    if not feature.paginated or not cursor:
        return values
    try:
        state = decode_cursor(str(cursor))
    except ValueError as exc:
        raise ClitgError(ErrorCode.INVALID_INPUT, "Invalid feature cursor") from exc
    if (
        state.get("feature") != command
        or state.get("parameter") != feature.cursor_param
        or "offset" not in state
    ):
        raise ClitgError(ErrorCode.INVALID_INPUT, "Feature cursor does not match this command")
    return {**values, "cursor": state["offset"]}


def _feature_result_items(value: Any) -> list[Any]:
    if not isinstance(value, dict):
        return []
    categories = value.get("categories")
    if isinstance(categories, list):
        nested = [
            peer
            for category in categories
            if isinstance(category, dict)
            for peer in category.get("peers", [])
        ]
        if nested:
            return nested
    candidates = [
        item
        for key, item in value.items()
        if key not in {"users", "chats", "categories"} and isinstance(item, list)
    ]
    return max(candidates, key=len, default=[])


def feature_next_cursor(
    feature: FeatureCommand,
    command: str,
    values: dict[str, Any],
    result: Any,
) -> str | None:
    """Derive the next opaque cursor from a paginated Telegram result."""

    if not feature.paginated or not isinstance(result, dict):
        return None
    next_offset = result.get("next_offset")
    if next_offset in {None, ""}:
        items = _feature_result_items(result)
        limit = int(values.get("limit") or 100)
        if not items or len(items) < limit:
            return None
        current = values.get("cursor") or 0
        if feature.cursor_param == "offset_id":
            last = items[-1]
            if not isinstance(last, dict) or last.get("id") is None:
                return None
            next_offset = last["id"]
        elif feature.cursor_param == "offset" and OFFSET not in feature.options:
            next_offset = int(current) + len(items)
        else:
            return None
    return encode_cursor(
        {
            "feature": command,
            "parameter": feature.cursor_param,
            "offset": next_offset,
        }
    )


class ClitgService:
    """Implement every CLI use case behind a testable API."""

    def __init__(self, paths: Paths | None = None, *, timeout_seconds: int = 30) -> None:
        self.paths = paths or Paths()
        self.profiles = ProfileStore(self.paths)
        self.state = StateStore(self.paths)
        self.credentials = CredentialStore(self.paths)
        self.telegram = TelegramAdapter(self.paths, timeout_seconds=timeout_seconds)

    def profile(self, requested: str | None) -> Profile:
        """Resolve a profile and apply explicit environment credential overrides."""

        profile = self.profiles.resolve(requested)
        changes: dict[str, Any] = {}
        if api_id := os.getenv("CLITG_API_ID"):
            try:
                changes["api_id"] = int(api_id)
            except ValueError as exc:
                raise ClitgError(
                    ErrorCode.INVALID_INPUT, "CLITG_API_ID must be an integer"
                ) from exc
        if api_hash := os.getenv("CLITG_API_HASH"):
            changes["api_hash"] = api_hash
        elif profile.api_hash:
            reference = self.credentials.save(profile.name, profile.api_hash)
            profile = self.profiles.set_secret_reference(profile.name, reference)
            changes["api_hash"] = self.credentials.load(reference)
        elif profile.api_hash_ref:
            changes["api_hash"] = self.credentials.load(profile.api_hash_ref)
        else:
            raise ClitgError(ErrorCode.PROFILE_ERROR, "The profile API hash is unavailable")
        if phone := os.getenv("CLITG_PHONE"):
            changes["phone"] = phone
        return profile.model_copy(update=changes)

    async def telegram_call(self, operation: Awaitable[T]) -> T:
        """Translate Telethon and network failures into the public error contract."""

        try:
            return await operation
        except ClitgError:
            raise
        except FloodWaitError as exc:
            raise ClitgError(
                ErrorCode.RATE_LIMITED,
                "Telegram rate limit reached",
                retryable=True,
                retry_after_seconds=int(exc.seconds),
            ) from exc
        except RPCError as exc:
            message = str(exc)
            lowered = message.lower()
            code = (
                ErrorCode.PERMISSION_DENIED
                if "admin" in lowered or "forbidden" in lowered
                else ErrorCode.TELEGRAM_RPC
            )
            raise ClitgError(code, message, details={"rpc_error": exc.__class__.__name__}) from exc
        except (TimeoutError, ConnectionError, OSError) as exc:
            raise ClitgError(ErrorCode.NETWORK, str(exc), retryable=True) from exc

    def create_profile(
        self,
        name: str,
        api_id: int,
        api_hash: str,
        phone: str | None,
        *,
        make_default: bool,
    ) -> CommandResult:
        """Create a profile."""

        normalized = self.profiles.validate_name(name)
        reference = self.credentials.save(normalized, api_hash)
        return CommandResult(
            data=self.profiles.create(
                Profile(
                    name=normalized,
                    api_id=api_id,
                    api_hash_ref=reference,
                    phone=phone,
                ),
                make_default=make_default,
            )
        )

    def list_profiles(self) -> CommandResult:
        """List profiles."""

        return CommandResult(data=self.profiles.list(), items=self.profiles.list())

    def get_profile(self, name: str) -> CommandResult:
        """Get a safe profile."""

        return CommandResult(data=self.profiles.get(name))

    def set_default_profile(self, name: str) -> CommandResult:
        """Set the default profile."""

        return CommandResult(data=self.profiles.set_default(name))

    def remove_profile(
        self, name: str, *, dry_run: bool, confirmation: str | None
    ) -> CommandResult:
        """Remove local profile metadata with explicit intent."""

        preview = {"action": "profiles.remove", "profile": name, "server_session_revoked": False}
        if dry_run:
            return CommandResult(data={**preview, "dry_run": True})
        self._require_confirmation("profiles.remove", confirmation)
        removed = self.profiles.remove(name)
        return CommandResult(data={**preview, "removed": removed})

    async def request_code(self, requested: str | None, phone: str | None) -> CommandResult:
        """Begin login and persist an opaque transaction."""

        profile = self.profile(requested)
        selected_phone = phone or profile.phone
        if not selected_phone:
            raise ClitgError(ErrorCode.INVALID_INPUT, "A phone number is required")
        code_hash = await self.telegram_call(self.telegram.request_code(profile, selected_phone))
        login = self.state.save_login(profile.name, selected_phone, code_hash)
        return CommandResult(
            data={
                "login_id": login.login_id,
                "profile": login.profile,
                "phone": login.phone,
                "expires_at": login.expires_at,
            }
        )

    async def verify_login(
        self,
        requested: str | None,
        login_id: str,
        code: str,
        password: str | None,
    ) -> CommandResult:
        """Finish a pending Telegram login."""

        login = self.state.get_login(login_id)
        profile = self.profile(requested or login.profile)
        if profile.name != login.profile:
            raise ClitgError(ErrorCode.CONFLICT, "Login transaction belongs to another profile")
        user = await self.telegram_call(
            self.telegram.verify(
                profile,
                login.phone,
                code,
                login.phone_code_hash,
                password,
            )
        )
        self.state.delete_login(login_id)
        return CommandResult(data={"authorized": True, "user": user})

    async def auth_status(self, requested: str | None) -> CommandResult:
        """Return profile authorization state."""

        profile = self.profile(requested)
        return CommandResult(data=await self.telegram_call(self.telegram.auth_status(profile)))

    async def qr_login(
        self,
        requested: str | None,
        output: Path,
        timeout: int,
    ) -> CommandResult:
        """Authenticate by writing a QR image and waiting for a scan."""

        if timeout < 1:
            raise ClitgError(ErrorCode.INVALID_INPUT, "QR timeout must be positive")
        if output.exists():
            raise ClitgError(ErrorCode.CONFLICT, f"QR output already exists: {output}")
        profile = self.profile(requested)
        return CommandResult(
            data=await self.telegram_call(self.telegram.qr_login(profile, output, timeout))
        )

    async def logout(
        self, requested: str | None, *, dry_run: bool, confirmation: str | None
    ) -> CommandResult:
        """Revoke the current session."""

        profile = self.profile(requested)
        preview = {"action": "auth.logout", "profile": profile.name}
        if dry_run:
            return CommandResult(data={**preview, "dry_run": True})
        self._require_confirmation("auth.logout", confirmation)
        revoked = await self.telegram_call(self.telegram.logout(profile))
        return CommandResult(data={**preview, "revoked": revoked})

    async def dialogs(
        self,
        requested: str | None,
        *,
        query: str | None,
        cursor: str | None,
        limit: int,
        include_raw: bool,
    ) -> CommandResult:
        """List or search dialogs."""

        self._limit(limit)
        profile = self.profile(requested)
        self._authorize(profile, "dialogs.search" if query else "dialogs.list", "read")
        offset = int(self._cursor(cursor).get("offset", 0))
        items = await self.telegram_call(
            self.telegram.dialogs(
                profile,
                query=query,
                offset=offset,
                limit=limit,
                include_raw=include_raw,
            )
        )
        next_cursor = (
            encode_cursor({"offset": offset + len(items)}) if len(items) == limit else None
        )
        return CommandResult(data={"items": items}, items=items, next_cursor=next_cursor)

    async def peer(
        self, requested: str | None, reference: str, *, include_raw: bool
    ) -> CommandResult:
        """Resolve a peer."""

        profile = self.profile(requested)
        self._authorize(profile, "dialogs.get", "read", reference)
        return CommandResult(
            data=await self.telegram_call(
                self.telegram.peer(profile, reference, include_raw=include_raw)
            )
        )

    async def contacts(self, requested: str | None, query: str | None) -> CommandResult:
        """List or search contacts locally."""

        profile = self.profile(requested)
        self._authorize(profile, "contacts.search" if query else "contacts.list", "read")
        items = await self.telegram_call(self.telegram.contacts(profile))
        if query:
            folded = query.casefold()
            items = [
                item
                for item in items
                if folded in str(item.get("title") or "").casefold()
                or folded in str(item.get("username") or "").casefold()
                or folded in str(item.get("phone") or "").casefold()
            ]
        return CommandResult(data={"items": items}, items=items)

    async def messages(
        self,
        requested: str | None,
        peer: str | None,
        *,
        query: str | None,
        cursor: str | None,
        limit: int,
        topic_id: int | None,
        include_raw: bool,
        sender: str | None = None,
        after: datetime | None = None,
        before: datetime | None = None,
        media_only: bool = False,
    ) -> CommandResult:
        """List or search a peer's messages."""

        self._limit(limit)
        if after is not None and before is not None and after >= before:
            raise ClitgError(ErrorCode.INVALID_INPUT, "--after must be earlier than --before")
        profile = self.profile(requested)
        self._authorize(profile, "messages.search" if query else "messages.list", "read", peer)
        offset_id = int(self._cursor(cursor).get("offset_id", 0))
        if peer is None:
            if not query:
                raise ClitgError(ErrorCode.INVALID_INPUT, "Global search requires a query")
            if topic_id is not None:
                raise ClitgError(ErrorCode.INVALID_INPUT, "Global search does not support topics")
            items = await self.telegram_call(
                self.telegram.global_search(
                    profile,
                    query=query,
                    limit=limit,
                    offset_id=offset_id,
                    sender=sender,
                    after=after,
                    before=before,
                    media_only=media_only,
                    include_raw=include_raw,
                )
            )
        else:
            items = await self.telegram_call(
                self.telegram.messages(
                    profile,
                    peer,
                    limit=limit,
                    offset_id=offset_id,
                    query=query,
                    topic_id=topic_id,
                    sender=sender,
                    after=after,
                    before=before,
                    media_only=media_only,
                    include_raw=include_raw,
                )
            )
        next_cursor = (
            encode_cursor({"offset_id": int(items[-1]["id"])}) if len(items) == limit else None
        )
        return CommandResult(data={"items": items}, items=items, next_cursor=next_cursor)

    async def inbox(
        self,
        requested: str | None,
        *,
        view: str,
        include_archived: bool,
        cursor: str | None,
        limit: int,
        peer: str | None = None,
        sender: str | None = None,
        folder_id: int | None = None,
        after: datetime | None = None,
        before: datetime | None = None,
        media_only: bool = False,
    ) -> CommandResult:
        """List unread messages or unread dialog summaries."""

        if view not in {"messages", "dialogs"}:
            raise ClitgError(ErrorCode.INVALID_INPUT, "Inbox view must be messages or dialogs")
        if folder_id is not None and folder_id < 0:
            raise ClitgError(ErrorCode.INVALID_INPUT, "Folder ID cannot be negative")
        if after is not None and before is not None and after >= before:
            raise ClitgError(ErrorCode.INVALID_INPUT, "--after must be earlier than --before")
        self._limit(limit)
        offset = int(self._cursor(cursor).get("offset", 0))
        profile = self.profile(requested)
        self._authorize(profile, "inbox.list", "read")
        items = await self.telegram_call(
            self.telegram.inbox(
                profile,
                view=view,
                include_archived=include_archived,
                limit=limit,
                offset=offset,
                peer=peer,
                sender=sender,
                folder_id=folder_id,
                after=after,
                before=before,
                media_only=media_only,
            )
        )
        next_cursor = (
            encode_cursor({"offset": offset + len(items)}) if len(items) == limit else None
        )
        return CommandResult(
            data={"view": view, "items": items}, items=items, next_cursor=next_cursor
        )

    async def message_context(
        self,
        requested: str | None,
        peer: str,
        message_id: int,
        *,
        before: int,
        after: int,
        include_raw: bool,
    ) -> CommandResult:
        """Return messages around one anchor without changing read state."""

        if not 0 <= before <= 100 or not 0 <= after <= 100:
            raise ClitgError(ErrorCode.INVALID_INPUT, "Context windows must be between 0 and 100")
        profile = self.profile(requested)
        self._authorize(profile, "messages.context", "read", peer)
        items = await self.telegram_call(
            self.telegram.message_context(
                profile,
                peer,
                message_id,
                before=before,
                after=after,
                include_raw=include_raw,
            )
        )
        return CommandResult(data={"anchor_id": message_id, "items": items}, items=items)

    async def message_replies(
        self,
        requested: str | None,
        peer: str,
        message_id: int,
        *,
        cursor: str | None,
        limit: int,
        include_raw: bool,
    ) -> CommandResult:
        """List paginated replies or comments."""

        self._limit(limit)
        offset_id = int(self._cursor(cursor).get("offset_id", 0))
        profile = self.profile(requested)
        self._authorize(profile, "messages.replies", "read", peer)
        items = await self.telegram_call(
            self.telegram.message_replies(
                profile,
                peer,
                message_id,
                limit=limit,
                offset_id=offset_id,
                include_raw=include_raw,
            )
        )
        next_cursor = (
            encode_cursor({"offset_id": int(items[-1]["id"])}) if len(items) == limit else None
        )
        return CommandResult(data={"items": items}, items=items, next_cursor=next_cursor)

    async def export_conversation(
        self,
        requested: str | None,
        peer: str,
        output: Path,
        *,
        limit: int,
        resume: bool,
        download_media: bool,
    ) -> CommandResult:
        """Append one resumable history page to a JSONL export."""

        self._limit(limit)
        profile = self.profile(requested)
        self._authorize(profile, "messages.export", "read", peer)
        manifest_path = output / "manifest.json"
        messages_path = output / "messages.jsonl"
        if output.exists() and not resume:
            raise ClitgError(ErrorCode.CONFLICT, f"Export output already exists: {output}")
        if resume and not manifest_path.is_file():
            raise ClitgError(ErrorCode.INVALID_INPUT, "Export manifest was not found")
        cursor: str | None = None
        if resume:
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                cursor = manifest.get("next_cursor")
            except (OSError, json.JSONDecodeError, AttributeError) as exc:
                raise ClitgError(ErrorCode.INVALID_INPUT, "Export manifest is invalid") from exc
        output.mkdir(parents=True, exist_ok=True)
        result = await self.messages(
            requested,
            peer,
            query=None,
            cursor=cursor,
            limit=limit,
            topic_id=None,
            include_raw=False,
        )
        items = list(result.items or [])
        media_dir = output / "media"
        if download_media:
            media_dir.mkdir(exist_ok=True)
            for item in items:
                if item.get("has_media"):
                    target = media_dir / str(item["id"])
                    try:
                        path = await self.telegram_call(
                            self.telegram.download(profile, peer, int(item["id"]), target)
                        )
                        item["exported_media"] = str(path.relative_to(output))
                    except ClitgError as exc:
                        item["media_error"] = exc.info
        with messages_path.open("a", encoding="utf-8") as stream:
            for item in items:
                stream.write(json_dumps(item) + "\n")
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "peer": peer,
            "next_cursor": result.next_cursor,
            "complete": result.next_cursor is None,
            "download_media": download_media,
        }
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        return CommandResult(
            data={
                "output": str(output.resolve()),
                "count": len(items),
                "next_cursor": result.next_cursor,
                "complete": result.next_cursor is None,
            },
            items=items,
            next_cursor=result.next_cursor,
        )

    async def watch_updates(
        self,
        requested: str | None,
        *,
        event_types: set[str],
        peers: set[str],
        cursor: str | None,
        consumer_id: str | None,
        max_events: int | None,
        idle_timeout: float | None,
        total_timeout: float | None,
        heartbeat: float | None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream updates and persist optional consumer checkpoints."""

        profile = self.profile(requested)
        self._authorize(profile, "updates.watch", "read")
        if max_events is not None and max_events < 1:
            raise ClitgError(ErrorCode.INVALID_INPUT, "max_events must be positive")
        for value, label in (
            (idle_timeout, "idle_timeout"),
            (total_timeout, "total_timeout"),
            (heartbeat, "heartbeat"),
        ):
            if value is not None and value <= 0:
                raise ClitgError(ErrorCode.INVALID_INPUT, f"{label} must be positive")
        selected_cursor = cursor
        if selected_cursor is None and consumer_id:
            selected_cursor = self.state.get_checkpoint(profile.name, consumer_id)
        state = self._cursor(selected_cursor)
        sequence = int(state.get("sequence", 0))
        resolved_peers: set[str] = set()
        for peer in peers:
            value = await self.telegram_call(self.telegram.peer(profile, peer, include_raw=False))
            resolved_peers.add(str(value["id"]))
        async for item in self.telegram.watch_updates(
            profile,
            event_types=event_types,
            peers=resolved_peers,
            max_events=max_events,
            idle_timeout=idle_timeout,
            total_timeout=total_timeout,
            heartbeat=heartbeat,
        ):
            sequence += 1
            next_cursor = encode_cursor(
                {"stream": "updates", "sequence": sequence, "event_id": item["event_id"]}
            )
            item["cursor"] = next_cursor
            if consumer_id:
                self.state.save_checkpoint(profile.name, consumer_id, next_cursor)
            yield item

    async def get_message(
        self,
        requested: str | None,
        peer: str,
        message_id: int,
        *,
        include_raw: bool,
    ) -> CommandResult:
        """Get one message."""

        profile = self.profile(requested)
        self._authorize(profile, "messages.get", "read", peer)
        return CommandResult(
            data=await self.telegram_call(
                self.telegram.get_message(
                    profile,
                    peer,
                    message_id,
                    include_raw=include_raw,
                )
            )
        )

    async def send(
        self,
        requested: str | None,
        peer: str,
        *,
        text: str,
        files: list[Path],
        reply_to: int | None,
        topic_id: int | None,
        parse_mode: str,
        media_kind: str,
        schedule_at: datetime | None,
        repeat: str | None = None,
        idempotency_key: str | None,
        dry_run: bool,
    ) -> CommandResult:
        """Send, reply, schedule, or upload messages safely."""

        if not text and not files:
            raise ClitgError(ErrorCode.INVALID_INPUT, "Text or at least one file is required")
        missing = [str(path) for path in files if not path.is_file()]
        if missing:
            raise ClitgError(
                ErrorCode.INVALID_INPUT, "Upload files do not exist", details={"files": missing}
            )
        repeat_seconds = repeat_period(repeat, schedule_at)
        if repeat_seconds is not None and len(files) > 1:
            raise ClitgError(
                ErrorCode.INVALID_INPUT,
                "Repeating messages support at most one file",
            )
        profile = self.profile(requested)
        self._authorize(profile, "messages.send", "write", peer)
        payload = {
            "peer": peer,
            "text": text,
            "files": [str(path.resolve()) for path in files],
            "reply_to": reply_to,
            "topic_id": topic_id,
            "parse_mode": parse_mode,
            "media_kind": media_kind,
            "schedule_at": schedule_at,
            "repeat": repeat,
            "repeat_seconds": repeat_seconds,
        }
        if dry_run:
            resolved = await self.telegram_call(
                self.telegram.peer(profile, peer, include_raw=False)
            )
            return CommandResult(
                data={
                    "action": "messages.send",
                    "payload": payload,
                    "peer": resolved,
                    "dry_run": True,
                }
            )
        if idempotency_key:
            existing = self.state.get_idempotent(
                profile.name, "messages.send", idempotency_key, payload
            )
            if existing is not None:
                return CommandResult(data={"messages": existing, "idempotent_replay": True})
        result = await self.telegram_call(
            self.telegram.send(
                profile,
                peer,
                text=text,
                files=files,
                reply_to=reply_to,
                topic_id=topic_id,
                parse_mode=parse_mode,
                media_kind=media_kind,
                schedule_at=schedule_at,
                repeat_period=repeat_seconds,
            )
        )
        if idempotency_key:
            self.state.save_idempotent(
                profile.name,
                "messages.send",
                idempotency_key,
                payload,
                result,
            )
        return CommandResult(data={"messages": result, "idempotent_replay": False})

    async def forward(
        self,
        requested: str | None,
        source_peer: str,
        target_peer: str,
        message_ids: list[int],
        *,
        schedule_at: datetime | None = None,
        repeat: str | None = None,
        idempotency_key: str | None,
        dry_run: bool,
    ) -> CommandResult:
        """Forward messages with optional idempotency."""

        if not message_ids:
            raise ClitgError(ErrorCode.INVALID_INPUT, "At least one message ID is required")
        repeat_seconds = repeat_period(repeat, schedule_at)
        if repeat_seconds is not None and len(message_ids) != 1:
            raise ClitgError(
                ErrorCode.INVALID_INPUT,
                "Repeating forwards require exactly one message ID",
            )
        profile = self.profile(requested)
        self._authorize(profile, "messages.forward", "write", target_peer)
        payload = {
            "source_peer": source_peer,
            "target_peer": target_peer,
            "message_ids": message_ids,
            "schedule_at": schedule_at,
            "repeat": repeat,
            "repeat_seconds": repeat_seconds,
        }
        if dry_run:
            source = await self.telegram_call(
                self.telegram.peer(profile, source_peer, include_raw=False)
            )
            target = await self.telegram_call(
                self.telegram.peer(profile, target_peer, include_raw=False)
            )
            return CommandResult(
                data={
                    "action": "messages.forward",
                    "payload": payload,
                    "source": source,
                    "target": target,
                    "dry_run": True,
                }
            )
        if idempotency_key:
            existing = self.state.get_idempotent(
                profile.name, "messages.forward", idempotency_key, payload
            )
            if existing is not None:
                return CommandResult(data={"messages": existing, "idempotent_replay": True})
        result = await self.telegram_call(
            self.telegram.forward(
                profile,
                source_peer,
                target_peer,
                message_ids,
                schedule_at=schedule_at,
                repeat_period=repeat_seconds,
            )
        )
        if idempotency_key:
            self.state.save_idempotent(
                profile.name, "messages.forward", idempotency_key, payload, result
            )
        return CommandResult(data={"messages": result, "idempotent_replay": False})

    async def edit_message(
        self,
        requested: str | None,
        peer: str,
        message_id: int,
        text: str,
        parse_mode: str,
        *,
        schedule_at: datetime | None = None,
        repeat: str | None = None,
        dry_run: bool,
        idempotency_key: str | None = None,
    ) -> CommandResult:
        """Preview or edit one message."""

        profile = self.profile(requested)
        self._authorize(profile, "messages.edit", "write", peer)
        repeat_seconds = repeat_period(repeat, schedule_at)
        payload = {
            "peer": peer,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
            "schedule_at": schedule_at,
            "repeat": repeat,
            "repeat_seconds": repeat_seconds,
        }
        if dry_run:
            resolved = await self.telegram_call(
                self.telegram.peer(profile, peer, include_raw=False)
            )
            return CommandResult(
                data={
                    "action": "messages.edit",
                    "payload": payload,
                    "peer": resolved,
                    "dry_run": True,
                }
            )
        result, replay = await self._idempotent_call(
            profile,
            "messages.edit",
            idempotency_key,
            payload,
            lambda: self.telegram.edit(
                profile,
                peer,
                message_id,
                text,
                parse_mode,
                schedule_at=schedule_at,
                repeat_period=repeat_seconds,
            ),
        )
        return CommandResult(data=self._mutation_data(result, replay))

    async def delete_messages(
        self,
        requested: str | None,
        peer: str,
        message_ids: list[int],
        scope: str,
        *,
        dry_run: bool,
        confirmation: str | None,
        idempotency_key: str | None = None,
    ) -> CommandResult:
        """Preview or delete an exact message set."""

        if not message_ids:
            raise ClitgError(ErrorCode.INVALID_INPUT, "At least one message ID is required")
        if scope not in {"self", "everyone"}:
            raise ClitgError(ErrorCode.INVALID_INPUT, "Scope must be 'self' or 'everyone'")
        profile = self.profile(requested)
        self._authorize(profile, "messages.delete", "destructive", peer)
        action = "messages.delete"
        payload = {"peer": peer, "message_ids": message_ids, "scope": scope}
        if dry_run:
            resolved = await self.telegram_call(
                self.telegram.peer(profile, peer, include_raw=False)
            )
            return CommandResult(
                data={"action": action, "payload": payload, "peer": resolved, "dry_run": True}
            )
        self._require_confirmation(action, confirmation)
        result, replay = await self._idempotent_call(
            profile,
            action,
            idempotency_key,
            payload,
            lambda: self.telegram.delete(
                profile,
                peer,
                message_ids,
                everyone=scope == "everyone",
            ),
        )
        return CommandResult(data=self._mutation_data(result, replay))

    async def read_messages(
        self,
        requested: str | None,
        peer: str,
        max_id: int | None,
        *,
        dry_run: bool,
        idempotency_key: str | None = None,
    ) -> CommandResult:
        """Preview or explicitly acknowledge messages as read."""

        profile = self.profile(requested)
        self._authorize(profile, "messages.read", "write", peer)
        payload = {"peer": peer, "max_id": max_id}
        if dry_run:
            return CommandResult(
                data={"action": "messages.read", "payload": payload, "dry_run": True}
            )
        result, replay = await self._idempotent_call(
            profile,
            "messages.read",
            idempotency_key,
            payload,
            lambda: self.telegram.read(profile, peer, max_id),
        )
        return CommandResult(data=self._mutation_data(result, replay))

    async def react_message(
        self,
        requested: str | None,
        peer: str,
        message_id: int,
        reaction: str | None,
        *,
        dry_run: bool,
        idempotency_key: str | None = None,
    ) -> CommandResult:
        """Preview or update a message reaction."""

        profile = self.profile(requested)
        self._authorize(profile, "messages.react", "write", peer)
        payload = {"peer": peer, "message_id": message_id, "reaction": reaction}
        if dry_run:
            return CommandResult(
                data={"action": "messages.react", "payload": payload, "dry_run": True}
            )
        result, replay = await self._idempotent_call(
            profile,
            "messages.react",
            idempotency_key,
            payload,
            lambda: self.telegram.react(profile, peer, message_id, reaction),
        )
        return CommandResult(data=self._mutation_data(result, replay))

    async def pin_message(
        self,
        requested: str | None,
        peer: str,
        message_id: int,
        *,
        unpin: bool,
        dry_run: bool,
        idempotency_key: str | None = None,
    ) -> CommandResult:
        """Preview or update a pinned message."""

        profile = self.profile(requested)
        self._authorize(profile, "messages.unpin" if unpin else "messages.pin", "write", peer)
        action = "messages.unpin" if unpin else "messages.pin"
        payload = {"peer": peer, "message_id": message_id}
        if dry_run:
            return CommandResult(data={"action": action, "payload": payload, "dry_run": True})
        result, replay = await self._idempotent_call(
            profile,
            action,
            idempotency_key,
            payload,
            lambda: self.telegram.pin(profile, peer, message_id, unpin=unpin),
        )
        return CommandResult(data=self._mutation_data(result, replay))

    async def scheduled_messages(self, requested: str | None, peer: str) -> CommandResult:
        """List scheduled messages."""

        profile = self.profile(requested)
        self._authorize(profile, "scheduled.list", "read", peer)
        items = await self.telegram_call(self.telegram.scheduled(profile, peer))
        return CommandResult(data={"items": items}, items=items)

    async def cancel_scheduled(
        self,
        requested: str | None,
        peer: str,
        message_ids: list[int],
        *,
        dry_run: bool,
        confirmation: str | None,
        idempotency_key: str | None = None,
    ) -> CommandResult:
        """Preview or cancel scheduled messages."""

        if not message_ids:
            raise ClitgError(ErrorCode.INVALID_INPUT, "At least one message ID is required")
        profile = self.profile(requested)
        self._authorize(profile, "scheduled.cancel", "destructive", peer)
        payload = {"peer": peer, "message_ids": message_ids}
        if dry_run:
            return CommandResult(
                data={"action": "scheduled.cancel", "payload": payload, "dry_run": True}
            )
        self._require_confirmation("scheduled.cancel", confirmation)
        result, replay = await self._idempotent_call(
            profile,
            "scheduled.cancel",
            idempotency_key,
            payload,
            lambda: self.telegram.cancel_scheduled(profile, peer, message_ids),
        )
        return CommandResult(data=self._mutation_data(result, replay))

    async def topics(self, requested: str | None, peer: str, limit: int) -> CommandResult:
        """List forum topics."""

        self._limit(limit)
        profile = self.profile(requested)
        self._authorize(profile, "topics.list", "read", peer)
        result = await self.telegram_call(self.telegram.topics(profile, peer, limit=limit))
        items = result.get("topics", []) if isinstance(result, dict) else []
        return CommandResult(data=result, items=items)

    async def create_poll(
        self,
        requested: str | None,
        peer: str,
        question: str,
        answers: list[str],
        *,
        multiple_choice: bool,
        anonymous: bool,
        quiz: bool,
        dry_run: bool,
        idempotency_key: str | None = None,
    ) -> CommandResult:
        """Preview or create a poll."""

        if len(answers) < 2:
            raise ClitgError(ErrorCode.INVALID_INPUT, "A poll requires at least two answers")
        if len(answers) > 10:
            raise ClitgError(ErrorCode.INVALID_INPUT, "A poll supports at most ten answers")
        profile = self.profile(requested)
        self._authorize(profile, "polls.create", "write", peer)
        payload = {
            "peer": peer,
            "question": question,
            "answers": answers,
            "multiple_choice": multiple_choice,
            "anonymous": anonymous,
            "quiz": quiz,
        }
        if dry_run:
            return CommandResult(
                data={"action": "polls.create", "payload": payload, "dry_run": True}
            )
        result, replay = await self._idempotent_call(
            profile,
            "polls.create",
            idempotency_key,
            payload,
            lambda: self.telegram.create_poll(
                profile,
                peer,
                question,
                answers,
                multiple_choice=multiple_choice,
                anonymous=anonymous,
                quiz=quiz,
            ),
        )
        return CommandResult(data={"messages": result, "idempotent_replay": replay})

    async def vote_poll(
        self,
        requested: str | None,
        peer: str,
        message_id: int,
        options: list[int],
        *,
        dry_run: bool,
        idempotency_key: str | None = None,
    ) -> CommandResult:
        """Preview or vote in a poll."""

        if not options or any(option < 0 or option > 9 for option in options):
            raise ClitgError(ErrorCode.INVALID_INPUT, "Poll options must be indexes from 0 to 9")
        profile = self.profile(requested)
        self._authorize(profile, "polls.vote", "write", peer)
        payload = {"peer": peer, "message_id": message_id, "options": options}
        if dry_run:
            return CommandResult(data={"action": "polls.vote", "payload": payload, "dry_run": True})
        result, replay = await self._idempotent_call(
            profile,
            "polls.vote",
            idempotency_key,
            payload,
            lambda: self.telegram.vote_poll(profile, peer, message_id, options),
        )
        return CommandResult(data=self._mutation_data(result, replay))

    async def close_poll(
        self,
        requested: str | None,
        peer: str,
        message_id: int,
        *,
        dry_run: bool,
        confirmation: str | None,
        idempotency_key: str | None = None,
    ) -> CommandResult:
        """Preview or close a poll."""

        profile = self.profile(requested)
        self._authorize(profile, "polls.close", "destructive", peer)
        payload = {"peer": peer, "message_id": message_id}
        if dry_run:
            return CommandResult(
                data={"action": "polls.close", "payload": payload, "dry_run": True}
            )
        self._require_confirmation("polls.close", confirmation)
        result, replay = await self._idempotent_call(
            profile,
            "polls.close",
            idempotency_key,
            payload,
            lambda: self.telegram.close_poll(profile, peer, message_id),
        )
        return CommandResult(data=self._mutation_data(result, replay))

    async def raw(
        self,
        requested: str | None,
        method: str,
        params: dict[str, Any],
        *,
        allow_raw: bool,
        dry_run: bool,
        confirmation: str | None,
        confirmation_token: str | None,
    ) -> CommandResult:
        """Validate, preview, authorize, and invoke a raw TL method."""

        if not allow_raw:
            raise ClitgError(ErrorCode.CONFIRMATION_REQUIRED, "Raw invocation requires --allow-raw")
        profile = self.profile(requested)
        await self.telegram.codec.build(method, params, resolve=False)
        risk = self.telegram.codec.risk(method)
        require_policy(
            evaluate_policy(
                self._policy(profile),
                "raw.invoke",
                risk=risk,
                raw_method=method,
            )
        )
        payload = {"method": method, "params": params}
        critical = risk in {"critical", "unknown"}
        if dry_run:
            data: dict[str, Any] = {
                "action": "raw.invoke",
                "risk": risk,
                "payload": payload,
                "dry_run": True,
            }
            if critical:
                issued = self.state.issue_confirmation(profile.name, method, params)
                data["confirmation_token"] = issued.token
                data["confirmation_expires_at"] = issued.expires_at
            return CommandResult(data=data)
        if critical:
            if not confirmation_token:
                raise ClitgError(
                    ErrorCode.CONFIRMATION_REQUIRED, "A critical confirmation token is required"
                )
            self.state.consume_confirmation(confirmation_token, profile.name, method, params)
        elif risk == "destructive":
            self._require_confirmation(method, confirmation)
        return CommandResult(
            data={
                "method": method,
                "risk": risk,
                "result": await self.telegram_call(
                    self.telegram.raw_invoke(profile, method, params)
                ),
            }
        )

    async def execute_operation(
        self,
        requested: str | None,
        command: str,
        params: dict[str, Any],
        *,
        dry_run: bool,
        confirmation: str | None,
        confirmation_token: str | None,
        idempotency_key: str | None,
    ) -> CommandResult:
        """Execute one registered operation with uniform agent safeguards."""

        operation = OPERATION_BY_COMMAND.get(command)
        if operation is None:
            raise ClitgError(ErrorCode.NOT_FOUND, f"Operation '{command}' was not found")
        profile = self.profile(requested)
        selected = self._role_params(operation, params)
        normalized = normalize_params(selected)
        await self.telegram.codec.build(operation.method, normalized, resolve=False)
        target = self._operation_target(selected)
        require_policy(
            evaluate_policy(
                self._policy(profile),
                operation.command,
                risk=operation.risk,
                peer=target,
            )
        )
        payload = {"command": command, "method": operation.method, "params": selected}
        if dry_run:
            data: dict[str, Any] = {
                "action": command,
                "method": operation.method,
                "risk": operation.risk,
                "payload": payload,
                "dry_run": True,
            }
            if operation.critical:
                issued = self.state.issue_confirmation(profile.name, command, payload)
                data["confirmation_token"] = issued.token
                data["confirmation_expires_at"] = issued.expires_at
            return CommandResult(data=data)
        if not operation.mutation and idempotency_key:
            raise ClitgError(ErrorCode.INVALID_INPUT, "Read operations do not use idempotency keys")
        if operation.mutation and idempotency_key:
            existing = self.state.get_idempotent(profile.name, command, idempotency_key, payload)
            if existing is not None:
                return CommandResult(data={"result": existing, "idempotent_replay": True})
        if operation.critical:
            self._require_confirmation(command, confirmation)
            if not confirmation_token:
                raise ClitgError(
                    ErrorCode.CONFIRMATION_REQUIRED,
                    "A critical confirmation token is required",
                )
            self.state.consume_confirmation(confirmation_token, profile.name, command, payload)
        elif operation.risk == "destructive":
            self._require_confirmation(command, confirmation)
        result = await self.telegram_call(
            self.telegram.raw_invoke(profile, operation.method, normalized)
        )
        if operation.mutation and idempotency_key:
            self.state.save_idempotent(profile.name, command, idempotency_key, payload, result)
        return CommandResult(
            data={
                "command": command,
                "method": operation.method,
                "risk": operation.risk,
                "result": result,
                "idempotent_replay": False,
            }
        )

    async def execute_feature(
        self,
        requested: str | None,
        command: str,
        values: dict[str, Any],
        structured: dict[str, Any] | list[Any] | None,
        *,
        dry_run: bool,
        confirmation: str | None,
        confirmation_token: str | None,
        idempotency_key: str | None,
        include_raw: bool,
    ) -> CommandResult:
        """Execute one stable high-level Telegram feature."""

        feature = FEATURE_BY_COMMAND.get(command)
        if feature is None:
            raise ClitgError(ErrorCode.NOT_FOUND, f"Feature '{command}' was not found")
        profile = self.profile(requested)
        effective_values = feature_cursor_values(feature, command, values)
        params = build_feature_params(feature, effective_values, structured)
        await self.telegram.validate_feature(feature, params)
        target = self._operation_target(values)
        require_policy(
            evaluate_policy(
                self._policy(profile),
                command,
                risk=feature.risk,
                peer=target,
            )
        )
        payload = {
            "command": command,
            "method": feature.method,
            "values": values,
            "structured": structured,
        }
        if dry_run:
            data: dict[str, Any] = {
                "action": command,
                "method": feature.method,
                "risk": feature.risk,
                "requirements": list(feature.requirements),
                "quota_consuming": feature.quota_consuming,
                "payload": payload,
                "dry_run": True,
            }
            if feature.critical:
                issued = self.state.issue_confirmation(profile.name, command, payload)
                data["confirmation_token"] = issued.token
                data["confirmation_expires_at"] = issued.expires_at
            return CommandResult(data=data)
        if not feature.mutation and idempotency_key:
            raise ClitgError(ErrorCode.INVALID_INPUT, "Read features do not use idempotency keys")
        raw_result: Any | None = None
        replay = False
        if feature.mutation and idempotency_key:
            raw_result = self.state.get_idempotent(
                profile.name,
                command,
                idempotency_key,
                payload,
            )
            replay = raw_result is not None
        if not replay:
            if feature.critical:
                self._require_confirmation(command, confirmation)
                if not confirmation_token:
                    raise ClitgError(
                        ErrorCode.CONFIRMATION_REQUIRED,
                        "A critical confirmation token is required",
                    )
                self.state.consume_confirmation(
                    confirmation_token,
                    profile.name,
                    command,
                    payload,
                )
            elif feature.risk == "destructive":
                self._require_confirmation(command, confirmation)
            raw_result = await self.telegram_call(
                self.telegram.feature_invoke(profile, feature, params, values=effective_values)
            )
            if feature.mutation and idempotency_key:
                self.state.save_idempotent(
                    profile.name,
                    command,
                    idempotency_key,
                    payload,
                    raw_result,
                )
        result_type, normalized = normalize_feature_result(raw_result)
        result = FeatureResult(
            command=command,
            method=feature.method,
            risk=feature.risk,
            result_type=result_type,
            result=normalized,
            requirements=list(feature.requirements),
            quota_consuming=feature.quota_consuming,
            idempotent_replay=replay,
            raw=raw_result if include_raw else None,
        ).model_dump(mode="python")
        if not include_raw:
            result.pop("raw")
        return CommandResult(
            data=result,
            next_cursor=feature_next_cursor(
                feature,
                command,
                effective_values,
                raw_result,
            ),
        )

    async def batch(
        self,
        requested: str | None,
        operations: list[BatchOperation],
        *,
        concurrency: int,
        fail_fast: bool,
    ) -> CommandResult:
        """Execute a bounded batch containing registered reads only."""

        if not 1 <= concurrency <= 10:
            raise ClitgError(ErrorCode.INVALID_INPUT, "Concurrency must be between 1 and 10")
        profile = self.profile(requested)
        policy = self._policy(profile)
        if policy and len(operations) > policy.max_operations:
            raise ClitgError(ErrorCode.PERMISSION_DENIED, "Batch exceeds policy max_operations")
        targets = {
            target
            for item in operations
            if (target := self._operation_target(item.params)) is not None
        }
        if policy and len(targets) > policy.max_targets:
            raise ClitgError(ErrorCode.PERMISSION_DENIED, "Batch exceeds policy max_targets")
        for item in operations:
            operation = OPERATION_BY_COMMAND.get(item.command)
            if operation is None or operation.mutation:
                raise ClitgError(
                    ErrorCode.INVALID_INPUT,
                    f"Batch operation '{item.command}' is not a registered read",
                )

        async def run(item: BatchOperation) -> dict[str, Any]:
            try:
                result = await self.execute_operation(
                    requested,
                    item.command,
                    item.params,
                    dry_run=False,
                    confirmation=None,
                    confirmation_token=None,
                    idempotency_key=None,
                )
                return {"id": item.id, "ok": True, "data": result.data, "error": None}
            except ClitgError as exc:
                return {"id": item.id, "ok": False, "data": None, "error": exc.info}

        results: list[dict[str, Any]] = []
        if fail_fast:
            for item in operations:
                result = await run(item)
                results.append(result)
                if not result["ok"]:
                    break
        else:
            semaphore = asyncio.Semaphore(concurrency)

            async def bounded(item: BatchOperation) -> dict[str, Any]:
                async with semaphore:
                    return await run(item)

            results = list(await asyncio.gather(*(bounded(item) for item in operations)))
        return CommandResult(
            data={
                "items": results,
                "count": len(results),
                "succeeded": sum(bool(item["ok"]) for item in results),
                "failed": sum(not bool(item["ok"]) for item in results),
            },
            items=results,
        )

    def set_policy(self, name: str, path: Path | None) -> CommandResult:
        """Attach a validated policy document to one profile."""

        resolved: str | None = None
        if path is not None:
            load_policy(path)
            resolved = str(path.resolve())
        return CommandResult(data=self.profiles.set_policy_file(name, resolved))

    def inspect_policy(self, requested: str | None) -> CommandResult:
        """Return the selected policy without credential material."""

        profile = self.profile(requested)
        policy = self._policy(profile)
        return CommandResult(
            data={"profile": profile.name, "policy_file": profile.policy_file, "policy": policy}
        )

    def validate_policy(self, path: Path) -> CommandResult:
        """Validate a policy without attaching it."""

        return CommandResult(data={"valid": True, "policy": load_policy(path)})

    def explain_policy(
        self,
        requested: str | None,
        command: str,
        risk: str,
        peer: str | None,
        raw_method: str | None,
    ) -> CommandResult:
        """Explain the selected policy decision without executing an operation."""

        profile = self.profile(requested)
        return CommandResult(
            data={
                "profile": profile.name,
                "command": command,
                "risk": risk,
                "peer": peer,
                "raw_method": raw_method,
                "decision": evaluate_policy(
                    self._policy(profile),
                    command,
                    risk=risk,
                    peer=peer,
                    raw_method=raw_method,
                ),
            }
        )

    def audit_records(self, limit: int) -> CommandResult:
        """List recent content-free audit records."""

        self._limit(limit)
        items = self.state.list_audit(limit)
        return CommandResult(data={"items": items}, items=items)

    def export_audit(self, output: Path, *, overwrite: bool) -> CommandResult:
        """Export content-free audit records as JSONL."""

        if output.exists() and not overwrite:
            raise ClitgError(ErrorCode.CONFLICT, f"Output already exists: {output}")
        output.parent.mkdir(parents=True, exist_ok=True)
        rows = self.state.list_audit(None)
        output.write_text("".join(json.dumps(item, separators=(",", ":")) + "\n" for item in rows))
        return CommandResult(data={"path": str(output.resolve()), "count": len(rows)})

    def record_audit(
        self,
        profile: str | None,
        command: str,
        request_id: str,
        *,
        ok: bool,
        error_code: str | None = None,
        target: str | None = None,
    ) -> None:
        """Persist one command audit record."""

        self.state.record_audit(
            profile,
            command,
            request_id,
            target=target,
            ok=ok,
            error_code=error_code,
        )

    @staticmethod
    def _operation_target(params: dict[str, Any]) -> str | None:
        for key in ("peer", "channel", "user", "user_id", "bot", "bot_id"):
            value = params.get(key)
            if isinstance(value, str | int):
                return str(value)
        return None

    async def _idempotent_call(
        self,
        profile: Profile,
        action: str,
        key: str | None,
        payload: dict[str, Any],
        factory: Callable[[], Awaitable[Any]],
    ) -> tuple[Any, bool]:
        """Execute and cache one compatible mutation."""

        if key:
            existing = self.state.get_idempotent(profile.name, action, key, payload)
            if existing is not None:
                return existing, True
        result = await self.telegram_call(factory())
        if key:
            self.state.save_idempotent(profile.name, action, key, payload, result)
        return result, False

    @staticmethod
    def _mutation_data(result: Any, replay: bool) -> dict[str, Any]:
        """Preserve mapping results while exposing uniform replay metadata."""

        if isinstance(result, dict):
            return {**result, "idempotent_replay": replay}
        return {"result": result, "idempotent_replay": replay}

    @staticmethod
    def _role_params(operation: Operation, params: dict[str, Any]) -> dict[str, Any]:
        selected = dict(params)
        role = selected.pop("role", None)
        overrides = selected.pop("rights_overrides", None)
        if role is None:
            return selected
        if operation.command not in {
            "chats.promote-channel",
            "chats.promote-group",
            "chats.restrict",
        }:
            raise ClitgError(ErrorCode.INVALID_INPUT, "Roles are not supported by this operation")
        if role not in {"moderator", "admin", "restricted"}:
            raise ClitgError(ErrorCode.INVALID_INPUT, "Unknown administration role")
        if operation.command == "chats.promote-group":
            selected["is_admin"] = role in {"moderator", "admin"}
            if overrides is not None:
                if not isinstance(overrides, dict):
                    raise ClitgError(ErrorCode.INVALID_INPUT, "rights_overrides must be an object")
                selected.update(overrides)
            return selected
        if operation.command == "chats.promote-channel":
            rights: dict[str, Any] = {
                "_": "ChatAdminRights",
                "delete_messages": True,
                "ban_users": True,
                "invite_users": True,
                "pin_messages": True,
                "manage_call": True,
                "manage_topics": True,
                "add_admins": role == "admin",
            }
            key = "admin_rights"
        else:
            rights = {"_": "ChatBannedRights", "until_date": None, "send_messages": True}
            key = "banned_rights"
        if overrides is not None:
            if not isinstance(overrides, dict):
                raise ClitgError(ErrorCode.INVALID_INPUT, "rights_overrides must be an object")
            rights.update(overrides)
        selected[key] = rights
        return selected

    @staticmethod
    def _policy(profile: Profile) -> PolicyDocument | None:
        return load_policy(profile.policy_file) if profile.policy_file else None

    def _authorize(
        self, profile: Profile, command: str, risk: str, peer: str | None = None
    ) -> None:
        require_policy(evaluate_policy(self._policy(profile), command, risk=risk, peer=peer))

    async def download(
        self,
        requested: str | None,
        peer: str,
        message_id: int,
        output: Path,
        *,
        create_dirs: bool,
        overwrite: bool,
        dry_run: bool,
    ) -> CommandResult:
        """Download media without implicit path creation or overwrite."""

        if output.exists() and not overwrite:
            raise ClitgError(ErrorCode.CONFLICT, f"Output already exists: {output}")
        if not output.parent.exists():
            if not create_dirs:
                raise ClitgError(ErrorCode.INVALID_INPUT, "Output parent does not exist")
            if not dry_run:
                output.parent.mkdir(parents=True)
        profile = self.profile(requested)
        payload = {"peer": peer, "message_id": message_id, "output": str(output)}
        if dry_run:
            return CommandResult(
                data={"action": "media.download", "payload": payload, "dry_run": True}
            )
        path = await self.telegram_call(self.telegram.download(profile, peer, message_id, output))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        mime, _ = mimetypes.guess_type(path.name)
        return CommandResult(
            data={
                "path": str(path.resolve()),
                "size": path.stat().st_size,
                "mime_type": mime,
                "sha256": digest,
            }
        )

    def capabilities(self, method: str | None = None, status: str | None = None) -> CommandResult:
        """Query the generated MTProto capability catalog."""

        catalog = capability_catalog()
        values = catalog.capabilities
        if method:
            match = next((item for item in values if item.method == method), None)
            if match is None:
                raise ClitgError(ErrorCode.NOT_FOUND, f"Capability '{method}' was not found")
            return CommandResult(data=match)
        if status:
            values = [item for item in values if item.status == status]
        return CommandResult(
            data={
                "telethon_version": catalog.telethon_version,
                "telegram_layer": catalog.telegram_layer,
                "items": values,
            },
            items=values,
        )

    def schemas(self, name: str | None = None) -> CommandResult:
        """Query public JSON Schemas and the command catalog."""

        schemas = schema_catalog()
        if name is None:
            names = sorted(schemas["models"])
            return CommandResult(data={"models": names, "commands": command_catalog()})
        schema = schemas["models"].get(name)
        if schema is None:
            raise ClitgError(ErrorCode.NOT_FOUND, f"Schema '{name}' was not found")
        return CommandResult(data={"name": name, "schema": schema})

    def export_schemas(self, output: Path, *, overwrite: bool) -> CommandResult:
        """Export public schema and command catalogs."""

        if output.exists() and not overwrite:
            raise ClitgError(ErrorCode.CONFLICT, f"Output already exists: {output}")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(
                {"commands": command_catalog(), **schema_catalog()}, indent=2, sort_keys=True
            )
            + "\n",
            encoding="utf-8",
        )
        return CommandResult(data={"path": str(output.resolve())})

    @property
    def mcp(self) -> McpStore:
        """Return the MCP authorization store."""

        store = getattr(self, "_mcp_store", None)
        if store is None:
            store = McpStore(self.paths)
            self._mcp_store = store
        return store

    async def mcp_pair(
        self, requested: str | None, *, delivery: str, dry_run: bool
    ) -> CommandResult:
        """Issue a single-use MCP pairing code and deliver it."""

        if delivery not in {"both", "terminal", "saved-messages"}:
            raise ClitgError(
                ErrorCode.INVALID_INPUT, "Delivery must be both, terminal, or saved-messages"
            )
        name = self.profiles.resolve(requested).name
        terminal, saved = pairing_delivery_targets(delivery)
        targets = [*(["terminal"] if terminal else []), *(["saved-messages"] if saved else [])]
        if dry_run:
            return CommandResult(
                data={
                    "action": "mcp.pair",
                    "profile": name,
                    "delivered_to": targets,
                    "dry_run": True,
                }
            )
        code, expires_at = self.mcp.issue_pairing_code(name)
        data: dict[str, Any] = {"profile": name, "expires_at": expires_at, "delivered_to": []}
        if terminal:
            data["code"] = code
            data["delivered_to"].append("terminal")
        if saved:
            try:
                await self.send(
                    requested,
                    "me",
                    text=pairing_message(name, code, expires_at),
                    files=[],
                    reply_to=None,
                    topic_id=None,
                    parse_mode="plain",
                    media_kind="auto",
                    schedule_at=None,
                    idempotency_key=None,
                    dry_run=False,
                )
            except ClitgError as exc:
                if not terminal:
                    raise
                data["saved_messages_error"] = exc.info.code
            else:
                data["delivered_to"].append("saved-messages")
        return CommandResult(data=data)

    def mcp_clients(self, requested: str | None) -> CommandResult:
        """List registered MCP OAuth clients."""

        clients = self.mcp.list_clients(self.profiles.resolve(requested).name)
        return CommandResult(data={"clients": clients}, items=clients)

    def mcp_revoke_client(
        self,
        requested: str | None,
        client_id: str,
        *,
        dry_run: bool,
        confirmation: str | None,
    ) -> CommandResult:
        """Revoke one MCP OAuth client and all its grants."""

        name = self.profiles.resolve(requested).name
        if dry_run:
            known = any(item["client_id"] == client_id for item in self.mcp.list_clients(name))
            if not known:
                raise ClitgError(ErrorCode.NOT_FOUND, "MCP client was not found")
            return CommandResult(
                data={
                    "action": "mcp.clients.revoke",
                    "client_id": client_id,
                    "required_confirmation": "mcp.clients.revoke",
                    "dry_run": True,
                }
            )
        self._require_confirmation("mcp.clients.revoke", confirmation)
        return CommandResult(data={"revoked": self.mcp.revoke_client(name, client_id)})

    def mcp_create_token(
        self,
        requested: str | None,
        token_name: str,
        *,
        expires_days: int | None,
        token_file: Path | None,
        dry_run: bool,
    ) -> CommandResult:
        """Create a named static bearer token for header-based MCP clients."""

        name = self.profiles.resolve(requested).name
        token_name = ProfileStore.validate_name(token_name)
        if expires_days is not None and not 1 <= expires_days <= 3650:
            raise ClitgError(ErrorCode.INVALID_INPUT, "Expiry must be between 1 and 3650 days")
        if token_file is not None and token_file.exists():
            raise ClitgError(ErrorCode.CONFLICT, "Token file already exists")
        if dry_run:
            return CommandResult(
                data={
                    "action": "mcp.tokens.create",
                    "name": token_name,
                    "expires_days": expires_days,
                    "token_file": token_file,
                    "dry_run": True,
                }
            )
        ttl = None if expires_days is None else expires_days * 86400
        token, record = self.mcp.create_static_token(name, token_name, ttl)
        if token_file is None:
            return CommandResult(data={**record, "token": token})
        try:
            descriptor = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(token + "\n")
        except OSError as exc:
            self.mcp.revoke_static_token(name, token_name)
            raise ClitgError(ErrorCode.INVALID_INPUT, "Unable to write the token file") from exc
        return CommandResult(data={**record, "token_file": token_file})

    def mcp_tokens(self, requested: str | None) -> CommandResult:
        """List static MCP bearer tokens without secrets."""

        tokens = self.mcp.list_static_tokens(self.profiles.resolve(requested).name)
        return CommandResult(data={"tokens": tokens}, items=tokens)

    def mcp_revoke_token(
        self,
        requested: str | None,
        token_name: str,
        *,
        dry_run: bool,
        confirmation: str | None,
    ) -> CommandResult:
        """Revoke one named static MCP bearer token."""

        name = self.profiles.resolve(requested).name
        if dry_run:
            active = [
                item
                for item in self.mcp.list_static_tokens(name)
                if item["name"] == token_name and item["active"]
            ]
            if not active:
                raise ClitgError(ErrorCode.NOT_FOUND, f"Token '{token_name}' was not found")
            return CommandResult(
                data={
                    "action": "mcp.tokens.revoke",
                    "name": token_name,
                    "required_confirmation": "mcp.tokens.revoke",
                    "dry_run": True,
                }
            )
        self._require_confirmation("mcp.tokens.revoke", confirmation)
        return CommandResult(data={"revoked": self.mcp.revoke_static_token(name, token_name)})

    def state_counts(self) -> CommandResult:
        """Return safe auxiliary state counts."""

        return CommandResult(data=self.state.counts())

    def prune_state(
        self,
        kind: str,
        before: datetime | None,
        *,
        dry_run: bool,
        confirmation: str | None,
        action: str = "state.prune",
    ) -> CommandResult:
        """Preview or delete auxiliary state."""

        if kind not in {"login", "idempotency", "confirmation", "checkpoint", "audit", "all"}:
            raise ClitgError(ErrorCode.INVALID_INPUT, "Invalid state kind")
        if dry_run:
            return CommandResult(
                data={
                    "action": action,
                    "kind": kind,
                    "before": before,
                    "current": self.state.counts(),
                    "dry_run": True,
                }
            )
        self._require_confirmation(action, confirmation)
        return CommandResult(data={"deleted": self.state.prune(kind, before)})

    @staticmethod
    def version() -> CommandResult:
        """Return runtime and protocol versions."""

        catalog = capability_catalog()
        return CommandResult(
            data={
                "cli_version": __version__,
                "schema_version": SCHEMA_VERSION,
                "telethon_version": catalog.telethon_version,
                "telegram_layer": catalog.telegram_layer,
            }
        )

    @staticmethod
    def _require_confirmation(action: str, supplied: str | None) -> None:
        if supplied != action:
            raise ClitgError(
                ErrorCode.CONFIRMATION_REQUIRED,
                f"Operation requires --confirm {action}",
                details={"expected": action},
            )

    @staticmethod
    def _limit(limit: int) -> None:
        if not 1 <= limit <= 500:
            raise ClitgError(ErrorCode.INVALID_INPUT, "Limit must be between 1 and 500")

    @staticmethod
    def _cursor(cursor: str | None) -> dict[str, Any]:
        try:
            return decode_cursor(cursor)
        except ValueError as exc:
            raise ClitgError(ErrorCode.INVALID_INPUT, str(exc)) from exc
