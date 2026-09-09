# PROGRESS

## 2026-09-10

- Final date-alignment hardening continued on branch `fix-adr-market-data-date-alignment` from recovered clean state `43fe19030ea6aabddd2ccbc752f8b7d00b5d72eb` in isolated checkout `/private/tmp/market-report-bot-date-alignment`; main was not touched.
- Hardened `calculate_tsm_adr_premium()` so identity failures return `INPUT_IDENTITY_MISMATCH` and temporal/freshness failures return `TEMPORAL_MISMATCH`; FX now requires a timezone-aware observation timestamp, rejects future observations, and applies the existing `max_fx_age_days` freshness window against explicit `report_as_of`.
- Added adversarial ADR regressions for VALID-labelled stale FX, future FX, timezone-naive FX, missing FX timestamp, wrong TSM identity, wrong 2330 identity, wrong FX identity, VALID-labelled identity/date mismatch, and the exact 1:5 ADR premium calculation.
- Local validation status: `.venv/bin/python -m unittest tests.test_temporal_integrity` PASS with 19 tests; `.venv/bin/python -m unittest tests.test_data_integrity` PASS with 50 tests; `.venv/bin/python -m unittest discover -s tests` PASS with 69 tests; `.venv/bin/python -m py_compile scripts/*.py` PASS; `scripts/dry_run_v2.py` PASS for all four report types; `git diff --check` PASS.
- Synthetic stale ADR regression report remains only under `tests/fixtures/synthetic_tw_open_stale_adr_rejected.md`; no matching synthetic/stale fixture file exists under production `reports/`.
- `scripts/validate_report.py` against existing committed production reports still reports `structured artifacts unavailable` because the matching transient `data/market_context.json` and `data/market_report_draft.json` artifacts are not present in the checkout; no fake structured artifacts were created to force this gate green.
- ADR hardening code commit `a6549005b165370453bb4169c75a21edd6c40267` passed GitHub Actions CI for both push run `34389997647` and PR run `34390001492` before this progress note.

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
- V2 implementation commit `25f1dbe` was pushed to `main` and manually verified with GitHub Actions `dry_run=true`: `tw-open` run `33168334449` success, `tw-close` run `33168334749` success, `us-open` run `33168335556` success, `us-close` run `33168334763` success.
- Production scheduled-delivery recommendation: safe to resume after confirming the GitHub Secrets remain valid; dry-run delivery validation is green and did not send live Telegram/email.
