from __future__ import annotations

import gzip
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from filelock import Timeout as FileLockTimeout
from PIL import Image
from telethon import functions, types
from telethon.errors import SessionPasswordNeededError

from clitg.errors import ClitgError
from clitg.features import FEATURE_BY_COMMAND, build_feature_params
from clitg.models import Profile
from clitg.storage import Paths
from clitg.telegram import TelegramAdapter, dialog_view, entity_view, message_view, update_view


def user(user_id: int = 1, name: str = "Alice") -> types.User:
    return types.User(
        id=user_id,
        first_name=name,
        last_name="User",
        username=name.lower(),
        phone="1555",
        bot=False,
        verified=True,
    )


def message(message_id: int = 1, *, media: Any = None) -> types.Message:
    return types.Message(
        id=message_id,
        peer_id=types.PeerUser(1),
        from_id=types.PeerUser(2),
        date=datetime(2026, 1, 1, tzinfo=UTC),
        message="hello",
        out=True,
        mentioned=True,
        silent=True,
        reply_to=types.MessageReplyHeader(reply_to_msg_id=7),
        grouped_id=8,
        media=media,
        views=9,
        forwards=10,
        edit_date=datetime(2026, 1, 2, tzinfo=UTC),
    )


def test_entity_message_and_dialog_views() -> None:
    assert entity_view(user())["kind"] == "user"
    photo = types.ChatPhotoEmpty()
    channel = types.Channel(
        id=2,
        title="Group",
        photo=photo,
        date=datetime.now(UTC),
        broadcast=False,
    )
    broadcast = types.Channel(
        id=3,
        title="News",
        photo=photo,
        date=datetime.now(UTC),
        broadcast=True,
    )
    chat = types.Chat(
        id=4,
        title="Chat",
        photo=photo,
        participants_count=2,
        date=datetime.now(UTC),
        version=1,
    )
    assert entity_view(channel)["kind"] == "group"
    assert entity_view(broadcast)["kind"] == "channel"
    assert entity_view(chat)["kind"] == "group"
    assert entity_view(SimpleNamespace(id=5, __class__=SimpleNamespace))["kind"]
    normalized = message_view(message(), include_raw=True)
    assert normalized["id"] == "1"
    assert normalized["reply_to_message_id"] == "7"
    assert normalized["raw"]["_"] == "Message"
    assert "raw" not in message_view(message(), include_raw=False)
    dialog = SimpleNamespace(
        entity=user(),
        unread_count=2,
        unread_mentions_count=1,
        pinned=True,
        archived=False,
        folder_id=0,
        message=message(),
    )
    assert dialog_view(dialog)["folder_id"] == 0
    assert dialog_view(dialog, include_raw=True)["last_message"]["id"] == "1"
    dialog.message = None
    assert dialog_view(dialog)["last_message"] is None


class FakeLock:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.released = False

    def acquire(self) -> None:
        if self.fail:
            raise FileLockTimeout("busy")

    def release(self) -> None:
        self.released = True


class FakeClient:
    def __init__(self) -> None:
        self.connected = False
        self.disconnected = False
        self.authorized = True
        self.password_needed = False
        self.dialog_entities: list[Any] = [user()]
        self.entity_error = False
        self.message_result: Any = message()
        self.download_result: str | None = None
        self.calls: list[Any] = []
        self.dialog_unread_count = 0
        self.dialog_archived = False
        self.event_handler: Any = None
        self.pending_updates: list[Any] = []
        self.request_result: Any = None
        self.upload_document: Any = types.Document(
            id=1,
            access_hash=2,
            file_reference=b"ref",
            date=datetime.now(UTC),
            mime_type="image/webp",
            size=10,
            dc_id=1,
            attributes=[],
        )

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnected = True

    async def is_user_authorized(self) -> bool:
        return self.authorized

    async def send_code_request(self, phone: str) -> Any:
        return SimpleNamespace(phone_code_hash=f"hash-{phone}")

    async def sign_in(self, **kwargs: Any) -> types.User:
        self.calls.append(("sign_in", kwargs))
        if self.password_needed and "password" not in kwargs:
            raise SessionPasswordNeededError(request=None)
        return user()

    async def get_me(self) -> types.User:
        return user()

    async def log_out(self) -> bool:
        return True

    async def qr_login(self) -> Any:
        return SimpleNamespace(url="tg://login?token=test", wait=self._qr_wait)

    async def _qr_wait(self, timeout: int) -> None:
        self.calls.append(("qr_wait", timeout))

    async def get_entity(self, reference: Any) -> Any:
        self.calls.append(("get_entity", reference))
        if self.entity_error:
            raise ValueError("missing")
        return user()

    async def get_input_entity(self, reference: Any) -> Any:
        return types.InputPeerSelf()

    async def upload_file(self, path: Path) -> Any:
        return ("upload", str(path))

    async def _parse_message_text(self, text: str, mode: str | None) -> tuple[str, list[Any]]:
        self.calls.append(("parse_message_text", text, mode))
        return text, []

    async def _file_to_media(self, *args: Any, **kwargs: Any) -> tuple[Any, Any, bool]:
        self.calls.append(("file_to_media", args, kwargs))
        return None, types.InputMediaEmpty(), False

    def _get_response_message(self, request: Any, result: Any, peer: Any) -> Any:
        self.calls.append(("get_response_message", request, result, peer))
        return self.message_result

    async def iter_dialogs(self, limit: int | None = None) -> AsyncIterator[Any]:
        for entity in self.dialog_entities[:limit]:
            yield SimpleNamespace(
                entity=entity,
                unread_count=self.dialog_unread_count,
                unread_mentions_count=0,
                pinned=False,
                archived=self.dialog_archived,
                message=message(),
                dialog=SimpleNamespace(read_inbox_max_id=0),
            )

    async def iter_messages(self, *args: Any, **kwargs: Any) -> AsyncIterator[types.Message]:
        self.calls.append(("iter_messages", args, kwargs))
        values = (
            self.message_result if isinstance(self.message_result, list) else [self.message_result]
        )
        for value in values:
            if value is not None:
                yield value

    async def get_messages(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(("get_messages", args, kwargs))
        return self.message_result

    async def send_file(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(("send_file", args, kwargs))
        return self.message_result

    async def send_message(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(("send_message", args, kwargs))
        return self.message_result

    async def forward_messages(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(("forward_messages", args, kwargs))
        return [message(2)]

    async def edit_message(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(("edit_message", args, kwargs))
        return self.message_result

    async def delete_messages(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(("delete_messages", args, kwargs))
        return [types.messages.AffectedMessages(pts=1, pts_count=1)]

    async def send_read_acknowledge(self, *args: Any, **kwargs: Any) -> bool:
        self.calls.append(("read", args, kwargs))
        return True

    async def pin_message(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(("pin", args, kwargs))
        return types.UpdatesTooLong()

    async def unpin_message(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(("unpin", args, kwargs))
        return types.UpdatesTooLong()

    async def download_media(self, *args: Any, **kwargs: Any) -> str | None:
        self.calls.append(("download", args, kwargs))
        return self.download_result

    def add_event_handler(self, handler: Any, event: Any) -> None:
        self.event_handler = handler
        self.calls.append(("add_event_handler", event))

    def remove_event_handler(self, handler: Any, event: Any) -> None:
        assert handler is self.event_handler
        self.calls.append(("remove_event_handler", event))

    async def catch_up(self) -> None:
        if self.event_handler:
            for update in self.pending_updates:
                await self.event_handler(update)

    async def __call__(self, request: Any) -> Any:
        self.calls.append(("request", request))
        if self.request_result is not None:
            if self.event_handler and isinstance(
                self.request_result, types.messages.TranscribedAudio
            ):
                for update in self.pending_updates:
                    await self.event_handler(update)
            return self.request_result
        if isinstance(request, functions.contacts.GetContactsRequest):
            return SimpleNamespace(users=[user()])
        if isinstance(request, functions.messages.UploadMediaRequest):
            return SimpleNamespace(document=self.upload_document)
        if isinstance(
            request,
            (
                functions.stickers.CreateStickerSetRequest,
                functions.stickers.AddStickerToSetRequest,
            ),
        ):
            return types.UpdatesTooLong()
        if isinstance(request, functions.messages.GetScheduledHistoryRequest):
            return SimpleNamespace(messages=[message()])
        if isinstance(request, functions.messages.GetForumTopicsRequest):
            return types.messages.ForumTopics(
                count=0, topics=[], messages=[], chats=[], users=[], pts=1
            )
        if isinstance(request, functions.messages.SendReactionRequest):
            return types.UpdatesTooLong()
        if isinstance(
            request,
            (functions.messages.EditMessageRequest, functions.messages.UpdatePinnedMessageRequest),
        ):
            return types.UpdatesTooLong()
        if isinstance(request, functions.messages.DeleteScheduledMessagesRequest):
            return types.UpdatesTooLong()
        if isinstance(request, functions.messages.SendVoteRequest):
            return types.UpdatesTooLong()
        return types.InputPeerSelf()


class StubAdapter(TelegramAdapter):
    def __init__(self, paths: Paths, fake: FakeClient) -> None:
        super().__init__(paths)
        self.fake = fake

    @asynccontextmanager
    async def client(self, profile: Profile, *, require_auth: bool = True) -> AsyncIterator[Any]:
        if require_auth and not self.fake.authorized:
            raise ClitgError
        yield self.fake


@pytest.fixture
def fake_client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def adapter(paths: Paths, fake_client: FakeClient) -> StubAdapter:
    return StubAdapter(paths, fake_client)


@pytest.mark.asyncio
async def test_real_client_context(
    paths: Paths, profile: Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeClient()
    lock = FakeLock()
    adapter = TelegramAdapter(paths)
    monkeypatch.setattr(adapter, "_new_client", lambda _: fake)
    monkeypatch.setattr(paths, "profile_lock", lambda _: lock)
    async with adapter.client(profile) as received:
        assert received is fake
    assert fake.connected and fake.disconnected and lock.released
    fake.authorized = False
    with pytest.raises(ClitgError, match="not authenticated"):
        async with adapter.client(profile):
            pass
    failing = FakeLock(fail=True)
    monkeypatch.setattr(paths, "profile_lock", lambda _: failing)
    with pytest.raises(ClitgError, match="already in use"):
        async with adapter.client(profile):
            pass
    client = TelegramAdapter._new_client(adapter, profile)
    assert client.flood_sleep_threshold == 0
    assert client.session is not None
    client.session.close()
    with pytest.raises(ClitgError, match="API hash"):
        TelegramAdapter._new_client(adapter, Profile(name="empty", api_id=1))


@pytest.mark.asyncio
async def test_auth(adapter: StubAdapter, profile: Profile, fake_client: FakeClient) -> None:
    assert await adapter.request_code(profile, "+1") == "hash-+1"
    assert (await adapter.verify(profile, "+1", "123", "h", None))["id"] == 1
    fake_client.password_needed = True
    with pytest.raises(ClitgError, match="2FA"):
        await adapter.verify(profile, "+1", "123", "h", None)
    assert (await adapter.verify(profile, "+1", "123", "h", "pw"))["id"] == 1
    assert (await adapter.auth_status(profile))["authorized"] is True
    fake_client.authorized = False
    assert (await adapter.auth_status(profile)) == {"authorized": False, "user": None}
    fake_client.authorized = True
    assert await adapter.logout(profile) is True


@pytest.mark.asyncio
async def test_resolve_peer(
    adapter: StubAdapter, profile: Profile, fake_client: FakeClient
) -> None:
    for reference in ("me", "@alice", "+1", "https://t.me/a", "t.me/a", "123", "-123"):
        assert await adapter.resolve_peer(fake_client, reference)
    fake_client.entity_error = True
    with pytest.raises(ClitgError, match="not found"):
        await adapter.resolve_peer(fake_client, "@missing")
    fake_client.entity_error = False
    assert (await adapter.resolve_peer(fake_client, "Alice User")).id == 1
    fake_client.dialog_entities = [user(1, "Bob"), user(2)]
    assert (await adapter.resolve_peer(fake_client, "Alice User")).id == 2
    fake_client.dialog_entities = [user(1), user(2)]
    with pytest.raises(ClitgError, match="ambiguous"):
        await adapter.resolve_peer(fake_client, "Alice User")
    fake_client.dialog_entities = []
    assert await adapter.resolve_peer(fake_client, "fallback")
    fake_client.entity_error = True
    with pytest.raises(ClitgError, match="not found"):
        await adapter.resolve_peer(fake_client, "missing")


@pytest.mark.asyncio
async def test_read_operations(
    adapter: StubAdapter, profile: Profile, fake_client: FakeClient
) -> None:
    assert await adapter.dialogs(profile, query=None, offset=0, limit=1, include_raw=False)
    assert not await adapter.dialogs(profile, query="missing", offset=0, limit=1, include_raw=True)
    assert (await adapter.peer(profile, "me", include_raw=True))["raw"]
    assert "raw" not in await adapter.peer(profile, "me", include_raw=False)
    assert await adapter.contacts(profile)
    assert await adapter.messages(
        profile,
        "me",
        limit=1,
        offset_id=0,
        query=None,
        topic_id=None,
        include_raw=True,
    )
    assert (await adapter.get_message(profile, "me", 1, include_raw=False))["id"] == "1"
    fake_client.message_result = None
    with pytest.raises(ClitgError, match="not found"):
        await adapter.get_message(profile, "me", 1, include_raw=False)


@pytest.mark.asyncio
async def test_send_and_mutations(
    adapter: StubAdapter, profile: Profile, fake_client: FakeClient, tmp_path: Path
) -> None:
    fake_client.message_result = message()
    assert await adapter.send(
        profile,
        "me",
        text="hello",
        files=[],
        reply_to=None,
        topic_id=None,
        parse_mode="plain",
        media_kind="auto",
        schedule_at=None,
    )
    file = tmp_path / "x"
    file.write_text("x")
    fake_client.message_result = [message(), message(2)]
    assert (
        len(
            await adapter.send(
                profile,
                "me",
                text="caption",
                files=[file, file],
                reply_to=1,
                topic_id=2,
                parse_mode="markdown",
                media_kind="voice",
                schedule_at=datetime.now(UTC),
            )
        )
        == 2
    )
    fake_client.message_result = message()
    assert await adapter.send(
        profile,
        "me",
        text="caption",
        files=[file],
        reply_to=None,
        topic_id=None,
        parse_mode="html",
        media_kind="document",
        schedule_at=None,
    )
    assert await adapter.forward(profile, "me", "@alice", [1])
    assert (await adapter.edit(profile, "me", 1, "x", "plain"))["id"] == "1"
    assert (await adapter.edit(profile, "me", 1, "x", "html"))["id"] == "1"
    assert (await adapter.delete(profile, "me", [1], everyone=True))["message_ids"] == [1]
    assert (await adapter.read(profile, "me", 1))["read"] is True
    assert await adapter.react(profile, "me", 1, "👍")
    assert await adapter.react(profile, "me", 1, None)
    assert await adapter.pin(profile, "me", 1, unpin=False)
    assert await adapter.pin(profile, "me", 1, unpin=True)


@pytest.mark.asyncio
async def test_repeating_scheduled_message_paths(
    adapter: StubAdapter, profile: Profile, fake_client: FakeClient, tmp_path: Path
) -> None:
    scheduled = datetime(2026, 8, 1, tzinfo=UTC)
    assert await adapter.send(
        profile,
        "me",
        text="repeat",
        files=[],
        reply_to=1,
        topic_id=None,
        parse_mode="markdown",
        media_kind="auto",
        schedule_at=scheduled,
        repeat_period=86_400,
    )
    file = tmp_path / "document.txt"
    file.write_text("content")
    assert await adapter.send(
        profile,
        "me",
        text="caption",
        files=[file],
        reply_to=None,
        topic_id=2,
        parse_mode="html",
        media_kind="document",
        schedule_at=scheduled,
        repeat_period=604_800,
    )
    assert await adapter.forward(
        profile,
        "me",
        "@alice",
        [1],
        schedule_at=scheduled,
        repeat_period=86_400,
    )
    assert (
        await adapter.edit(
            profile,
            "me",
            1,
            "changed",
            "markdown",
            schedule_at=scheduled,
            repeat_period=86_400,
        )
    )["id"] == "1"

    fake_client.message_result = None
    with pytest.raises(ClitgError, match="repeating scheduled"):
        await adapter.send(
            profile,
            "me",
            text="repeat",
            files=[],
            reply_to=None,
            topic_id=None,
            parse_mode="plain",
            media_kind="auto",
            schedule_at=scheduled,
            repeat_period=86_400,
        )
    with pytest.raises(ClitgError, match="every forwarded"):
        await adapter.forward(
            profile,
            "me",
            "@alice",
            [1],
            schedule_at=scheduled,
            repeat_period=86_400,
        )
    with pytest.raises(ClitgError, match="edited scheduled"):
        await adapter.edit(
            profile,
            "me",
            1,
            "changed",
            "plain",
            schedule_at=scheduled,
            repeat_period=86_400,
        )


@pytest.mark.asyncio
async def test_scheduled_topics_polls_raw_and_download(
    adapter: StubAdapter,
    profile: Profile,
    fake_client: FakeClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert await adapter.scheduled(profile, "me")
    assert await adapter.cancel_scheduled(profile, "me", [1])
    assert await adapter.topics(profile, "me", limit=3)
    assert await adapter.create_poll(
        profile,
        "me",
        "question",
        ["a", "b"],
        multiple_choice=True,
        anonymous=False,
        quiz=True,
    )
    assert await adapter.vote_poll(profile, "me", 1, [0, 1])
    poll = types.Poll(
        id=1,
        question=types.TextWithEntities(text="q", entities=[]),
        answers=[types.PollAnswer(text=types.TextWithEntities(text="a", entities=[]), option=b"0")],
        hash=0,
    )
    fake_client.message_result = message(
        media=types.MessageMediaPoll(poll=poll, results=types.PollResults())
    )
    assert await adapter.close_poll(profile, "me", 1)
    original_get_messages = fake_client.get_messages
    calls = 0

    async def disappearing_message(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return await original_get_messages(*args, **kwargs) if calls == 1 else None

    with monkeypatch.context() as context:
        context.setattr(fake_client, "get_messages", disappearing_message)
        with pytest.raises(ClitgError, match="Closed poll"):
            await adapter.close_poll(profile, "me", 1)
    fake_client.message_result = None
    with pytest.raises(ClitgError, match="not found"):
        await adapter.close_poll(profile, "me", 1)
    fake_client.message_result = message()
    with pytest.raises(ClitgError, match="not found"):
        await adapter.close_poll(profile, "me", 1)
    fake_client.message_result = message(
        media=types.MessageMediaDocument(document=types.DocumentEmpty(id=1))
    )
    output = tmp_path / "file"
    fake_client.download_result = str(output)
    assert await adapter.download(profile, "me", 1, output) == output
    fake_client.download_result = None
    with pytest.raises(ClitgError, match="did not return"):
        await adapter.download(profile, "me", 1, output)
    fake_client.message_result = None
    with pytest.raises(ClitgError, match="not found"):
        await adapter.download(profile, "me", 1, output)
    assert (await adapter.raw_invoke(profile, "help.getConfig", {}))["_"] == "InputPeerSelf"


def test_update_normalization() -> None:
    created = update_view(types.UpdateNewMessage(message=message(), pts=1, pts_count=1))
    assert created["event_type"] == "message.new" and created["peer_id"] == "1"
    edited = update_view(types.UpdateEditMessage(message=message(), pts=1, pts_count=1))
    assert edited["event_type"] == "message.edited"
    deleted = update_view(types.UpdateDeleteMessages(messages=[1], pts=1, pts_count=1))
    assert deleted["event_type"] == "message.deleted"
    read = update_view(
        types.UpdateReadHistoryInbox(
            peer=types.PeerUser(2),
            max_id=1,
            still_unread_count=0,
            pts=1,
            pts_count=1,
        )
    )
    assert read["event_type"] == "message.read" and read["peer_id"] == "2"
    fallback = update_view(
        types.UpdateUserStatus(user_id=3, status=types.UserStatusOnline(datetime.now(UTC)))
    )
    assert fallback["event_type"] == "telegram.raw_update"
    assert fallback["raw_type"] == "UpdateUserStatus" and fallback["peer_id"] == "3"
    chat_read = update_view(
        types.UpdateReadHistoryInbox(
            peer=types.PeerChat(4),
            max_id=1,
            still_unread_count=0,
            pts=1,
            pts_count=1,
        )
    )
    assert chat_read["peer_id"] == "4"
    channel_read = update_view(
        types.UpdateReadHistoryInbox(
            peer=types.PeerChannel(5),
            max_id=1,
            still_unread_count=0,
            pts=1,
            pts_count=1,
        )
    )
    assert channel_read["peer_id"] == "5"
    assert update_view(types.UpdateConfig())["peer_id"] is None
    assert update_view(SimpleNamespace(peer=SimpleNamespace()))["peer_id"] is None


def test_sticker_file_validation(tmp_path: Path) -> None:
    missing = tmp_path / "missing.webp"
    with pytest.raises(ClitgError, match="does not exist"):
        TelegramAdapter._validate_sticker_file(missing)
    unsupported = tmp_path / "sticker.gif"
    unsupported.write_bytes(b"GIF89a")
    with pytest.raises(ClitgError, match="must be PNG"):
        TelegramAdapter._validate_sticker_file(unsupported)
    too_large = tmp_path / "large.tgs"
    too_large.write_bytes(b"\x1f\x8b" + b"x" * 64_000)
    with pytest.raises(ClitgError, match="too large"):
        TelegramAdapter._validate_sticker_file(too_large)

    invalid = tmp_path / "invalid.webp"
    invalid.write_bytes(b"not-webp")
    with pytest.raises(ClitgError, match="signature"):
        TelegramAdapter._validate_sticker_file(invalid)
    png = tmp_path / "valid.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    assert TelegramAdapter._validate_sticker_file(png) == "image/png"
    webp = tmp_path / "valid.webp"
    webp.write_bytes(b"RIFF\x00\x00\x00\x00WEBP")
    assert TelegramAdapter._validate_sticker_file(webp) == "image/webp"
    webm = tmp_path / "valid.webm"
    webm.write_bytes(b"\x1a\x45\xdf\xa3")
    assert TelegramAdapter._validate_sticker_file(webm) == "video/webm"

    valid_tgs = tmp_path / "valid.tgs"
    valid_tgs.write_bytes(
        gzip.compress(json.dumps({"w": 512, "h": 512, "ip": 0, "op": 60, "fr": 30}).encode())
    )
    assert TelegramAdapter._validate_sticker_file(valid_tgs) == "application/x-tgsticker"
    broken_tgs = tmp_path / "broken.tgs"
    broken_tgs.write_bytes(b"\x1f\x8bnot-gzip")
    with pytest.raises(ClitgError, match="TGS file is invalid"):
        TelegramAdapter._validate_sticker_file(broken_tgs)
    invalid_shape = tmp_path / "shape.tgs"
    invalid_shape.write_bytes(
        gzip.compress(json.dumps({"w": 256, "h": 512, "ip": 0, "op": 120, "fr": 30}).encode())
    )
    with pytest.raises(ClitgError, match="dimensions or duration"):
        TelegramAdapter._validate_sticker_file(invalid_shape)


@pytest.mark.asyncio
async def test_feature_validation_sticker_upload_and_invocation(
    adapter: StubAdapter,
    profile: Profile,
    fake_client: FakeClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    webp = tmp_path / "valid.webp"
    webp.write_bytes(b"RIFF\x00\x00\x00\x00WEBP")
    create = FEATURE_BY_COMMAND["stickers.create-set"]
    add = FEATURE_BY_COMMAND["stickers.add"]
    await adapter.validate_feature(create, {"_feature_files": [str(webp)]})
    await adapter.validate_feature(add, {"_feature_files": str(webp)})
    await adapter.validate_feature(FEATURE_BY_COMMAND["ai-tones.list"], {"hash": 0})

    uploaded = await adapter._upload_sticker(fake_client, webp)
    assert isinstance(uploaded, types.InputDocument)
    fake_client.upload_document = None
    with pytest.raises(ClitgError, match="sticker document"):
        await adapter._upload_sticker(fake_client, webp)
    fake_client.upload_document = types.Document(
        id=1,
        access_hash=2,
        file_reference=b"ref",
        date=datetime.now(UTC),
        mime_type="image/webp",
        size=10,
        dc_id=1,
        attributes=[],
    )

    with pytest.raises(ClitgError, match="one emoji"):
        await adapter.feature_invoke(
            profile,
            create,
            {
                "title": "Set",
                "short_name": "set_by_clitg_bot",
                "emoji": ["🙂", "🚀"],
                "_feature_files": [str(webp)],
            },
            values={},
        )
    created = await adapter.feature_invoke(
        profile,
        create,
        {
            "title": "Set",
            "short_name": "set_by_clitg_bot",
            "emoji": ["🙂"],
            "masks": False,
            "emojis": False,
            "text_color": False,
            "_feature_files": [str(webp)],
            "_feature_input": None,
        },
        values={},
    )
    assert created["_"] == "UpdatesTooLong"
    with pytest.raises(ClitgError, match="Exactly one"):
        await adapter.feature_invoke(
            profile,
            add,
            {
                "short_name": "set_by_clitg_bot",
                "emoji": "🙂",
                "_feature_files": [],
            },
            values={},
        )
    added = await adapter.feature_invoke(
        profile,
        add,
        {
            "short_name": "set_by_clitg_bot",
            "emoji": "🙂",
            "keywords": "happy",
            "file": str(webp),
            "_feature_files": str(webp),
        },
        values={},
    )
    assert added["_"] == "UpdatesTooLong"
    regular = await adapter.feature_invoke(
        profile,
        FEATURE_BY_COMMAND["ai-tones.list"],
        {"hash": 0},
        values={},
    )
    assert regular["_"] == "InputPeerSelf"
    todo = FEATURE_BY_COMMAND["todos.create"]
    todo_params = build_feature_params(
        todo,
        {"peer": "me", "title": "Smoke", "item": ["Done"]},
    )
    sent_todo = await adapter.feature_invoke(profile, todo, todo_params, values={})
    assert sent_todo["_"] == "InputPeerSelf"
    todo_request = next(
        call[1]
        for call in fake_client.calls
        if call[0] == "request" and isinstance(call[1], functions.messages.SendMediaRequest)
    )
    assert todo_request.random_id is not None

    async def build_without_random_id(*args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(random_id=None)

    monkeypatch.setattr(adapter.codec, "build", build_without_random_id)
    await adapter.feature_invoke(profile, FEATURE_BY_COMMAND["ai-tones.list"], {}, values={})
    assert fake_client.calls[-1][1].random_id is not None


@pytest.mark.asyncio
async def test_transcription_wait_paths(
    adapter: StubAdapter,
    profile: Profile,
    fake_client: FakeClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from telethon.tl.types import messages as message_types

    feature = FEATURE_BY_COMMAND["messages.transcribe"]
    params = {"peer": {"$peer": "me"}, "msg_id": 1}
    fake_client.request_result = message_types.TranscribedAudio(
        transcription_id=7, text="done", pending=False
    )
    immediate = await adapter.feature_invoke(profile, feature, params, values={"wait_seconds": 1})
    assert immediate["text"] == "done"

    fake_client.request_result = message_types.TranscribedAudio(
        transcription_id=7, text="", pending=True
    )
    fake_client.pending_updates = [
        types.UpdateConfig(),
        types.UpdateTranscribedAudio(
            peer=types.PeerUser(1),
            msg_id=1,
            transcription_id=8,
            text="other",
            pending=False,
        ),
        types.UpdateTranscribedAudio(
            peer=types.PeerUser(1),
            msg_id=1,
            transcription_id=7,
            text="final",
            pending=False,
        ),
    ]
    completed = await adapter.feature_invoke(profile, feature, params, values={"wait_seconds": 1})
    assert completed["text"] == "final"

    async def timeout(awaitable: Any, *, timeout: float) -> Any:
        del timeout
        awaitable.close()
        raise TimeoutError

    fake_client.pending_updates = []
    monkeypatch.setattr("clitg.telegram.asyncio.wait_for", timeout)
    timed_out = await adapter.feature_invoke(profile, feature, params, values={"wait_seconds": 1})
    assert timed_out["wait_timed_out"] is True

    times = iter((0.0, 2.0))
    monkeypatch.undo()
    monkeypatch.setattr(
        "clitg.telegram.asyncio.get_running_loop",
        lambda: SimpleNamespace(time=lambda: next(times)),
    )
    expired = await adapter.feature_invoke(profile, feature, params, values={"wait_seconds": 1})
    assert expired["wait_timed_out"] is True

    fake_client.request_result = None
    no_wait = await adapter.feature_invoke(profile, feature, params, values={"wait_seconds": 0})
    assert no_wait["_"] == "InputPeerSelf"


@pytest.mark.asyncio
async def test_qr_login_writes_png(
    adapter: StubAdapter,
    profile: Profile,
    fake_client: FakeClient,
    tmp_path: Path,
) -> None:
    fake_client.authorized = False
    output = tmp_path / "qr" / "login.png"
    result = await adapter.qr_login(profile, output, 12)
    assert result["authorized"] is True
    assert result["qr_path"] == str(output.resolve())
    with Image.open(output) as image:
        assert image.format == "PNG"
        assert image.width == image.height > 0
        image.verify()


@pytest.mark.asyncio
async def test_qr_and_new_read_operations(
    adapter: StubAdapter,
    profile: Profile,
    fake_client: FakeClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Image:
        def save(self, output: Path) -> None:
            output.write_bytes(b"png")

    monkeypatch.setattr("clitg.telegram.qrcode.make", lambda _: Image())
    fake_client.authorized = False
    qr = await adapter.qr_login(profile, tmp_path / "qr.png", 12)
    assert qr["authorized"] and qr["qr_path"]
    fake_client.authorized = True
    assert (await adapter.qr_login(profile, tmp_path / "unused.png", 12))["already_authorized"]
    fake_client.message_result = message()
    assert await adapter.global_search(
        profile,
        query="hello",
        limit=1,
        offset_id=0,
        include_raw=True,
    )
    threshold = datetime(2026, 1, 2, tzinfo=UTC)
    media = types.MessageMediaDocument(document=types.DocumentEmpty(id=1))
    fake_client.message_result = [
        message(1),
        message(2, media=media),
        message(3, media=media),
    ]
    fake_client.message_result[2].date = datetime(2026, 1, 3, tzinfo=UTC)
    filtered = await adapter.global_search(
        profile,
        query="hello",
        limit=2,
        offset_id=0,
        sender="me",
        after=threshold,
        before=datetime(2026, 1, 4, tzinfo=UTC),
        media_only=True,
        include_raw=False,
    )
    assert [item["id"] for item in filtered] == ["3"]
    fake_client.message_result = []
    assert not await adapter.global_search(
        profile,
        query="missing",
        limit=2,
        offset_id=0,
        include_raw=False,
    )
    fake_client.message_result = [message(1), message(2)]
    assert not await adapter.messages(
        profile,
        "me",
        limit=3,
        offset_id=0,
        query=None,
        topic_id=None,
        sender="me",
        media_only=True,
        include_raw=False,
    )
    fake_client.message_result = [message(1, media=media), message(2, media=media)]
    assert (
        len(
            await adapter.messages(
                profile,
                "me",
                limit=3,
                offset_id=0,
                query=None,
                topic_id=None,
                include_raw=False,
            )
        )
        == 2
    )
    fake_client.dialog_unread_count = 1
    incoming = message()
    incoming.out = False
    fake_client.message_result = incoming
    assert await adapter.inbox(
        profile,
        view="messages",
        include_archived=False,
        limit=10,
        offset=0,
    )
    assert not await adapter.inbox(
        profile,
        view="messages",
        include_archived=False,
        limit=10,
        offset=0,
        sender="me",
    )
    fake_client.message_result = message()
    assert not await adapter.inbox(
        profile,
        view="messages",
        include_archived=True,
        limit=10,
        offset=0,
    )
    assert await adapter.inbox(
        profile,
        view="dialogs",
        include_archived=False,
        limit=10,
        offset=0,
    )
    fake_client.dialog_archived = True
    assert not await adapter.inbox(
        profile,
        view="dialogs",
        include_archived=False,
        limit=10,
        offset=0,
    )
    assert await adapter.inbox(
        profile,
        view="dialogs",
        include_archived=True,
        limit=10,
        offset=0,
    )
    assert not await adapter.inbox(
        profile,
        view="dialogs",
        include_archived=True,
        limit=10,
        offset=0,
        folder_id=2,
    )
    fake_client.message_result = [message(1), None, message(3)]
    context = await adapter.message_context(
        profile,
        "me",
        2,
        before=1,
        after=1,
        include_raw=False,
    )
    assert len(context) == 2
    fake_client.message_result = message()
    assert await adapter.message_replies(
        profile,
        "me",
        1,
        limit=1,
        offset_id=0,
        include_raw=True,
    )


def test_message_filter_predicate() -> None:
    current = message()
    assert TelegramAdapter._message_matches(
        current, sender_id=2, after=None, before=None, media_only=False
    )
    assert not TelegramAdapter._message_matches(
        None, sender_id=None, after=None, before=None, media_only=False
    )
    assert not TelegramAdapter._message_matches(
        current, sender_id=99, after=None, before=None, media_only=False
    )
    assert not TelegramAdapter._message_matches(
        current,
        sender_id=None,
        after=datetime(2026, 1, 2, tzinfo=UTC),
        before=None,
        media_only=False,
    )
    assert not TelegramAdapter._message_matches(
        current,
        sender_id=None,
        after=None,
        before=datetime(2026, 1, 1, tzinfo=UTC),
        media_only=False,
    )
    assert not TelegramAdapter._message_matches(
        current, sender_id=None, after=None, before=None, media_only=True
    )
    no_date = SimpleNamespace(sender_id=2, date=None, media=object())
    assert not TelegramAdapter._message_matches(
        no_date, sender_id=None, after=datetime.now(UTC), before=None, media_only=False
    )
    assert not TelegramAdapter._message_matches(
        no_date, sender_id=None, after=None, before=datetime.now(UTC), media_only=False
    )


@pytest.mark.asyncio
async def test_update_watch_controls(
    adapter: StubAdapter, profile: Profile, fake_client: FakeClient
) -> None:
    fake_client.pending_updates = [types.UpdateNewMessage(message=message(), pts=1, pts_count=1)]
    records = [
        item
        async for item in adapter.watch_updates(
            profile,
            event_types={"message.new"},
            peers={"1"},
            max_events=1,
            idle_timeout=1,
            total_timeout=1,
            heartbeat=None,
        )
    ]
    assert len(records) == 1
    assert any(call[0] == "remove_event_handler" for call in fake_client.calls)

    fake_client.pending_updates = [types.UpdateNewMessage(message=message(), pts=1, pts_count=1)]
    filtered = [
        item
        async for item in adapter.watch_updates(
            profile,
            event_types={"message.deleted"},
            peers=set(),
            max_events=1,
            idle_timeout=0.01,
            total_timeout=0.02,
            heartbeat=0.002,
        )
    ]
    assert filtered and all(item["event_type"] == "heartbeat" for item in filtered)

    fake_client.pending_updates = [types.UpdateNewMessage(message=message(), pts=1, pts_count=1)]
    peer_filtered = [
        item
        async for item in adapter.watch_updates(
            profile,
            event_types=set(),
            peers={"99"},
            max_events=1,
            idle_timeout=0.005,
            total_timeout=None,
            heartbeat=None,
        )
    ]
    assert peer_filtered == []
    fake_client.pending_updates = []
    timed_out = [
        item
        async for item in adapter.watch_updates(
            profile,
            event_types=set(),
            peers=set(),
            max_events=None,
            idle_timeout=None,
            total_timeout=0.002,
            heartbeat=None,
        )
    ]
    assert timed_out == []
