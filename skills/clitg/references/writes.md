# Mutations

Use only the action and scope explicitly authorized by the user. Reuse prior authorization for the exact action; ask only if the prepared target, payload, or effect needs authorization that is missing. A preview or analysis request does not authorize execution.

Inspect `clitg --profile <profile> policy get`. Build the complete command with an explicit profile, target, payload, and scope, and add a stable `--idempotency-key` where supported. Run that exact command with `--dry-run`, verify its resolved target, risk, and normalized payload, then execute the authorized command without `--dry-run`. Preserve its payload and key.

For destructive actions, supply the exact `--confirm` value required by the command. For critical actions, use the payload-bound `confirmation_token` returned by dry-run; tokens expire after five minutes and one use. Obtain a fresh dry-run token if needed without changing the authorized action. An idempotent replay may return its stored result without consuming another token. Normal writes require dry-run but no confirmation token. Never weaken policy, risk, confirmation, token, or idempotency checks.

Use plain text unless formatting was requested; select `--parse-mode markdown` or `html` explicitly when needed. Prefer file or stdin sources for long content. Where structured input is required or chosen, use exactly one `--input` file or `--stdin` source containing JSON or JSONL. Never duplicate a field between flags and structured input.
