# PROGRESS

## 2026-08-03

- Confirmed active repo is `/Users/chenpinxuan/Projects/03_market_report_bot`; `/Users/chenpinxuan/Projects/market-report-bot` is not present locally.
- Verified `AGENTS.md` exists and recent commit history is present.
- Checked GitHub Actions `us-close`: latest visible run `30411340108` / `#628` completed successfully on `2026-07-29T00:30:17Z`.
- GitHub summary shows artifacts were produced, with annotations: `Telegram send failed`, `extraction parse error`, and a Node.js 20 deprecation warning.
- Public API can read run/job status but not job logs (`403 Forbidden` for logs without authenticated GitHub session).
- Patched main Telegram report delivery to retry without Markdown parse mode when Markdown send fails.
- Reviewed portfolio/advice flow: encrypted `portfolio.json.enc`, `PORTFOLIO_KEY`, Telegram `/portfolio /buy /sell /cash`, and daily private advice are already wired.
- Current gap for Shane's goal: initialize/refresh actual holdings from screenshot without committing plaintext, then verify `/portfolio` and one daily report private advice run.
- Initialized encrypted portfolio from three brokerage screenshots: 6 TW positions, 10 US positions, cash unset.
- Verified temporary plaintext `portfolio.json` was removed and encrypted file decrypts/schema-validates locally with `.env` `PORTFOLIO_KEY`.

## 2026-08-04

- Investigated missing US open report: latest `us-open` run was `2026-07-29T13:00:23Z`; no runs after that.
- Root cause: report workflows only had `repository_dispatch` / `workflow_dispatch`; GitHub-native `schedule` triggers had been removed, leaving delivery dependent on local/external dispatch.
- Restored GitHub Actions cron schedules for all four report workflows so the system is cloud-scheduled again.
