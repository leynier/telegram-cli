# clitg: Telegram CLI for AI agents

Give an AI agent a deterministic interface to a real Telegram user account. `clitg` reads inboxes,
searches conversations, sends and manages messages, uses Telegram AI, automates Business features,
works with groups, channels, contacts, collaborative checklists, stories, polls, topics, folders,
stickers, GIFs, and media, and exposes the wider MTProto API through structured JSON.

`clitg` is designed for workflows powered by **Claude Code**, **OpenAI Codex**, **Google
Antigravity**, **GitHub Copilot**, **xAI Grok**, **Cursor**, **Amp**, and every other AI agent that can
run a command and consume JSON or JSONL. It is an unofficial Telegram client for user accounts,
powered by Telethon. It is not a Bot API wrapper and does not use a BotFather token.

## Why agents use clitg

- **No prompts:** every value comes from an option, environment variable, file, or explicitly
  selected stdin.
- **Structured contracts:** successes and failures use versioned JSON envelopes. Lists and live
  updates support JSONL.
- **Broad Telegram coverage:** 153 agent-friendly commands added in 0.3 cover 133 Telegram methods,
  while the generated MTProto gateway provides a conservative escape hatch for the rest.
- **Safety for real accounts:** dry-run previews, exact destructive confirmations, one-use critical
  tokens, local policies, idempotency keys, and content-free audit metadata.
- **Automation primitives:** filtered inboxes, account-wide search, resumable exports, update
  checkpoints, and bounded read-only batches.
- **Agent discovery:** machine-readable command, schema, capability, error, cursor, risk, and skill
  metadata are part of the product.
- **MCP server:** serve the same commands to ChatGPT, Claude, Codex, Cursor, and other MCP clients over stdio or Streamable HTTP with OAuth pairing or bearer tokens.

Build inbox assistants, Telegram research agents, translation and summarization workflows,
Business automations, personal knowledge capture, collaborative follow-up systems, recurring
delivery, moderation tools, story workflows, sticker pipelines, or your own account automation
without inventing another protocol.

> [!IMPORTANT]
> A Telethon session grants access to its Telegram account. Never commit, upload, or share session
> files. `clitg` stores sessions and credentials outside the repository with restrictive
> permissions.

## Install

Install [uv](https://docs.astral.sh/uv/) and then install the published CLI:

```bash
uv tool install clitg
clitg version
```

`clitg` 0.4 requires Python 3.14. uv can install the compatible interpreter automatically. The
public JSON contract remains on schema 0.2 for compatibility with existing agents.

For local development:

```bash
git clone https://github.com/leynier/telegram-cli.git
cd telegram-cli
uv sync --all-groups
uv run clitg version
```

The project metadata and lockfile are managed with `uv init`, `uv add`, `uv remove`, and `uv lock`.

## Install the Agent Skill

The repository ships an Agent Skill that teaches compatible agents how to operate `clitg` safely.
Install it with the npm or Bun runner:

```bash
npx skills add leynier/telegram-cli --skill clitg
bunx skills add leynier/telegram-cli --skill clitg
```

Target an agent and install globally without prompts when desired:

```bash
npx skills add leynier/telegram-cli --skill clitg --agent claude-code -g -y
bunx skills add leynier/telegram-cli --skill clitg --agent codex -g -y
```

The open [`skills` CLI](https://github.com/vercel-labs/skills) supports Claude Code, Codex,
Antigravity, GitHub Copilot, Cursor, Amp, and other agents. Grok and any shell-capable agent can use
the same CLI contract even if it loads skills through another mechanism. The skill does not install
the executable. If `clitg` is missing, it tells the operator to run `uv tool install clitg`.

## Get a Telegram App ID and API hash

Telegram user-account clients need an application identity. This is different from a Telegram bot
token.

1. Open Telegram's official portal at [my.telegram.org/apps](https://my.telegram.org/apps).
2. Enter the account phone number in international format.
3. Enter the confirmation code delivered inside the Telegram app.
4. Open **API development tools**.
5. Create an application with a title, short name, and platform.
6. Copy **App api_id** and **App api_hash** into local environment variables.

```bash
export CLITG_API_ID='12345678'
export CLITG_API_HASH='your-private-api-hash'
export CLITG_PHONE='+15551234567'
```

Treat the API hash as a secret. Never paste it into an issue, log it, or commit it. Create a profile
without placing the hash in the process arguments:

```bash
clitg profiles create --name personal --default
```

The API hash is stored in the operating-system keyring when a usable non-interactive backend is
available. Otherwise, `clitg` uses a private file with mode `0600`. Existing 0.1 profiles containing
an inline API hash are migrated automatically and atomically the next time they are used.
`profiles list` and `profiles get` expose only the storage classification, never the secret.

Values can come from explicit options, secret files, or `CLITG_API_ID`, `CLITG_API_HASH`,
`CLITG_PHONE`, `CLITG_CODE`, and `CLITG_PASSWORD`. Explicit options take precedence. Login codes and
2FA passwords are never persisted.

## Authenticate without prompts

Use the resumable phone-code flow:

```bash
clitg --profile personal auth request-code --phone +15551234567
clitg --profile personal auth verify \
  --login-id '<login-id>' \
  --code-file ./telegram-code.txt \
  --password-file ./telegram-2fa.txt
clitg --profile personal auth status
```

Telegram normally delivers the code inside its app. The password file is needed only when the
account has two-step verification enabled. Delete temporary secret files after use.

Alternatively, create a QR image and wait for the Telegram mobile app to scan it:

```bash
clitg --profile personal auth qr-login \
  --qr-output ./telegram-login.png \
  --timeout 120
```

Open Telegram on the phone, go to **Settings**, **Devices**, **Link Desktop Device**, then scan the
generated image before the timeout. The command remains non-interactive.

## Structured output contract

Every normal operation writes one envelope to stdout:

```json
{
  "schema_version": "0.2",
  "ok": true,
  "data": {},
  "error": null,
  "meta": {
    "command": "messages.list",
    "profile": "personal",
    "request_id": "...",
    "next_cursor": null
  }
}
```

Failures use the same envelope and a nonzero exit code. `--output jsonl` emits `item` records and
ends with `summary` or `error`. Operational errors stay on stdout. Redacted diagnostics use stderr
only when `--verbose` is explicit.

`clitg help` and `clitg --help` are equivalent. `clitg version` and `clitg --version` are also
equivalent. Agents can discover contracts with:

```bash
clitg --help-json
clitg commands list
clitg commands get --command stories.publish
clitg schema list
clitg capabilities get --method stories.sendStory
```

Global options precede the command group:

```bash
clitg --profile personal --output jsonl messages list --peer @example --limit 25
```

Opaque cursors can be returned in `meta.next_cursor`. Pass them back unchanged with `--cursor`.
Listing, searching, exporting, and inspecting context never mark messages read.

Exit codes are stable within schema 0.2:

| Code | Meaning |
| ---: | --- |
| 0 | Success |
| 1 | Internal error |
| 2 | Invalid input or CLI usage |
| 3 | Profile or authentication error |
| 4 | Missing or ambiguous entity |
| 5 | Conflict or missing confirmation |
| 6 | Telegram permission failure |
| 7 | Telegram rate limit |
| 8 | Telegram RPC or network failure |

## Inbox, search, context, and export

Read a filtered unread inbox as messages or dialog summaries:

```bash
clitg --profile personal inbox list \
  --view messages \
  --peer @example \
  --from @alice \
  --folder-id 0 \
  --after 2026-07-01T00:00:00Z \
  --before 2026-08-01T00:00:00Z \
  --media-only
```

Search one conversation or the entire account. Omit `--peer` for global search:

```bash
clitg --profile personal messages search --peer @example --query invoice
clitg --profile personal messages search \
  --query invoice \
  --from @alice \
  --after 2026-01-01T00:00:00Z \
  --media-only
```

Recover surrounding context, replies, or a resumable export:

```bash
clitg --profile personal messages context --peer @example --message-id 123 --before 10 --after 10
clitg --profile personal messages replies --peer @example --message-id 123
clitg --profile personal messages export --peer @example --output ./exports/example
clitg --profile personal messages export --peer @example --output ./exports/example --resume
```

Add `--download-media` to export media into the export directory. Existing exports require
`--resume`; an accidental overwrite is rejected.

## Send and mutate safely

Plain text is the default. Markdown or HTML must be explicit:

```bash
clitg --profile personal messages send \
  --peer me \
  --text '**Status:** complete' \
  --parse-mode markdown \
  --idempotency-key job-42 \
  --dry-run

clitg --profile personal messages send \
  --peer me \
  --text '**Status:** complete' \
  --parse-mode markdown \
  --idempotency-key job-42
```

Text and JSON accept a literal value, a file, or explicitly selected stdin as mutually exclusive
sources. Repeat `--file` for albums. Use `--media-kind voice`, `sticker`, or `document` when needed.
`--schedule-at` requires RFC 3339 with an offset. Add `--repeat daily`, `weekly`, `biweekly`,
`monthly`, `quarterly`, `semiannual`, or `yearly` to create a repeating scheduled text, single-file
message, single-message forward, or scheduled edit. Repetition always requires `--schedule-at` and
albums cannot repeat.

```bash
clitg --profile personal messages send \
  --peer me \
  --text 'Weekly review' \
  --schedule-at 2026-08-03T09:00:00-06:00 \
  --repeat weekly \
  --idempotency-key weekly-review \
  --dry-run
```

Every compatible mutation accepts `--idempotency-key`. Reusing the same key and payload returns the
stored result with `idempotent_replay: true`. Changing the payload under an existing key is a
conflict. Records expire after 30 days.

Destructive operations require the exact confirmation:

```bash
clitg --profile personal messages delete \
  --peer @example \
  --message-id 123 \
  --scope everyone \
  --dry-run

clitg --profile personal messages delete \
  --peer @example \
  --message-id 123 \
  --scope everyone \
  --confirm messages.delete \
  --idempotency-key delete-123
```

Critical operations also require the payload-bound token returned by dry-run. Tokens are single
use and expire after five minutes.

## Telegram AI and collaborative work

Translate messages or supplied text, request Telegram transcription with an optional bounded wait,
rate transcriptions, summarize a message, or transform supplied text with Telegram AI. AI commands
return Telegram's result only. They never send or edit the transformed text automatically.

```bash
clitg --profile personal messages translate --text 'Hola mundo' --to-lang en
clitg --profile personal messages transcribe --peer @example --message-id 42 --wait-seconds 30
clitg --profile personal messages summarize --peer @example --message-id 42 --to-lang en
clitg --profile personal messages compose --text 'ship it today' --proofread
```

Manage saved AI tones with `ai-tones`, and create or update collaborative checklists with `todos`:

```bash
clitg --profile personal todos create \
  --peer me \
  --title 'Release 0.3' \
  --item 'Run quality gates' \
  --item 'Publish the release' \
  --dry-run
```

Telegram may require Premium for AI tones and collaborative checklists, and may meter AI features.
The command manifest exposes `requirements` and `quota_consuming` so agents can reason about those
constraints before execution.

## Business automation and focused inboxes

Telegram Business accounts can manage quick replies, chat links, greeting and away messages,
working hours, location, introduction, and connected bots. Each mutation supports dry-run and the
same confirmation and idempotency model as the rest of the CLI. Business hours use
`DAY:HH:MM-HH:MM`, where day 0 is Monday and day 6 is Sunday.

```bash
clitg --profile personal quick-replies create --name welcome --text 'Thanks for writing' --dry-run
clitg --profile personal business hours-set \
  --timezone America/Mexico_City \
  --open '0:09:00-17:00' \
  --open '1:09:00-17:00' \
  --dry-run
clitg --profile personal business links
```

Focused inbox commands list unread mentions, reactions, and poll votes without marking messages
read. Search-calendar and search-counter commands expose Telegram's media-oriented search views.

```bash
clitg --profile personal inbox mentions --peer @example --limit 50
clitg --profile personal inbox reactions --peer @example --limit 50
clitg --profile personal messages search-calendar \
  --peer @example \
  --filter InputMessagesFilterPhotos
```

## Statistics, moderation, folders, and stories

Admin workflows can inspect channel, group, message, story, poll, public-forward, and graph
statistics. Reviewed critical commands cover anti-spam, slow mode, default permissions, and member
ranks. Reporting commands, join-request bulk actions, participant-reaction cleanup, and spam reports
are explicit mutations and retain the normal safety checks.

Shared-folder commands create, inspect, edit, revoke, join, update, dismiss, and leave Telegram chat
list invitations. Story commands cover archive and pinned views, viewers, reactions, albums, links,
visibility, stealth mode, read acknowledgement, and live-story state control.

```bash
clitg --profile personal stats channel --channel @example
clitg --profile personal folders shared-links --folder-id 2
clitg --profile personal stories viewers --peer @example --story-id 7 --limit 100
```

## Profile, contacts, stickers, and GIFs

Profile commands edit names and bio, upload or delete profile photos, manage birthdays, colors,
emoji status, personal channels, and profile music. Contact commands support batch import, birthdays,
top peers, private notes, username resolution, and phone resolution.

Sticker workflows list, inspect, install, favorite, create, add, edit, move, replace, remove, rename,
set thumbnails, delete, search by emoji, and inspect available reactions. Uploads accept
Telegram-ready PNG, WebP, TGS, or WebM files. `clitg` validates signatures, sizes, and TGS dimensions
and duration, but does not convert source assets. PNG and WebP are limited to 512 KB, TGS to 64 KB,
512 by 512 pixels, and three seconds, and WebM to 256 KB.

Complex Telegram constructors can be supplied through exactly one `--input` JSON or `--stdin` JSON
or JSONL source. Dedicated scalar fields remain explicit flags, and duplicate fields are rejected.

```bash
clitg --profile personal stickers create-set \
  --title 'Agent Pack' \
  --short-name agent_pack_by_example \
  --file ./agent.webp \
  --emoji '🤖' \
  --dry-run
```

## Dedicated Telegram actions

The 0.3 command registry retains all 0.2 actions and adds the high-level families above. Existing
stable actions also cover:

- account details, privacy rules, and active authorization sessions;
- bot starts, callbacks, and inline queries;
- group and channel creation, editing, descriptions, photos, usernames, membership, administration,
  restrictions, invites, joins, leaves, participants, and admin logs;
- contact creation, deletion, blocking, and unblocking;
- dialog archive, pin, mute, unread, draft, and folder organization;
- GIFs, stickers, contact cards, locations, venues, and live locations;
- invite links and join request moderation;
- reactions, polls, Saved Messages, scheduled messages, stories, and forum topics.

Registered actions use a stable command name and structured MTProto parameters. Inspect the exact
signature, risk, and method before constructing a payload:

```bash
clitg commands get --command chats.create-channel
clitg --profile personal chats create-channel \
  --params '{"title":"Agent Updates","about":"Generated by clitg","broadcast":true,"megagroup":false}' \
  --dry-run
```

Friendly strings in peer, channel, and user fields are resolved safely. TL constructors still use
the `_` discriminator when a nested generated type is required.

Nearby contacts, takeout export, secret chats, payments, account-security mutations, session
revocation, and mutation batches are intentionally excluded from dedicated 0.3 commands.

## Live updates with JSONL

Stream normalized message events and preserve a checkpoint by consumer ID:

```bash
clitg --profile personal --output jsonl updates watch \
  --event message.new \
  --event message.edited \
  --peer @example \
  --consumer-id inbox-agent \
  --max-events 100 \
  --idle-timeout 30 \
  --timeout 300 \
  --heartbeat 15
```

Known updates use stable event types. Other Telegram updates are retained as
`telegram.raw_update`. Each item carries an opaque cursor. A consumer checkpoint is updated only
after an item is emitted.

## MCP server

`clitg` can serve its commands to Model Context Protocol clients such as ChatGPT, Claude Code, Codex, and Cursor. Install the optional `mcp` extra:

```bash
uv tool install 'clitg[mcp]'
```

The server exposes 15 tools: typed tools for dialogs, contact resolution, the unread inbox, and listing, getting, searching, reading context, replying to, sending, and marking messages read; `clitg_commands` and `clitg_command` for discovery; and `clitg_run`, which executes any other command from its CLI arguments. Every tool call runs the normal CLI for the server's profile, so local policies, dry runs, exact confirmations, critical tokens, idempotency keys, and content-free audit records behave exactly as in a terminal. The `mcp` command group itself is not reachable through MCP.

> [!WARNING]
> A connected MCP client has the same authority as the CLI on that profile, including reading local files for uploads and writing exports or downloads on the server host. Attach a [local policy](#local-authorization-policy) that limits commands, peers, and mutation risks before exposing a profile to a remote client.

### Local clients over stdio

MCP clients that spawn local processes run the server over stdio:

```json
{
  "mcpServers": {
    "telegram": {
      "command": "clitg",
      "args": ["--profile", "personal", "mcp", "serve"]
    }
  }
}
```

In stdio mode stdout carries only MCP protocol messages and no authentication is involved.

### Streamable HTTP

```bash
clitg --profile personal --output jsonl mcp serve --transport http --port 8765
```

HTTP mode is a streaming command and requires `--output jsonl`, like `updates watch`. It listens on `127.0.0.1` by default and emits `server.started`, `pairing.code`, `tunnel.started`, and `server.stopped` items followed by a terminal summary. Stop it with Ctrl+C. The server serializes tool calls for its profile and waits up to `--lock-wait` seconds, 15 by default, when another `clitg` process is using the same Telegram session.

The server offers two authentication methods:

- **OAuth 2.1** with authorization code, PKCE, dynamic client registration, and rotating refresh tokens. It is available when the issuer is an HTTPS `--public-url`, a tunnel URL, or a loopback address. ChatGPT uses this method.
- **Static bearer tokens** for clients that send an `Authorization: Bearer` header. On a non-loopback address without a public HTTPS origin, such as a NetBird or Tailscale IP, only static tokens are accepted.

```bash
clitg --profile personal mcp tokens create --name laptop --token-file ./laptop-token.txt
clitg --profile personal mcp tokens list
clitg --profile personal mcp tokens revoke --name laptop --confirm mcp.tokens.revoke
```

Without `--token-file` the token is printed once in the JSON result. Tokens, refresh tokens, authorization codes, and pairing codes are stored only as SHA-256 hashes in a private `mcp.sqlite3` file next to the other local state.

### Pair ChatGPT

1. Expose the server through a public HTTPS origin and start it with that origin as `--public-url`, or use `--tunnel netbird`.
2. In ChatGPT, enable developer mode, create a connector for `https://<origin>/mcp`, and choose OAuth authentication.
3. ChatGPT registers itself and opens the clitg pairing page, which names the client and the address it returns to.
4. Enter the pairing code and approve. ChatGPT then refreshes its access automatically.

Pairing codes have eight characters, work once, and expire after 10 minutes. When no OAuth client has been paired yet, `serve` issues one at startup and delivers it according to `--pair-delivery`: `both` by default, which shows it in the `pairing.code` event and sends it to your Saved Messages, or `terminal`, `saved-messages`, or `none`. Issue another code at any time:

```bash
clitg --profile personal mcp pair --deliver both
```

Each authorization request accepts five attempts, and 10 failed attempts within 15 minutes lock pairing for the profile temporarily. Inspect or revoke paired clients:

```bash
clitg --profile personal mcp clients list
clitg --profile personal mcp clients revoke --client-id '<client-id>' --confirm mcp.clients.revoke
```

### Expose the server with NetBird

Any HTTPS reverse proxy or tunnel works with `--public-url`. NetBird is supported directly, both on NetBird Cloud and on self-hosted deployments with the reverse proxy enabled.

For ChatGPT, prefer a persistent NetBird reverse proxy service so the connector URL never changes. In the NetBird dashboard, open **Reverse Proxy**, **Services**, and **Add Service**, target this peer's NetBird address on port 8765 over HTTP, and leave PIN, password, and SSO protection disabled because ChatGPT cannot pass them; the clitg OAuth pairing protects the server. Then start the server on the NetBird address:

```bash
clitg --profile personal --output jsonl mcp serve \
  --transport http \
  --host netbird \
  --public-url https://telegram.example.com
```

`--host netbird` binds to this peer's NetBird IPv4 address as reported by `netbird status --json`.

For quick tests, `--tunnel netbird` runs `netbird expose` for the server's port, binds to the NetBird address, uses the generated public URL, and removes it when the server stops. It requires NetBird 0.66 or later, **Peer Expose** enabled under **Settings**, **Clients** for a peer group that contains this peer, and **Block Inbound Connections** disabled on the peer. The URL changes on every run, so a ChatGPT connector must be created again each time.

Self-hosted NetBird needs a proxy cluster and a verified domain under **Reverse Proxy**, **Clusters** and **Custom Domains** before services can be published. More tunnel providers are tracked in [#26](https://github.com/leynier/telegram-cli/issues/26), and running the server as a background service or in containers in [#27](https://github.com/leynier/telegram-cli/issues/27).

## Local authorization policy

Attach a versioned JSON policy to a profile to constrain agents locally. Deny rules always take
precedence over allow rules.

```json
{
  "policy_version": "0.1",
  "allow_commands": ["dialogs.*", "inbox.*", "messages.*", "account.get"],
  "deny_commands": ["messages.delete"],
  "allow_peers": ["me", "@trusted_*"],
  "deny_peers": ["@blocked"],
  "allow_mutation_risks": ["write"],
  "allow_raw_methods": ["help.*"],
  "deny_raw_methods": [],
  "allow_raw_risks": ["read"],
  "max_operations": 100,
  "max_targets": 25
}
```

```bash
clitg policy validate --file examples/policy.json
clitg policy set --name personal --file examples/policy.json
clitg --profile personal policy get
clitg --profile personal policy explain --command messages.send --risk write --peer me
```

Clear the attached policy with `clitg policy set --name personal`.

## Read-only batch execution

Batch input is JSONL. Only registered read operations are accepted, concurrency is bounded from 1
to 10, and output order matches input order.

```jsonl
{"id":"account","command":"account.get","params":{"id":{"_":"InputUserSelf"}}}
{"id":"sessions","command":"auth.sessions","params":{}}
```

```bash
clitg --profile personal batch run --input examples/read-batch.jsonl --concurrency 2
```

Use `--fail-fast` for sequential stop-on-error behavior. Policies can cap operation and target
counts. Mutation batches are rejected.

## Content-free audit metadata

Operational commands record timestamp, profile name, command, request ID, optional target, success,
and error code. Message text, raw parameters, API hashes, codes, passwords, auth keys, and session
material are never recorded.

```bash
clitg audit list --limit 100
clitg audit export --output ./audit.jsonl
clitg audit prune --before 2026-07-01T00:00:00Z --dry-run
clitg audit prune --before 2026-07-01T00:00:00Z --confirm audit.prune
```

## Raw MTProto gateway

Prefer a dedicated command. When one does not exist, inspect the method first:

```bash
clitg capabilities get --method help.getConfig
clitg --profile personal raw invoke \
  --method help.getConfig \
  --params '{}' \
  --allow-raw \
  --dry-run
```

Special JSON values include `$peer`, `$channel`, `$user`, `$bytes`, `$datetime`, and `$upload`:

```json
{
  "peer": {"$peer": "@example"},
  "offset_id": 0,
  "offset_date": null,
  "add_offset": 0,
  "limit": 10,
  "max_id": 0,
  "min_id": 0,
  "hash": 0
}
```

All raw calls require `--allow-raw`. Destructive calls also require `--confirm <method>`. Critical
or unclassified calls require a dry-run token bound to the profile, method, and payload. Unknown raw
methods remain critical until reviewed.

The generated `schemas/capabilities.json` classifies every request in the installed Telethon layer.
`schemas/schemas.json` contains public JSON Schemas and the command manifest.

## Development quality gates

```bash
uv lock --check
uv run ruff format --check .
uv run ruff check .
uv run ty check
uv run pytest --cov=clitg --cov-branch --cov-fail-under=100
uv run python scripts/check_skill.py
uv run python -m clitg.catalog
uv build --no-sources
```

`make check` runs Ruff formatting, Ruff lint, and ty. `make tests` runs those checks plus the full
coverage gate. The normal suite is offline and deterministic. A separate protected workflow runs a
minimal send, edit, and delete lifecycle against Telegram Test DC when its credentials are present.

## Terms and privacy

`clitg` is an unofficial third-party client and is not affiliated with Telegram. Operators are
responsible for the [Telegram API Terms of Service](https://core.telegram.org/api/terms), including
privacy, consent, branding, automation, and content or AI restrictions. The CLI does not bypass
read receipts, rate limits, permissions, or Telegram behavior.

## License

MIT. See [LICENSE](LICENSE).
