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

## 2026-08-27

- Implemented V2 data-integrity checkpoint for `market-report-bot`: deterministic instrument registry, dynamic portfolio quote universe, session-aware `QuoteObservation`, and separate `market_context_coverage` / `portfolio_quote_coverage`.
- Added explicit portfolio-critical fail-closed behavior: private portfolio advice requires 100% quote coverage and `VALID` observations; blocked advice now sends a short operational notice and writes non-sensitive `data/portfolio_advice_audit.json`.
- Fixed US timezone handling to use `America/New_York` and updated `us-open` scheduling to run both 13:00/14:00 UTC with a runtime duplicate guard for DST/standard-time.
- Changed report workflows to `generate-only → validate → deliver-existing`; critical price validation is no longer `continue-on-error` before delivery.
- Removed ungrounded current-news fallback by default; if Gemini Search grounding fails, reports degrade to verified-market-data-only output unless `ALLOW_UNGROUNDED_NEWS_FALLBACK=true`.
- Replaced forced daily portfolio trade language with Action Brief states: `NO_MATERIAL_CHANGE`, `WATCH`, `ACTION_REVIEW`, `DATA_BLOCKED`; exact buy sizing is blocked when cash is missing.
- Added `tests/test_data_integrity.py` covering GOOG/GOOGL identity, DRAM ETF identity, VOO/006208 dynamic portfolio universe, portfolio quote coverage, stale/missing quote blocks, cash-missing buy-size block, and sell-quantity-over-position block.

## 2026-08-28

- Completed V2 release implementation from the existing uncommitted checkpoint: added `MarketContext`, structured `MarketReportDraft`, structured `PortfolioActionBrief`, `PriceReference`, `Trigger`, `PortfolioContextProvider`, `EncryptedPortfolioProvider`, session engine, quote quality engine, small deterministic trigger engine, and delivery state/idempotency helpers.
- Added Yahoo chart extended-hours provider path for US equities/ETFs using timestamped minute observations with `includePrePost=true`; daily close providers remain fallback/reference and never relabel previous close as premarket.
- Replaced primary report validation with structured `quote_id` / instrument / session / value checks; LLM price extraction and generic 5% tolerance are no longer primary validation mechanisms.
- Public report V2 now renders a shorter high-signal structure with conditional optional modules; private portfolio holdings stay out of public reports.
- Private Action Brief V2 is deterministic monitoring output only: `NO_MATERIAL_CHANGE`, `WATCH`, `ACTION_REVIEW`, or `DATA_BLOCKED`; no automatic BUY/SELL/ADD/TRIM sizing.
- Local validation status: `python -m py_compile scripts/*.py` PASS; `python -m unittest discover -s tests -v` PASS with 50 tests; workflow YAML parse PASS; `scripts/dry_run_v2.py` PASS for `tw_open`, `tw_close`, `us_open`, `us_close`.
- Live provider smoke status: public quote fetch passed for `GOOG`, `DRAM`, `VOO`, `VTI` via `yahoo_chart_extended`, all `PREMARKET`, all `VALID`; full live fetch smoke passed for all four report types with 100% coverage in this environment.
- Release regression fixes after first GitHub dry-run: `generate-only` no longer skips structured artifact creation when same-day reports exist, rendered reports include every `PriceReference`, private portfolio quote gaps block only private Action Brief delivery, and duplicate same-day production sends are skipped at delivery.
- Documentation updated: `docs/DESIGN_SPEC.md` now states V2 authority model and PIOS boundary; `README.md` documents dual US-open cron and V2 fail-closed behavior.
- Remaining release gate after commit/push: manually dispatch all four GitHub Actions workflows with `dry_run: true`; production scheduled delivery should only resume after those workflow runs are green.
