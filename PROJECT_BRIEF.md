# Market Report Bot — Project Brief

> **Purpose of this document**: Give any AI reviewer (ChatGPT, Claude, Gemini, etc.)
> enough context to understand this project and evaluate report quality without
> additional explanation from the user.

---

## 1. What This System Does

An automated, institutional-grade market report pipeline running entirely on **GitHub Actions**.
Every trading day it generates four Traditional Chinese financial reports and pushes them to
**Telegram** and **Email**.

No local machine is needed. Everything runs in the cloud on a scheduled cron.

---

## 2. Four Report Types

| Report | 台灣名稱 | Schedule (TPE) | Audience Purpose |
|--------|---------|---------------|-----------------|
| `tw_open` | 台股開盤戰報 | Mon–Fri 07:50 | Pre-open battle plan for TAIEX |
| `tw_close` | 台股收盤日報 | Mon–Fri 16:30 | End-of-day debrief + 三大法人 |
| `us_open` | 美股開盤日報 | Mon–Fri 20:30 EDT | Pre-open setup for US markets |
| `us_close` | 美股收盤日報 | Mon–Fri 05:00+1 | US overnight debrief for Taiwan readers |

---

## 3. System Architecture

```
GitHub Actions (cron)
       │
       ▼
scripts/fetch_market_data.py
  └─ yfinance pulls: TW stocks (.TW), US indices (^GSPC ^NDX ^VIX ^TNX),
     DXY, BTC, TSM ADR, NVDA, USD/TWD
  └─ saves: data/market_snapshot.json
       │
       ▼
scripts/generate_report.py
  └─ reads prompt from prompts/{report_type}.md
  └─ injects market_snapshot as inviolable preamble
  └─ calls Gemini API (gemini-2.0-flash, temperature=0.1)
     with Google Search grounding (live web search)
  └─ max_output_tokens: tw_open/tw_close/us_open=16000, us_close=24000
  └─ saves: reports/{report_type}_YYYYMMDD_HHMMSS.md
  └─ sends via Telegram (chunked at 4096 chars) + Email (HTML)
       │
       ▼
scripts/validate_report.py
  └─ check_structure(): verifies all required section anchors present,
     detects truncation, counts ⚠️ 未取得 occurrences
  └─ extract_prices(): second Gemini call (JSON mode, temp=0) extracts
     every explicitly stated price from the report
  └─ compare(): checks extracted prices vs market_snapshot within 5% tolerance
  └─ saves: data/report_summary.json, data/validation_results.json
  └─ exit 1 if truncated OR any price deviation > 5%
       │
       ▼
scripts/prepare_review.py
  └─ bundles report + snapshot + validation_results into
     data/review_package_{type}_{datetime}.md
  └─ uploaded as GitHub Actions artifact (retained 30 days)
```

---

## 4. Report Structure per Type

### tw_open (台股開盤戰報) — 19 required sections
| § | Title |
|---|-------|
| 1 | 今日一句話 |
| 2 | 美股映射（NASDAQ/S&P500/Dow/SOX → TAIEX impact） |
| 3 | ADR 表現（TSM, others → today's expectation） |
| 4 | 台指期夜盤（futures close, spread, forecast） |
| 5 | 外資期貨未平倉（net long/short, change） |
| 6 | USD/TWD（rate, NDF direction, capital flow impact） |
| 7 | AI 供應鏈觀察（CoWoS / AI Server / PCB / 散熱 / 電源 / 光通訊 / ASIC / HBM / 伺服器 / 電力） |
| 8 | 權值股觀察（2330 台積電, 2317 鴻海, 2454 聯發科, 2308 台達電, 2382 廣達, 2303 聯電） |
| 9 | 強勢族群 |
| 10 | 弱勢族群 |
| 11 | 今日市場主線 |
| 12 | 資金流向（外資/投信/自營/融資餘額） |
| 13 | 重要新聞 |
| 14 | 今日風險 |
| 15 | 今日交易觀察名單 |
| — | Tomorrow Key Signals |
| — | Risk Matrix |
| — | Rotation Assessment |
| — | AI Trend Health Check |

### tw_close (台股收盤日報) — 19 required sections
§1 一句話 → §2 指數概況 → §3 盤中走勢復盤 → §4 三大法人 →
§5 USD/TWD → §6 權值股 → §7 AI供應鏈 → §8 強勢族群 → §9 弱勢族群 →
§10 市場主線 → §11 技術面 → §12 重要新聞 → §13 隔日交易計畫 →
§14 風險矩陣 → §15 最終結論 → Tomorrow Key Signals → Risk Matrix →
Rotation Assessment → AI Trend Health Check

### us_open (美股開盤日報) — 19 required sections
§1 一句話 → §2 指數期貨 → §3 全球市場 → §4 宏觀利率 →
§5 FedWatch / 宏觀日程 → §6 Premarket Movers → §7 大型科技股 →
§8 AI主線（半導體/基礎設施/電力） → §9 Sector Rotation →
§10 ETF資金流/Options → §11 市場寬度/技術面 → §12 財報 →
§13 交易計畫 → §14 風險矩陣 → §15 最終結論 → Tomorrow Key Signals →
Risk Matrix → Rotation Assessment → AI Trend Health Check

### us_close (美股收盤日報) — 26 required sections (LARGEST)
§1–§22 detailed sections + Tomorrow Key Signals → Risk Matrix →
Rotation Assessment → AI Trend Health Check

Key sections: §8 Sector Rotation, §9 AI主線（30+ stocks across
晶片/基礎設施/電力/算力子族群）, §17 ETF資金流, §18 Options Positioning,
§19 機構觀點

---

## 5. Quality Standards

### Hard Rules (any violation = error)
1. **No price estimation** — if search fails, write `⚠️ 未取得`, never estimate
2. **No arithmetic from cost basis** — `cost × 1.01` to infer today's price is forbidden
3. **No filler language** — "市場情緒樂觀", "投資者信心增強" are banned
4. **Snapshot prices are final** — when injected preamble exists, use those numbers verbatim
5. **No URL citations** — this is a push notification, not a research report
6. **Structure: Driver / Risk Condition / Confirmation Signal** — required in every major conclusion

### Style Standards
- **WHY not just WHAT** — explain the mechanism, not just the observation
- **High information density** — every sentence should contain a tradeable insight
- **Quantified claims** — "SOX +6.4%" not "semiconductors rose sharply"
- **Cross-asset mapping** — show how each asset affects the primary market

### Data Priority
1. Injected market snapshot (yfinance, verified) — inviolable
2. Google Search live results — for news, analysis, prices not in snapshot
3. If neither works → `⚠️ 未取得`

---

## 6. Common Failure Modes

| Failure | Root Cause | Detection |
|---------|-----------|-----------|
| Price hallucination | Model uses cost basis or training data | validate_report.py price check |
| Report truncation | max_output_tokens exceeded | Structure check (last anchor missing) |
| Empty sections | Search returned no results for niche stocks | ⚠️ 未取得 count |
| Filler analysis | Model fills space without data | Manual review (Tier 2) |
| Wrong date context | Cron ran on holiday | fetch_market_data.py exit code 2 |

---

## 7. Key Files

| File | Purpose |
|------|---------|
| `scripts/fetch_market_data.py` | Fetches verified prices via yfinance → `data/market_snapshot.json` |
| `scripts/generate_report.py` | Calls Gemini + injects snapshot → generates + sends report |
| `scripts/validate_report.py` | Structure check + price accuracy validation |
| `scripts/prepare_review.py` | Packages report + validation for AI reviewer |
| `prompts/{type}.md` | Report template + search tasks + style rules |
| `data/market_snapshot.json` | Runtime artifact: yfinance prices (ground truth) |
| `data/validation_results.json` | Runtime artifact: structure + price check results |
| `data/review_package_*.md` | Runtime artifact: AI reviewer input package |
| `reports/{type}_*.md` | Runtime artifact: generated reports |
| `.github/workflows/{type}.yml` | GitHub Actions workflow per report type |

---

## 8. GitHub Actions Secrets Required

| Secret | Purpose |
|--------|---------|
| `GEMINI_API_KEY` | Google AI Studio (free tier: gemini-2.0-flash) |
| `TELEGRAM_BOT_TOKEN` | Bot token for Telegram delivery |
| `TELEGRAM_CHAT_ID` | Target channel/chat |
| `EMAIL_SMTP_SERVER` | SMTP host (e.g., smtp.gmail.com) |
| `EMAIL_SMTP_PORT` | SMTP port (587 for TLS) |
| `EMAIL_USERNAME` | Sender email |
| `EMAIL_PASSWORD` | App password |
| `EMAIL_TO` | Recipient(s), comma-separated |
| `REPORT_MODEL` (var) | Gemini model override (default: gemini-2.0-flash) |

---

## 9. For the Reviewer AI

When you receive a `review_package_{type}_{date}.md`:

1. **Read Part 2** (market snapshot) to know the ground-truth prices
2. **Read Part 3** (validation results) to see what automated checks already caught
3. **Read Part 4** (the report) and evaluate against the rubric in Part 5
4. **Focus your review** on what automated checks cannot catch:
   - Filler language and low-information analysis
   - Missing driver/risk/signal structure
   - Sections that are technically present but analytically empty
   - Whether the trading plan is actionable
5. **Output JSON** as specified in Part 5 of the review package

Your review is used to improve prompt quality and catch systematic issues.
The goal is an institutional-grade report that a Taiwan equity trader would
find immediately actionable at 07:50 / 16:30 / 20:30 each day.
