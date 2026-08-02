from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import pytest
from typer.testing import CliRunner

import clitg.cli as cli
from clitg.errors import ClitgError
from clitg.features import FEATURE_COMMANDS
from clitg.models import CommandResult, ErrorCode, OutputFormat
from clitg.service import ClitgService
from clitg.storage import Paths

runner = CliRunner()


class FakeService:
    instances: ClassVar[list[FakeService]] = []
    failure: str | None = None

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.instances.append(self)

    def __getattr__(self, name: str) -> Any:
        def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, args, kwargs))
            if self.failure == "domain":
                raise ClitgError(ErrorCode.NOT_FOUND, "missing")
            if self.failure == "internal":
                raise RuntimeError("boom")
            if self.failure == "wrong":
                return "wrong"
            return CommandResult(data={"method": name}, items=[{"method": name}])

        return call

    async def watch_updates(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(("watch_updates", args, kwargs))
        if self.failure == "domain":
            raise ClitgError(ErrorCode.NETWORK, "stream failed")
        if self.failure == "internal":
            raise RuntimeError("stream failed")
        yield {"event_type": "message.new", "cursor": "cursor", "data": {}}


@pytest.fixture(autouse=True)
def fake_service(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeService.instances.clear()
    FakeService.failure = None
    monkeypatch.setattr(cli, "SERVICE_FACTORY", FakeService)


COMMANDS = [
    ["profiles", "create", "--name", "p", "--api-id", "1", "--api-hash", "h"],
    ["profiles", "list"],
    ["profiles", "get", "--name", "p"],
    ["profiles", "set-default", "--name", "p"],
    ["profiles", "remove", "--name", "p", "--dry-run"],
    ["auth", "request-code", "--phone", "+1"],
    ["auth", "verify", "--login-id", "id", "--code", "123"],
    ["auth", "status"],
    ["auth", "qr-login", "--qr-output", "qr.png"],
    ["auth", "logout", "--dry-run"],
    ["dialogs", "list"],
    ["dialogs", "search", "--query", "group"],
    ["dialogs", "get", "--peer", "me"],
    ["inbox", "list"],
    ["contacts", "list"],
    ["contacts", "search", "--query", "alice"],
    ["contacts", "resolve", "--peer", "@alice"],
    ["messages", "list", "--peer", "me"],
    ["messages", "search", "--peer", "me", "--query", "hello"],
    ["messages", "get", "--peer", "me", "--message-id", "1"],
    ["messages", "context", "--peer", "me", "--message-id", "1"],
    ["messages", "replies", "--peer", "me", "--message-id", "1"],
    ["messages", "export", "--peer", "me", "--output", "export"],
    ["messages", "send", "--peer", "me", "--text", "hello", "--dry-run"],
    [
        "messages",
        "reply",
        "--peer",
        "me",
        "--message-id",
        "1",
        "--text",
        "hello",
        "--dry-run",
    ],
    [
        "messages",
        "forward",
        "--source-peer",
        "me",
        "--target-peer",
        "@a",
        "--message-id",
        "1",
        "--dry-run",
    ],
    [
        "messages",
        "edit",
        "--peer",
        "me",
        "--message-id",
        "1",
        "--text",
        "edited",
        "--dry-run",
    ],
    [
        "messages",
        "delete",
        "--peer",
        "me",
        "--message-id",
        "1",
        "--scope",
        "self",
        "--dry-run",
    ],
    ["messages", "read", "--peer", "me", "--dry-run"],
    ["messages", "react", "--peer", "me", "--message-id", "1", "--dry-run"],
    ["messages", "pin", "--peer", "me", "--message-id", "1", "--dry-run"],
    ["messages", "unpin", "--peer", "me", "--message-id", "1", "--dry-run"],
    [
        "media",
        "download",
        "--peer",
        "me",
        "--message-id",
        "1",
        "--output",
        "x",
        "--dry-run",
    ],
    [
        "polls",
        "create",
        "--peer",
        "me",
        "--question",
        "q",
        "--answer",
        "a",
        "--answer",
        "b",
        "--dry-run",
    ],
    ["polls", "vote", "--peer", "me", "--message-id", "1", "--option", "0", "--dry-run"],
    ["polls", "close", "--peer", "me", "--message-id", "1", "--dry-run"],
    ["scheduled", "list", "--peer", "me"],
    ["scheduled", "cancel", "--peer", "me", "--message-id", "1", "--dry-run"],
    ["topics", "list", "--peer", "me"],
    ["raw", "invoke", "--method", "help.getConfig", "--params", "{}", "--allow-raw", "--dry-run"],
    ["capabilities", "list"],
    ["capabilities", "get", "--method", "help.getConfig"],
    ["schema", "list"],
    ["schema", "get", "--name", "Envelope"],
    ["schema", "export", "--output", "schema.json"],
    ["state", "get"],
    ["state", "prune", "--kind", "all", "--dry-run"],
    ["account", "get", "--params", '{"id":{"_":"InputUserSelf"}}'],
    ["commands", "list"],
    ["commands", "get", "--command", "stories.publish"],
    ["policy", "validate", "--file", "policy.json"],
    ["policy", "set", "--name", "personal", "--file", "policy.json"],
    ["policy", "get"],
    ["policy", "explain", "--command", "messages.send"],
    ["audit", "list"],
    ["audit", "export", "--output", "audit.jsonl"],
    ["audit", "prune", "--dry-run"],
]


@pytest.mark.parametrize("arguments", COMMANDS)
def test_every_cli_command(arguments: list[str]) -> None:
    result = runner.invoke(cli.app, ["--profile", "personal", *arguments])
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed["ok"] is True


@pytest.mark.parametrize("feature", FEATURE_COMMANDS, ids=lambda feature: feature.command)
def test_every_generated_feature_command(feature: Any) -> None:
    group, command = feature.command.split(".", maxsplit=1)
    arguments = ["--profile", "personal", group, command]
    values = {
        "str": "value",
        "int": "1",
        "float": "1.0",
        "str_list": "value",
        "int_list": "1",
    }
    for option in feature.options:
        if option.required:
            arguments.extend([option.flag, values[option.kind]])
    result = runner.invoke(cli.app, arguments)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["ok"] is True


def test_help_version_and_jsonl() -> None:
    help_result = runner.invoke(cli.app, [])
    assert help_result.exit_code == 0
    assert "Structured Telegram" in help_result.stdout
    help_option = runner.invoke(cli.app, ["--help"])
    help_command = runner.invoke(cli.app, ["help"])
    assert help_command.exit_code == help_option.exit_code == 0
    assert help_command.stdout == help_option.stdout
    structured = runner.invoke(cli.app, ["--help-json"])
    assert json.loads(structured.stdout)["data"]["groups"]["messages"]
    version_option = runner.invoke(cli.app, ["--version"])
    version = runner.invoke(cli.app, ["version"])
    assert version.exit_code == version_option.exit_code == 0
    assert json.loads(version_option.stdout)["data"]["cli_version"] == "0.3.2"
    assert json.loads(version.stdout)["data"] == json.loads(version_option.stdout)["data"]
    assert json.loads(version.stdout)["data"]["schema_version"] == "0.2"
    jsonl = runner.invoke(cli.app, ["--output", "jsonl", "profiles", "list"])
    lines = [json.loads(line) for line in jsonl.stdout.splitlines()]
    assert [line["record_type"] for line in lines] == ["item", "summary"]


def test_service_cache_and_invalid_context() -> None:
    context = cli.CliContext(None, OutputFormat.JSON, 4, False)
    first = cast(FakeService, cli._service(context))
    assert cli._service(context) is first
    assert first.kwargs == {"timeout_seconds": 4}
    bad = SimpleNamespace(find_root=lambda: SimpleNamespace(obj=None))
    with pytest.raises(RuntimeError):
        cli._context(cast(Any, bad))


def test_input_helpers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "value"
    source.write_text("from-file")
    assert cli._read_text(None, source, False, label="value") == "from-file"
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(read=lambda: "from-stdin"))
    assert cli._read_text(None, None, True, label="value") == "from-stdin"
    monkeypatch.setenv("VALUE", "from-env")
    assert cli._read_text(None, None, False, label="value", environment="VALUE") == "from-env"
    assert cli._read_text(None, None, False, label="value") == ""
    with pytest.raises(ClitgError, match="Only one"):
        cli._read_text("x", source, False, label="value")
    with pytest.raises(ClitgError, match="required"):
        cli._read_text(None, None, False, label="value", required=True)
    with pytest.raises(ClitgError, match="Unable"):
        cli._read_text(None, tmp_path / "missing", False, label="value")
    assert cli._read_json('{"x":1}', None, False) == {"x": 1}
    with pytest.raises(ClitgError, match="invalid"):
        cli._read_json("{", None, False)
    with pytest.raises(ClitgError, match="object"):
        cli._read_json("[]", None, False)


def test_feature_input_helpers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert cli._read_feature_input(None, False) is None
    source = tmp_path / "input.json"
    source.write_text('{"value":1}')
    assert cli._read_feature_input(source, False) == {"value": 1}
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(read=lambda: '{"id":1}\n{"id":2}\n'))
    assert cli._read_feature_input(None, True) == [{"id": 1}, {"id": 2}]
    source.write_text("not-json")
    with pytest.raises(ClitgError, match="invalid JSON or JSONL"):
        cli._read_feature_input(source, False)
    source.write_text("1")
    with pytest.raises(ClitgError, match="object or array"):
        cli._read_feature_input(source, False)


def test_generated_feature_handler_forwards_common_options(tmp_path: Path) -> None:
    source = tmp_path / "input.json"
    source.write_text('{"extra":1}')
    result = runner.invoke(
        cli.app,
        [
            "--profile",
            "personal",
            "messages",
            "translate",
            "--to-lang",
            "en",
            "--text",
            "hola",
            "--input",
            str(source),
            "--dry-run",
            "--confirm",
            "messages.translate",
            "--confirmation-token",
            "token",
            "--idempotency-key",
            "key",
            "--include-raw",
        ],
    )
    assert result.exit_code == 0, result.output
    call = FakeService.instances[-1].calls[-1]
    assert call[0] == "execute_feature"
    assert call[1][2] == {"to_lang": "en", "text": "hola"}
    assert call[1][3] == {"extra": 1}
    assert call[2] == {
        "dry_run": True,
        "confirmation": "messages.translate",
        "confirmation_token": "token",
        "idempotency_key": "key",
        "include_raw": True,
    }


def test_datetime_and_parse_mode() -> None:
    assert cli._parse_datetime(None) is None
    parsed = cli._parse_datetime("2026-01-01T00:00:00Z")
    assert parsed is not None and parsed.tzinfo
    with pytest.raises(ClitgError, match="RFC"):
        cli._parse_datetime("bad")
    with pytest.raises(ClitgError, match="offset"):
        cli._parse_datetime("2026-01-01T00:00:00")
    for value in ("plain", "markdown", "html"):
        assert cli._validate_parse_mode(value) == value
    with pytest.raises(ClitgError, match="Parse mode"):
        cli._validate_parse_mode("rich")


def test_file_and_environment_command_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = tmp_path / "secret"
    secret.write_text("hash")
    assert (
        runner.invoke(
            cli.app,
            [
                "profiles",
                "create",
                "--name",
                "p",
                "--api-id",
                "1",
                "--api-hash-file",
                str(secret),
            ],
        ).exit_code
        == 0
    )
    monkeypatch.setenv("CLITG_API_ID", "2")
    monkeypatch.setenv("CLITG_API_HASH", "hash")
    assert runner.invoke(cli.app, ["profiles", "create", "--name", "p"]).exit_code == 0
    monkeypatch.setenv("CLITG_API_ID", "0")
    invalid = runner.invoke(cli.app, ["profiles", "create", "--name", "p"])
    assert json.loads(invalid.stdout)["error"]["code"] == "invalid_input"
    text = tmp_path / "text"
    text.write_text("hello")
    assert (
        runner.invoke(
            cli.app,
            ["messages", "edit", "--peer", "me", "--message-id", "1", "--text-file", str(text)],
        ).exit_code
        == 0
    )
    code = tmp_path / "code"
    password = tmp_path / "password"
    code.write_text("123")
    password.write_text("pw")
    assert (
        runner.invoke(
            cli.app,
            [
                "auth",
                "verify",
                "--login-id",
                "id",
                "--code-file",
                str(code),
                "--password-file",
                str(password),
            ],
        ).exit_code
        == 0
    )


@pytest.mark.parametrize("failure", ["domain", "internal", "wrong"])
def test_structured_command_failures(failure: str) -> None:
    FakeService.failure = failure
    result = runner.invoke(cli.app, ["--verbose", "state", "get"])
    assert result.exit_code != 0
    parsed = json.loads(result.stdout)
    expected = "not_found" if failure == "domain" else "internal"
    assert parsed["error"]["code"] == expected
    if failure != "domain":
        assert "exception" in result.stderr


def test_non_verbose_internal_failure() -> None:
    FakeService.failure = "internal"
    result = runner.invoke(cli.app, ["state", "get"])
    assert result.exit_code == 1
    assert result.stderr == ""


def test_async_command_factory(capsys: pytest.CaptureFixture[str]) -> None:
    context = cli.CliContext(None, OutputFormat.JSON, 30, False)
    typer_context = SimpleNamespace(find_root=lambda: SimpleNamespace(obj=context))

    async def factory() -> CommandResult:
        return CommandResult(data={"async": True})

    cli._execute(cast(Any, typer_context), "async", factory)
    assert json.loads(capsys.readouterr().out)["data"] == {"async": True}


def test_jsonl_error() -> None:
    FakeService.failure = "domain"
    result = runner.invoke(cli.app, ["--output", "jsonl", "state", "get"])
    assert json.loads(result.stdout)["record_type"] == "error"


def test_new_cli_structured_inputs_and_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    params = tmp_path / "params.json"
    params.write_text('{"id":{"_":"InputUserSelf"}}')
    action = runner.invoke(cli.app, ["account", "get", "--params-file", str(params)])
    assert action.exit_code == 0
    assert (
        runner.invoke(
            cli.app,
            ["account", "get", "--params-stdin"],
            input=params.read_text(),
        ).exit_code
        == 0
    )
    stream = runner.invoke(
        cli.app,
        [
            "--output",
            "jsonl",
            "updates",
            "watch",
            "--event",
            "message.new",
            "--peer",
            "me",
            "--max-events",
            "1",
        ],
    )
    lines = [json.loads(line) for line in stream.stdout.splitlines()]
    assert [line["record_type"] for line in lines] == ["item", "summary"]
    wrong_output = runner.invoke(cli.app, ["updates", "watch", "--max-events", "1"])
    assert wrong_output.exit_code == 2
    FakeService.failure = "domain"
    failed = runner.invoke(
        cli.app,
        ["--output", "jsonl", "updates", "watch", "--max-events", "1"],
    )
    assert failed.exit_code == 8
    FakeService.failure = "internal"
    failed = runner.invoke(
        cli.app,
        ["--verbose", "--output", "jsonl", "updates", "watch", "--max-events", "1"],
    )
    assert failed.exit_code == 1
    assert json.loads(failed.stdout)["error"]["code"] == "internal"
    assert "exception" in failed.stderr


def test_batch_cli_and_manifest_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    batch = tmp_path / "batch.jsonl"
    batch.write_text(
        '\n{"id":"one","command":"account.get","params":{"id":{"_":"InputUserSelf"}}}\n'
    )
    assert runner.invoke(cli.app, ["batch", "run", "--input", str(batch)]).exit_code == 0
    assert (
        runner.invoke(
            cli.app,
            ["batch", "run", "--stdin"],
            input='{"id":"one","command":"auth.sessions"}\n',
        ).exit_code
        == 0
    )
    batch.write_text("bad")
    invalid = runner.invoke(cli.app, ["batch", "run", "--input", str(batch)])
    assert invalid.exit_code == 2
    batch.write_text("\n")
    empty = runner.invoke(cli.app, ["batch", "run", "--input", str(batch)])
    assert empty.exit_code == 2
    missing = runner.invoke(cli.app, ["commands", "get", "--command", "missing"])
    assert missing.exit_code == 4


def test_main_and_usage_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["clitg", "version"])
    assert cli.main() == 0
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["clitg", "messages", "get"])
    assert cli.main() == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_input"
    FakeService.failure = "domain"
    monkeypatch.setattr(sys, "argv", ["clitg", "state", "get"])
    assert cli.main() == 4
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "not_found"


def test_main_exit_and_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["clitg", "--help-json"])
    assert cli.main() == 0
    called: list[bool] = []
    monkeypatch.setattr(cli, "main", lambda: called.append(True) or 0)
    runpy.run_module("clitg.__main__", run_name="__main__")
    assert called == [True]


def test_main_returns_click_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_exit(**_: Any) -> None:
        raise cli.click.exceptions.Exit(7)

    monkeypatch.setattr(cli, "app", raise_exit)
    assert cli.main() == 7


def test_real_service_audit_paths(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    service = ClitgService(Paths(tmp_path / "config", tmp_path / "data"))
    context = cli.CliContext(None, OutputFormat.JSON, 30, False, service=service)
    typer_context = SimpleNamespace(find_root=lambda: SimpleNamespace(obj=context))
    cli._execute(cast(Any, typer_context), "ok", lambda: CommandResult(data={}))
    with pytest.raises(cli.typer.Exit):
        cli._execute(
            cast(Any, typer_context),
            "domain",
            lambda: (_ for _ in ()).throw(ClitgError(ErrorCode.NOT_FOUND, "missing")),
        )
    with pytest.raises(cli.typer.Exit):
        cli._execute(
            cast(Any, typer_context),
            "internal",
            lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        )
    assert len(service.state.list_audit()) == 3
    capsys.readouterr()

    context.output = OutputFormat.JSONL

    async def stream() -> Any:
        yield {"cursor": "one"}

    cli._execute_stream(cast(Any, typer_context), "stream", stream)

    async def failed_stream() -> Any:
        if False:
            yield {}
        raise ClitgError(ErrorCode.NETWORK, "failed")

    with pytest.raises(cli.typer.Exit):
        cli._execute_stream(cast(Any, typer_context), "failed-stream", failed_stream)

    async def internal_stream() -> Any:
        if False:
            yield {}
        raise RuntimeError("failed")

    with pytest.raises(cli.typer.Exit):
        cli._execute_stream(cast(Any, typer_context), "internal-stream", internal_stream)
    assert len(service.state.list_audit()) == 6
