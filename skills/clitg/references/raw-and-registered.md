# Structured registered actions and raw methods

## Registered actions

Some dedicated commands expose structured MTProto parameters. Inspect `clitg commands get --command <group.command>` first, then pass exactly one of `--params`, `--params-file`, or `--params-stdin`. The CLI resolves friendly peer, channel, and user strings. Use `_` for nested TL constructors required by the signature. Mutations follow [writes.md](writes.md).

## Raw methods

Use `raw invoke` only when no dedicated command exists. Inspect the method with `capabilities get`, including its risk, and build JSON parameters using `_` for TL constructors and `$peer`, `$channel`, `$user`, `$bytes`, `$datetime`, or `$upload` as appropriate.

Inspect the profile policy and pass `--allow-raw --dry-run` first. For mutations, follow [writes.md](writes.md); destructive execution also requires `--confirm <method>`. Critical or unknown execution requires the dry-run token with the exact same profile, method, and parameters. Unknown raw methods are critical; do not reinterpret their classification.
