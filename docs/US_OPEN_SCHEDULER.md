# US Open external scheduler

Production uses two Vercel Cron candidates at `12:55 UTC` and `13:55 UTC` on
weekdays. The server-side handler resolves `America/New_York` itself, so only
the slot that is exactly 08:55 in the active DST offset dispatches GitHub.
It starts the Actions workflow ten minutes before the sole 09:05 premarket
snapshot intent; the workflow waits only until that intent and refuses data at
or after 09:30.

Set these **Production-only** Vercel Environment Variables in the Vercel
dashboard; do not put any values in this repository:

- `CRON_SECRET`: a high-entropy secret used to verify Vercel Cron requests.
- `GITHUB_WORKFLOW_DISPATCH_TOKEN`: a fine-grained GitHub token limited to
  `pincoin1002/market-report-bot`, with only the permission required to invoke
  the `us-open.yml` workflow dispatch endpoint.

The GitHub workflow receives `trigger_source`, intended NYSE date/time, and
the scheduler UTC timestamp. It serializes all US-open triggers, uses
`us_open:<NYSE-date>` as the canonical key, and records a privacy-safe terminal
status artifact. The native GitHub schedule remains a backup, not the primary
clock.
