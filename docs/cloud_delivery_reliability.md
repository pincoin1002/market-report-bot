# Cloud scheduler and delivery recovery

The precision clock is external cron-job.org. GitHub native cron and Vercel
Hobby Cron are independent best-effort fallbacks, not minute-precision SLAs.
Vercel Hobby documents up to 59 minutes timing uncertainty, unsuitable as the
only clock for a 25-minute US-open window. No paid upgrade is required.

The dispatcher is isolated in cloud_scheduler, with zero dependencies and no
portfolio, Gemini, quotes, Telegram token, PIOS access or report generation.
Its only credential capability is workflow dispatch and receipt inspection.
It rejects missing auth, wrong DST slot and out-of-window calls, with bounded
network calls and no sleep/staging/retry loop. Reports enforce exchange calendars.

All report jobs share per-product concurrency across external/native/manual
triggers. Production delivery rechecks the latest main receipt, not the SHA
checked out before another run sent, and never treats a report artifact as proof
of delivery. A remote compare-and-create DELIVERING receipt is acquired before
Telegram. A confirmed API result requires positive message IDs; final receipt
stores HTTP/API confirmations without chat IDs or credentials.

Telegram has no transactional client idempotency key. If a send or final receipt
write is uncertain, the durable claim blocks automatic repeat delivery. Recover
only from the exact owner's trusted delivery artifacts/API-confirmed message IDs;
do not clear claims and resend on a generic workflow failure. This intentionally
trades unattended retry for duplicate prevention under ambiguous network failure.

The original dirty workspace and its Phase 3/PIOS work are not in this release.
PIOS remains separate from public-market validation and delivery. Vercel remains
an optional thin HTTP dispatcher, not a required research/report platform. An
external clock can instead dispatch GitHub directly using a repo-scoped token.
