"""Generate machine-readable command, schema, and MTProto capability catalogs."""

from __future__ import annotations

import inspect
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import telethon
from telethon.tl import alltlobjects
from telethon.tl.tlobject import TLRequest

from clitg import SCHEMA_VERSION, __version__
from clitg.models import (
    AuditRecord,
    BatchOperation,
    Capability,
    CapabilityCatalog,
    Confirmation,
    Envelope,
    ErrorInfo,
    FeatureResult,
    JsonlRecord,
    LoginState,
    Meta,
    PolicyDocument,
    ProfileView,
    UpdateRecord,
)
from clitg.serialization import to_jsonable

HIGH_LEVEL_METHODS: dict[str, str] = {
    "messages.getForumTopics": "topics.list",
    "contacts.getContacts": "contacts.list",
    "messages.deleteMessages": "messages.delete",
    "messages.deleteScheduledMessages": "scheduled.cancel",
    "messages.editMessage": "messages.edit",
    "messages.forwardMessages": "messages.forward",
    "messages.getHistory": "messages.list",
    "messages.getMessages": "messages.get",
    "messages.getScheduledHistory": "scheduled.list",
    "messages.readHistory": "messages.read",
    "messages.search": "messages.search",
    "messages.sendMedia": "messages.send",
    "messages.sendMessage": "messages.send",
    "messages.sendMultiMedia": "messages.send",
    "messages.sendReaction": "messages.react",
    "messages.updatePinnedMessage": "messages.pin",
}

UNSUPPORTED_METHODS: dict[str, str] = {
    "messages.acceptEncryption": "Secret-chat encryption state is not implemented",
    "messages.discardEncryption": "Secret-chat encryption state is not implemented",
    "messages.requestEncryption": "Secret-chat encryption state is not implemented",
    "messages.sendEncrypted": "Secret-chat encryption state is not implemented",
    "messages.sendEncryptedFile": "Secret-chat encryption state is not implemented",
    "messages.sendEncryptedService": "Secret-chat encryption state is not implemented",
}

CRITICAL_PATTERNS = (
    re.compile(r"^account\.(delete|updatePassword|resetPassword|changePhone)"),
    re.compile(r"^auth\."),
    re.compile(r"^channels\.editCreator"),
)
DESTRUCTIVE_WORDS = ("delete", "discard", "revoke", "terminate")
WRITE_WORDS = (
    "accept",
    "add",
    "cancel",
    "create",
    "edit",
    "forward",
    "import",
    "invite",
    "join",
    "leave",
    "mark",
    "read",
    "save",
    "send",
    "set",
    "start",
    "toggle",
    "update",
    "upload",
)


COMMAND_CATALOG: dict[str, dict[str, Any]] = {
    "account": {"commands": []},
    "audit": {"commands": ["list", "export", "prune"]},
    "auth": {"commands": ["request-code", "verify", "qr-login", "status", "logout"]},
    "batch": {"commands": ["run"]},
    "bots": {"commands": []},
    "capabilities": {"commands": ["list", "get"]},
    "chats": {"commands": []},
    "commands": {"commands": ["list", "get"]},
    "contacts": {"commands": ["list", "search", "resolve"]},
    "dialogs": {"commands": ["list", "get", "search"]},
    "drafts": {"commands": []},
    "folders": {"commands": []},
    "gifs": {"commands": []},
    "help": {"commands": []},
    "inbox": {"commands": ["list"]},
    "invite-links": {"commands": []},
    "join-requests": {"commands": []},
    "mcp": {
        "commands": [
            "serve",
            "pair",
            "clients list",
            "clients revoke",
            "tokens create",
            "tokens list",
            "tokens revoke",
        ]
    },
    "media": {"commands": ["download"]},
    "messages": {
        "commands": [
            "list",
            "get",
            "search",
            "context",
            "replies",
            "export",
            "send",
            "reply",
            "forward",
            "edit",
            "delete",
            "read",
            "react",
            "pin",
            "unpin",
        ]
    },
    "polls": {"commands": ["create", "vote", "close"]},
    "policy": {"commands": ["validate", "set", "get", "explain"]},
    "profiles": {"commands": ["create", "list", "get", "set-default", "remove"]},
    "raw": {"commands": ["invoke"]},
    "saved": {"commands": []},
    "scheduled": {"commands": ["list", "cancel"]},
    "schema": {"commands": ["list", "get", "export"]},
    "state": {"commands": ["get", "prune"]},
    "stickers": {"commands": []},
    "stories": {"commands": []},
    "topics": {"commands": ["list"]},
    "updates": {"commands": ["watch"]},
    "version": {"commands": []},
}

SCHEMA_MODELS = {
    cls.__name__: cls
    for cls in (
        Capability,
        CapabilityCatalog,
        AuditRecord,
        BatchOperation,
        Confirmation,
        Envelope,
        ErrorInfo,
        FeatureResult,
        JsonlRecord,
        LoginState,
        Meta,
        ProfileView,
        PolicyDocument,
        UpdateRecord,
    )
}


def method_name(request_class: type[TLRequest]) -> str:
    """Convert a generated Telethon request class to its canonical TL method name."""

    namespace = request_class.__module__.rsplit(".", maxsplit=1)[-1]
    class_name = request_class.__name__.removesuffix("Request")
    return f"{namespace}.{class_name[0].lower()}{class_name[1:]}"


def risk_for(method: str) -> Literal["read", "write", "destructive", "critical", "unknown"]:
    """Conservatively classify one raw method."""

    if any(pattern.search(method) for pattern in CRITICAL_PATTERNS):
        return "critical"
    leaf = method.split(".", maxsplit=1)[1]
    lowered = leaf.lower()
    if lowered.startswith(DESTRUCTIVE_WORDS):
        return "destructive"
    if leaf.startswith(("get", "search")):
        return "read"
    if lowered.startswith(WRITE_WORDS):
        return "write"
    return "unknown"


def request_registry() -> dict[str, type[TLRequest]]:
    """Return every request exposed by the installed Telethon layer."""

    registry: dict[str, type[TLRequest]] = {}
    for candidate in alltlobjects.tlobjects.values():
        if inspect.isclass(candidate) and issubclass(candidate, TLRequest):
            registry[method_name(candidate)] = candidate
    return dict(sorted(registry.items()))


def capability_catalog() -> CapabilityCatalog:
    """Generate the capability manifest from the live Telethon registry."""

    from clitg.features import FEATURE_COMMANDS
    from clitg.operations import OPERATIONS

    dedicated = {operation.method: operation.command for operation in OPERATIONS}
    features: dict[str, list[Any]] = {}
    for feature in FEATURE_COMMANDS:
        features.setdefault(feature.method, []).append(feature)
    high_level = {
        **{method: values[0].command for method, values in features.items()},
        **HIGH_LEVEL_METHODS,
        **dedicated,
    }
    capabilities: list[Capability] = []
    for method, request_class in request_registry().items():
        reason = UNSUPPORTED_METHODS.get(method)
        if reason:
            status = "unsupported"
        elif method in high_level:
            status = "high-level"
        else:
            status = "raw-only"
        commands = list(
            dict.fromkeys(
                [
                    *([high_level[method]] if method in high_level else []),
                    *(feature.command for feature in features.get(method, [])),
                ]
            )
        )
        feature_values = features.get(method, [])
        capabilities.append(
            Capability(
                method=method,
                python_class=f"{request_class.__module__}.{request_class.__name__}",
                status=status,
                risk=risk_for(method),
                command=high_level.get(method),
                commands=commands,
                reason=reason,
                requirements=sorted(
                    {
                        requirement
                        for feature in feature_values
                        for requirement in feature.requirements
                    }
                ),
                quota_consuming=any(feature.quota_consuming for feature in feature_values),
            )
        )
    return CapabilityCatalog(
        telethon_version=telethon.__version__,
        telegram_layer=alltlobjects.LAYER,
        generated_at=datetime.now(UTC),
        capabilities=capabilities,
    )


def command_catalog() -> dict[str, Any]:
    """Return the machine-readable top-level command catalog."""

    from clitg.features import feature_catalog
    from clitg.operations import operation_catalog

    operations = operation_catalog()
    operations.update(feature_catalog())
    groups = {name: dict(value) for name, value in COMMAND_CATALOG.items()}
    for command in operations:
        group, leaf = command.split(".", maxsplit=1)
        groups.setdefault(group, {"commands": []})["commands"].append(leaf)
    for value in groups.values():
        value["commands"] = sorted(set(value["commands"]))
    return {
        "schema_version": SCHEMA_VERSION,
        "cli_version": __version__,
        "global_options": {
            "profile": "string|null",
            "output": ["json", "jsonl"],
            "timeout_seconds": "positive integer",
            "verbose": "boolean",
        },
        "groups": groups,
        "operations": operations,
    }


def schema_catalog() -> dict[str, Any]:
    """Return JSON Schema documents for stable public models."""

    return {
        "schema_version": SCHEMA_VERSION,
        "models": {name: model.model_json_schema() for name, model in SCHEMA_MODELS.items()},
    }


def write_catalogs(root: Path) -> None:
    """Write deterministic checked-in catalogs."""

    root.mkdir(parents=True, exist_ok=True)
    capabilities = capability_catalog().model_dump(mode="json")
    capabilities["generated_at"] = "generated"
    (root / "capabilities.json").write_text(
        json.dumps(to_jsonable(capabilities), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (root / "schemas.json").write_text(
        json.dumps(schema_catalog(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    write_catalogs(Path("schemas"))
