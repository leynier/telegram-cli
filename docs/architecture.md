# Architecture

`clitg` separates the stable agent contract from Telegram's dynamic MTProto layer:

1. Typer parses explicit options and serializes operational results as JSON or JSONL.
2. `ClitgService` owns profile selection, policies, audit metadata, safety, idempotency, pagination,
   batches, checkpoints, and error translation.
3. `ProfileStore`, `CredentialStore`, and `StateStore` persist configuration, secret references,
   and content-free functional metadata.
4. `TelegramAdapter` owns Telethon clients, peer resolution, normalization, friendly operations,
   exports, and live updates.
5. `Operation` maps the original stable dedicated commands to generated Telegram requests.
6. `FeatureCommand` defines reviewed explicit options, requirements, quotas, builders, and result
   normalization for the 0.3 high-level catalog.
7. `RawCodec` reflects Telethon's generated TL registry for methods without dedicated commands and
   validates complex JSON constructors used by high-level commands.

The offline test suite replaces the adapter and never opens a Telegram connection. Telethon values
are normalized before they cross the service boundary unless `--include-raw` or `raw invoke` is
explicit.

## Profiles, credentials, and sessions

Profiles contain an API ID, optional phone, optional policy path, and an opaque API-hash reference.
`CredentialStore` prefers a usable non-interactive operating-system keyring and falls back to a
private `0600` file. Legacy inline API hashes are migrated automatically and the profile is updated
atomically. Public profile views expose only a storage classification.

Each profile has its own Telethon SQLite session. Sessions, API hashes, login codes, passwords,
auth keys, and Telegram content are outside audit storage and structured diagnostics.

## Policies and mutation safety

A profile can reference one versioned JSON policy. Command, peer, mutation-risk, raw-method, and
raw-risk allow and deny rules are evaluated before Telegram access. Deny rules take precedence.
Batch operation and target limits are also policy controlled.

Mutations have a reviewed risk: write, destructive, or critical. All support dry-run. Destructive
operations require an exact confirmation. Critical operations additionally require a short-lived,
one-use, payload-bound token. Compatible mutations use the same 30-day idempotency contract.

Repeating scheduled messages use reviewed named intervals which map to Telegram's permitted repeat
periods. A repeat requires a scheduled timestamp and exactly one message or media item. Albums and
multi-message forwards are rejected before Telegram access.

## High-level feature boundary

The feature catalog is declarative. Typer signatures are generated from explicit reviewed options,
while application builders convert stable agent inputs into Telegram constructors. Dedicated
builders cover AI transforms, transcription waiting, collaborative checklists, Business recipients
and schedules, shared folders, profile values, contacts, live stories, and sticker uploads.

Generated TL objects never cross the application boundary directly. High-level feature results use
the stable `FeatureResult` wrapper and recursively remove generated type discriminators. Agents can
request the serialized source value with `--include-raw` when the normalized result is insufficient.
Capabilities expose feature requirements such as Premium, Business, or administrator access and
identify quota-consuming AI calls.

Sticker files are validated locally and uploaded only at execution time. The adapter accepts
Telegram-ready PNG, WebP, TGS, and WebM assets and deliberately performs no conversion. A bounded
transcription wait listens for the matching completion update and returns a timeout marker without
turning a pending Telegram transcription into an operational error.

## State

The auxiliary SQLite database stores pending logins, idempotency results, critical confirmations,
update checkpoints, and content-free audit records. Audit records contain only command metadata,
never request payloads or message content. State pruning is explicit and confirmed.

## Streaming and exports

`updates watch` emits JSONL items followed by a terminal summary or error. Known Telegram updates
use stable event types, while other updates retain a normalized raw fallback. Optional consumer IDs
persist opaque cursors after successful emission.

Conversation export appends normalized JSONL messages and writes a manifest containing the next
cursor. Optional media downloads live below the export directory. Resume is explicit and an
existing export is never overwritten implicitly.

## MCP server

The optional `mcp` extra adds an MCP presentation layer beside Typer. `mcp serve` builds the SDK server, registers curated typed tools plus `clitg_run`, and executes every tool call as a `clitg` subprocess for the server's profile. The subprocess boundary keeps policies, confirmations, critical tokens, idempotency, audit records, and Telethon session handling identical to the terminal contract. A per-server lock serializes calls, and a bounded retry waits while another process holds the profile session.

Streamable HTTP runs statelessly with JSON responses behind uvicorn on a pre-bound socket. Host and Origin headers are restricted to loopback, the bound address, and the public origin. When the issuer is HTTPS or loopback the server acts as its own OAuth 2.1 authorization server with dynamic client registration, PKCE, single-use authorization codes, one-hour access tokens, and rotating 30-day refresh tokens bound to the resource URL. Authorization requests are approved on a pairing page with a single-use, rate-limited code issued by `mcp pair` or at startup. Elsewhere only static bearer tokens are accepted. `McpStore` keeps clients, pending requests, and SHA-256 hashes of every secret in a private SQLite file.

The optional NetBird tunnel supervises `netbird expose` and reads its public URL before the server starts, so the OAuth issuer always matches the address clients see.

## Generated contracts

`uv run python -m clitg.catalog` regenerates the checked-in capability and JSON Schema artifacts.
The generator enumerates every `TLRequest` in Telethon's installed layer, applies dedicated command
mappings and unsupported overrides, and assigns a conservative risk. Unknown methods remain
critical in execution.
