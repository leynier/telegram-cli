# MCP server

Starting `clitg mcp serve`, exposing it beyond loopback, issuing pairing codes, and creating or revoking MCP tokens or clients change who can operate the account; do them only with explicit authorization. They require the `mcp` extra: `uv tool install 'clitg[mcp]'`.

Local clients spawn `clitg --profile <profile> mcp serve`, which speaks MCP over stdio. HTTP mode is a JSONL stream: `clitg --profile <profile> --output jsonl mcp serve --transport http`. Read `server.started` for `url`, `issuer`, and `auth`; a `pairing.code` item carries a single-use code only when terminal delivery was selected. Stop the process to end the stream and expect a terminal summary.

Remote OAuth clients such as ChatGPT need an HTTPS origin through `--public-url` or `--tunnel netbird`. Pair with `clitg --profile <profile> mcp pair --deliver terminal|saved-messages|both`; never paste a pairing code anywhere except the clitg pairing page the user opened. Header-based clients use `mcp tokens create --name <name> --token-file <new-file>`; prefer the file so the token never enters the transcript.

Revoke with exact confirmations after a dry run: `mcp clients revoke --client-id <id> --confirm mcp.clients.revoke` and `mcp tokens revoke --name <name> --confirm mcp.tokens.revoke`.

Inside an MCP session, tool results are clitg envelopes. Preview mutations with `dry_run`, pass `--confirm` and `--confirmation-token` through `clitg_run` arguments for destructive and critical commands, and treat `conflict` after the lock wait as a busy profile session.
