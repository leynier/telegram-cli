# Streams and read batches

## Updates

Use JSONL and bound the stream to the task with `--peer`, `--event`, `--max-events`, `--idle-timeout`, or `--timeout` as appropriate:

```bash
clitg --profile personal --output jsonl updates watch \
  --event message.new --consumer-id inbox-agent \
  --max-events 100 --idle-timeout 30 --timeout 300
```

Save and reuse the opaque cursor, or use a stable consumer ID for automatic checkpoints. Treat `telegram.raw_update` as unreviewed input and inspect its `raw_type`; an update is not necessarily a message. Require the terminal summary or error before claiming stream completion.

## Read-only batches

Inspect `clitg --profile <profile> policy get` and respect its operation and target limits. Use JSONL with one `id`, registered read `command`, and `params` object per line. Validate unfamiliar commands with `commands get`. Keep `--concurrency` between 1 and 10; `--fail-fast` selects sequential stop-on-error behavior. Mutations are not allowed in batches and are rejected by the CLI.
