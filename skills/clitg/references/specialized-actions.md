# Specialized actions

Inspect `commands get --command <group.command>` for current flags, allowed values, requirements, and `quota_consuming`. For mutations, follow [writes.md](writes.md).

## AI transforms

AI commands return a result; sending or editing it requires authorization for that mutation, which may already be part of the user's request. Treat `quota_consuming: true` as potentially metered and establish authorization for quota use before execution; dry-run a quota-consuming call before its first execution when that use has not already been authorized. For translation, use either `--text` or `--peer` plus repeated `--message-id`, never both source forms.

## Business

Working hours use repeated `--open DAY:HH:MM-HH:MM`, with Monday as day 0 and Sunday as day 6. Custom away schedules require both `--start-at` and `--end-at`. Inspect the command contract for schedule modes, recipient scopes, and connected-bot rights instead of inferring them.

## Stickers

Provide Telegram-ready PNG, WebP, TGS, or WebM assets; the CLI does not convert them. PNG and WebP are limited to 512 KB; TGS to 64 KB, 512 by 512 pixels, and three seconds; WebM to 256 KB. For `create-set`, repeat `--file` and provide either one `--emoji` for every file or one per file. `stickers add` accepts exactly one file.

## Schedules and repeats

Use a named `--repeat` value with `--schedule-at`; discover allowed repeat values from the command contract. The timestamp must be RFC 3339 with an offset, such as `2026-07-21T15:00:00Z`. Repeating delivery supports one text or media message, one forwarded message, or one scheduled edit; repeating albums are unsupported. Preview and execute the same payload and idempotency key, including the schedule and recurrence.
