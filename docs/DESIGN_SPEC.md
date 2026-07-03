# market-report-bot — Design Specification & Refactoring Plan

**Version:** 1.0
**Date:** 2026-07-03
**Scope:** `scripts/`, `prompts/`, `.github/workflows/`
**Baseline commit:** `a98180b`

---

## 0. Current-State Audit — Defects That Block the Plan

Three defects exist **today** and must be fixed before (or as part of) any refactor,
because Phases A–C build on persistence guarantees that are currently broken.

### D-1 (Critical) — `reports/` is gitignored but workflows commit it

`.gitignore` line 2 ignores `reports/`. All four report workflows run:

```yaml
git add reports/          # fatal: "paths are ignored by .gitignore", exit 1
```

The step is `continue-on-error: true`, so the job shows green while **no report is
ever committed**. Downstream consequences:

- `check_report_already_generated()` always sees an empty `reports/` on a fresh
  checkout → the idempotency guard added in `a98180b` is dead code in CI.
- `telegram_bot.py get_latest_report()` always returns `None` in CI → the bot
  answers every question with "(報告內容不可用…)" context.

**Fix:** remove `reports/` from `.gitignore`; add `reports/.gitkeep`. Reports are
markdown without secrets — committing them is the intended persistence mechanism.

### D-2 (Critical) — `bot_state.json` is gitignored but `bot.yml` commits it

Same failure mode: `git add bot_state.json` exits 1, masked by job-level
`continue-on-error: true`. The Telegram offset never persists across runs;
duplicate replies are possible at run boundaries.

**Fix:** remove `bot_state.json` from `.gitignore` (it contains only an integer
offset and cached report text — no secrets), or `git add -f bot_state.json`.
Prefer the `.gitignore` removal: `-f` hides intent.

### D-3 (Major) — exit-code capture in fetch steps is dead code

GHA `run:` uses `bash -eo pipefail`. In:

```yaml
python scripts/fetch_market_data.py tw_open
EXIT_CODE=$?               # never reached when exit code != 0
```

exit 2/3 kills the step before `$?` is read, so the `market_closed` branch logic
in YAML never executes and **holiday runs show as failed jobs**. The Python-side
`_set_github_output("market_closed", …)` happens to run before `sys.exit`, which
is why downstream `if:` conditions still work — but the job status is red.

**Fix (per workflow):**

```yaml
- name: Fetch market snapshot
  id: fetch
  run: |
    set +e
    python scripts/fetch_market_data.py tw_open
    EXIT_CODE=$?
    set -e
    if [ $EXIT_CODE -eq 2 ] || [ $EXIT_CODE -eq 3 ]; then
      exit 0            # market closed — market_closed=true already in GITHUB_OUTPUT
    fi
    exit $EXIT_CODE
```

### D-4 (Note) — `portfolio.json` never exists in CI

`portfolio.json` is gitignored and not tracked, so `_build_portfolio_block()` and
`_load_portfolio_context()` are no-ops on GHA runners. Since the file is now
de-identified, either (a) commit the de-identified version, or (b) store it as a
single GitHub Secret `PORTFOLIO_JSON` and materialize it in a workflow step:

```yaml
- name: Materialize portfolio
  env: { PORTFOLIO_JSON: ${{ secrets.PORTFOLIO_JSON }} }
  run: '[ -n "$PORTFOLIO_JSON" ] && echo "$PORTFOLIO_JSON" > portfolio.json || true'
```

Option (b) is recommended: holdings and cash remain out of the public repo even
in de-identified form.

---

## 1. Target Architecture

```
scripts/
  models.py            # NEW — pydantic DTOs (single source of truth for schemas)
  logging_config.py    # NEW — structured JSON logging + GHA annotations
  providers.py         # NEW — quote provider chain (yfinance batch → single → stooq/twse)
  report_store.py      # NEW — SQLite FTS5 store, 30-day report retention
  fetch_market_data.py # REFACTOR — thin orchestrator over providers.py + models.py
  generate_report.py   # REFACTOR — DTOs, logging, prompt assembly extracted
  validate_report.py   # REFACTOR — DTOs, logging
  telegram_bot.py      # REFACTOR — report_store-backed context, logging
  prepare_review.py    # minor — logging only
prompts/
  _common.md           # NEW — shared preamble (rules, style, search discipline)
  tw_open.md …         # SLIM — per-report body only
data/
  reports.db           # NEW — committed by bot.yml / report workflows
```

Dependency additions to `requirements.txt`:

```
pydantic>=2.7
```

No other new dependencies. SQLite FTS5 with the `trigram` tokenizer ships with
CPython 3.12's bundled SQLite (≥ 3.34) on `ubuntu-latest`.

---

## 2. Section A — Code Architecture & Robustness

### 2.1 A-1: yfinance Resiliency — Provider Chain (not proxy rotation)

**Recommendation: do not implement proxy rotation.** Rationale:

1. Free proxy pools are the least reliable component you can add to a pipeline
   whose whole purpose is data integrity; they also frequently MITM traffic.
2. Paid residential proxies add a secret, a cost, and a ToS gray zone with Yahoo.
3. GHA egress IPs rotate per-run already; blocks are typically rate-based, not
   IP-pinned. The correct mitigations are *fewer requests* and *fallback sources*.

**Design: three-tier provider chain with per-symbol failover.**

```
Tier 1  YFinanceBatchProvider   — ONE yf.download() call for all ~60 symbols
Tier 2  YFinanceSingleProvider  — per-symbol Ticker().history with tenacity retry
Tier 3  StooqProvider (US)      — free CSV endpoint, no key, no auth
        TWSEProvider (TW .TW)   — TWSE OpenAPI STOCK_DAY_ALL, one call, no key
        TPExProvider (TW .TWO)  — TPEx OpenAPI equivalent for OTC symbols
```

Tier 1 collapses ~60 sequential HTTP requests into one, which removes most of
the rate-limit exposure that motivates proxy talk in the first place. Tiers 2–3
only run for symbols Tier 1 missed.

**Coverage gate.** The snapshot records which provider served each symbol and an
aggregate `fetch_coverage` ratio. Below `MIN_COVERAGE = 0.70` the script exits 1:
the workflow's existing failure path then either retries or the report falls
back to pure-search mode (existing behavior when `market_snapshot.json` is
absent). A partially-empty snapshot silently anchoring the LLM is worse than no
snapshot.

**`scripts/providers.py`:**

```python
"""Quote providers with tiered failover. Order: batch yfinance → single yfinance
→ free HTTP fallbacks (Stooq for US, TWSE/TPEx OpenAPI for Taiwan)."""

import csv
import io
import logging
from typing import Protocol

import requests
import yfinance as yf
from tenacity import retry, stop_after_attempt, wait_exponential_jitter

from models import Quote

log = logging.getLogger("providers")

# Stooq symbol mapping for indices; plain US equities map to "<sym>.us"
_STOOQ_INDEX = {
    "^GSPC": "^spx", "^NDX": "^ndx", "^DJI": "^dji", "^RUT": "^rut",
    "^SOX": "^sox", "^VIX": "^vix", "^TWII": "^twse",
}


class QuoteProvider(Protocol):
    name: str
    def fetch_many(self, symbols: list[str]) -> dict[str, Quote]: ...


def _quote_from_closes(closes: list[tuple[str, float]]) -> Quote | None:
    """closes: [(YYYY-MM-DD, close), …] ascending. Needs >= 1 row."""
    if not closes:
        return None
    date, last = closes[-1]
    prev = closes[-2][1] if len(closes) >= 2 else last
    change = (last - prev) / prev * 100 if prev else 0.0
    return Quote(price=round(last, 4), prev_close=round(prev, 4),
                 change_pct=round(change, 2), data_date=date)


class YFinanceBatchProvider:
    name = "yfinance_batch"

    def fetch_many(self, symbols: list[str]) -> dict[str, Quote]:
        out: dict[str, Quote] = {}
        try:
            df = yf.download(symbols, period="5d", group_by="ticker",
                             progress=False, threads=True)
        except Exception:
            log.warning("batch download failed", exc_info=True)
            return out
        for sym in symbols:
            try:
                closes_series = (df[sym]["Close"] if len(symbols) > 1
                                 else df["Close"]).dropna()
                closes = [(idx.strftime("%Y-%m-%d"), float(v))
                          for idx, v in closes_series.items()]
                if (q := _quote_from_closes(closes)):
                    out[sym] = q
            except (KeyError, TypeError):
                continue
        return out


class YFinanceSingleProvider:
    name = "yfinance_single"

    @retry(reraise=False, stop=stop_after_attempt(3),
           wait=wait_exponential_jitter(initial=2, max=15))
    def _one(self, symbol: str) -> Quote | None:
        hist = yf.Ticker(symbol).history(period="5d")
        if hist.empty:
            return None
        closes = [(idx.strftime("%Y-%m-%d"), float(v))
                  for idx, v in hist["Close"].dropna().items()]
        return _quote_from_closes(closes)

    def fetch_many(self, symbols: list[str]) -> dict[str, Quote]:
        out = {}
        for sym in symbols:
            try:
                if (q := self._one(sym)):
                    out[sym] = q
            except Exception:
                log.warning("single fetch failed", extra={"symbol": sym})
        return out


class StooqProvider:
    """US equities + major indices. https://stooq.com/q/d/l/?s=aapl.us&i=d"""
    name = "stooq"

    def _map(self, symbol: str) -> str | None:
        if symbol in _STOOQ_INDEX:
            return _STOOQ_INDEX[symbol]
        if symbol.endswith((".TW", ".TWO")) or "=" in symbol or "-" in symbol:
            return None  # not covered
        return f"{symbol.lower()}.us"

    def fetch_many(self, symbols: list[str]) -> dict[str, Quote]:
        out = {}
        for sym in symbols:
            stooq_sym = self._map(sym)
            if not stooq_sym:
                continue
            try:
                resp = requests.get("https://stooq.com/q/d/l/",
                                    params={"s": stooq_sym, "i": "d"}, timeout=15)
                resp.raise_for_status()
                rows = list(csv.DictReader(io.StringIO(resp.text)))[-5:]
                closes = [(r["Date"], float(r["Close"])) for r in rows]
                if (q := _quote_from_closes(closes)):
                    out[sym] = q
            except Exception:
                log.warning("stooq fetch failed", extra={"symbol": sym})
        return out


class TWSEProvider:
    """All .TW symbols in ONE call via TWSE OpenAPI (no key)."""
    name = "twse_openapi"
    URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"

    def fetch_many(self, symbols: list[str]) -> dict[str, Quote]:
        wanted = {s.split(".")[0]: s for s in symbols if s.endswith(".TW")}
        if not wanted:
            return {}
        out = {}
        try:
            rows = requests.get(self.URL, timeout=20).json()
        except Exception:
            log.warning("twse openapi failed", exc_info=True)
            return out
        for row in rows:
            code = row.get("Code")
            if code in wanted:
                try:
                    last = float(row["ClosingPrice"])
                    change = float(row.get("Change", 0) or 0)
                    prev = last - change
                    out[wanted[code]] = Quote(
                        price=round(last, 4), prev_close=round(prev, 4),
                        change_pct=round(change / prev * 100, 2) if prev else 0.0,
                        data_date=row.get("Date", ""))
                except (KeyError, ValueError):
                    continue
        return out


def fetch_with_failover(symbols: list[str]) -> tuple[dict[str, Quote], dict[str, str]]:
    """Return ({symbol: Quote}, {symbol: provider_name}) trying each tier for
    whatever the previous tiers missed."""
    chain: list[QuoteProvider] = [
        YFinanceBatchProvider(), YFinanceSingleProvider(),
        TWSEProvider(), StooqProvider(),
    ]
    quotes: dict[str, Quote] = {}
    sources: dict[str, str] = {}
    remaining = list(symbols)
    for provider in chain:
        if not remaining:
            break
        got = provider.fetch_many(remaining)
        for sym, q in got.items():
            quotes[sym] = q
            sources[sym] = provider.name
        remaining = [s for s in remaining if s not in quotes]
        if got:
            log.info("provider tier done", extra={
                "provider": provider.name, "hit": len(got), "miss": len(remaining)})
    return quotes, sources
```

Add a TPEx provider for `.TWO` symbols when needed (currently only `3324.TWO`;
`YFinanceSingleProvider` covers it as Tier 2, so TPEx is optional in Phase 1).

**Explicitly out of scope:** LLM web search as a price fallback. The whole
Market Data Layer exists because search-derived prices hallucinate; feeding
search prices back into the "verified snapshot" would poison the ground truth
that `validate_report.py` compares against. A missing symbol must stay missing
(`⚠️ 未取得`), and the coverage gate handles wholesale outages.

### 2.2 A-2: Type Safety — pydantic DTOs

**`scripts/models.py`:**

```python
"""Data-transfer objects shared by fetch / generate / validate / review scripts.
pydantic v2. These models ARE the schema of data/*.json — change them here only."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

ReportType = Literal["tw_open", "tw_close", "us_open", "us_close"]


class Quote(BaseModel):
    price: float = Field(gt=0)
    prev_close: float = Field(gt=0)
    change_pct: float
    data_date: str  # YYYY-MM-DD

    @field_validator("data_date")
    @classmethod
    def _iso_date(cls, v: str) -> str:
        datetime.strptime(v[:10].replace("/", "-"), "%Y-%m-%d")
        return v


class NamedQuote(Quote):
    name: str
    currency: str = ""
    symbol: str = ""


class Snapshot(BaseModel):
    generated_at: datetime
    report_type: ReportType
    fetch_coverage: float = Field(ge=0, le=1, default=1.0)
    sources: dict[str, str] = {}          # symbol → provider name
    tw_stocks: dict[str, NamedQuote] = {}
    us_markets: dict[str, NamedQuote] = {}
    forex: dict[str, NamedQuote] = {}

    def ground_truth(self) -> dict[str, float]:
        gt: dict[str, float] = {}
        for section in (self.tw_stocks, self.us_markets, self.forex):
            for key, q in section.items():
                gt[key] = q.price
        return gt


class Position(BaseModel):
    ticker: str
    name: str
    shares: float = Field(gt=0)
    cost_basis: float = Field(gt=0)
    note: str = ""


class Portfolio(BaseModel):
    tw_positions: list[Position] = []
    us_positions: list[Position] = []
    available_cash: str | float | None = None
    portfolio_notes: str = ""


class StructureCheck(BaseModel):
    sections_total: int
    sections_present: int
    sections_missing: list[str]
    truncated: bool
    missing_data_count: int
    missing_data_pct: float
    char_count: int


class PriceCheckRow(BaseModel):
    ticker: str
    reported: float
    actual: float
    diff_pct: float


class PriceCheck(BaseModel):
    passed: list[PriceCheckRow] = []
    failed: list[PriceCheckRow] = []
    unchecked: list[dict] = []


class ValidationResult(BaseModel):
    structure: StructureCheck
    price_check: PriceCheck | None = None
```

**Adoption pattern** (the entire refactor, applied uniformly):

```python
# writing
snapshot_path.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")

# reading — raises pydantic.ValidationError on schema drift instead of KeyError
snapshot = Snapshot.model_validate_json(snapshot_path.read_text(encoding="utf-8"))
portfolio = Portfolio.model_validate_json(portfolio_path.read_text(encoding="utf-8"))
```

A malformed `market_snapshot.json` now fails **at load time with a field-level
error message**, not deep inside `_build_snapshot_block` with a `KeyError:
'change_pct'`. `generate_report.py` treats `ValidationError` on the snapshot the
same as a missing snapshot (fall back to pure-search mode, log at ERROR);
a `ValidationError` on the portfolio skips portfolio injection.

### 2.3 A-3: Logging & Observability

**`scripts/logging_config.py`:**

```python
"""Structured JSON logging for GHA runners. One JSON object per line; ERROR+
additionally emits GitHub Actions ::error:: annotations."""

import json
import logging
import os
import sys
from datetime import datetime, timezone

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, val in record.__dict__.items():        # extra={...} fields
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = val
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class GHAAnnotationHandler(logging.Handler):
    """Surface ERROR+ as ::error:: so failures appear in the run summary."""
    def emit(self, record: logging.LogRecord) -> None:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            msg = record.getMessage().replace("\n", "%0A")
            print(f"::error title={record.name}::{msg}", file=sys.stderr)


def setup_logging(level: str | None = None) -> None:
    root = logging.getLogger()
    root.setLevel(level or os.environ.get("LOG_LEVEL", "INFO"))
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(JsonFormatter())
    annot = GHAAnnotationHandler()
    annot.setLevel(logging.ERROR)
    root.handlers = [stream, annot]
    logging.getLogger("yfinance").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
```

Migration rule — mechanical, no judgment calls:

| Before | After |
|---|---|
| `print(f"[fetch] …")` | `log.info("…", extra={…})` |
| `print(…, file=sys.stderr)` | `log.warning(…)` or `log.error(…)` |
| bare `except Exception as exc: print(exc)` | `log.error("…", exc_info=True)` |

Each entry point gains two lines:

```python
from logging_config import setup_logging
setup_logging()
log = logging.getLogger("fetch")   # or "generate" / "validate" / "bot"
```

Human-readable progress lines (per-ticker prices during fetch) stay as
`log.info` with the values in `extra` — greppable in the GHA log *and* parseable
as JSON if logs are ever shipped elsewhere.

---

## 3. Section B — Prompt Engineering & Search Grounding

### 3.1 B-1: Prompt Coherence — shared preamble, de-duplicated rules

Current state: the four prompt files (217–341 lines) repeat the role block,
style rules, data-discipline rules, and tail-module definitions with slight
drift between copies (the Risk Matrix duplicate-header bug in `952af71` was a
symptom of exactly this drift).

**Refactor:**

1. Create `prompts/_common.md` containing: role definition, 分析風格規則,
   data-discipline rules (no estimation, ⚠️ 未取得, snapshot supremacy),
   anti-fluff rules, and the four tail-module templates (Tomorrow Key Signals /
   Risk Matrix / Rotation Assessment / AI Trend Health Check).
2. Per-report files keep only: the search-task list, the section skeleton
   (`## 1.` … `## N.`), and report-specific emphases.
3. `load_prompt()` becomes:

```python
def load_prompt(report_type: str) -> str:
    prompts_dir = Path(__file__).parent.parent / "prompts"
    common = (prompts_dir / "_common.md").read_text(encoding="utf-8")
    body = (prompts_dir / f"{report_type}.md").read_text(encoding="utf-8")
    text = common + "\n\n" + body
    mdate = get_market_date(report_type)
    ...
```

4. **Anti-fluff additions to `_common.md`** (append to 分析風格規則):

```
- 禁止輸出任何開場白、免責聲明、「以下是報告」等框架性文字，直接從 H1 標題開始
- 禁止在報告結尾加任何總結性客套話；最後一個字元必須屬於最後一個模組的內容
- 每一節直接進入數據與判斷；禁止「本節將分析…」式的過渡句
- 表格欄位不留空白；無數據填「⚠️ 未取得」
```

5. **Portfolio-actionability contract** — since `_build_portfolio_block` injects
   holdings, `_common.md` defines what "actionable" means once, precisely:

```
- 涉及使用者持股的個股，操作建議必須包含：方向（加碼/減碼/續抱/出場）、
  觸發價位（相對現價的具體數字）、失效條件（什麼情況下此建議作廢）
- 有 available_cash 時，加碼建議必須換算成可執行股數
- 禁止「視情況而定」「建議觀望」等無觸發條件的建議
```

Acceptance: run `wc -l prompts/*.md` before/after; expect ≥ 35% total-line
reduction and byte-identical tail-module definitions across all four types
(verifiable with `grep -A6 "Risk Matrix" prompts/_common.md`).

### 3.2 B-2: Search Grounding Precision

Gemini's `google_search` tool issues the queries it invents from prose
instructions. Vague instructions ("搜尋今日台股重要新聞") produce vague queries
and noisy grounding. The countermeasure is to write the search-task list as
**query templates with resolved dates and source constraints**, not topics.

Rewrite pattern for each `## 搜尋任務` list (dates resolve via the existing
`{{TODAY_DATE}}` substitution; add `{{PREV_TRADE_DATE}}` as a new placeholder
computed in `load_prompt()` from the snapshot's `data_date`):

```
## 搜尋任務（依下列查詢句執行搜尋，每項最多 1 次查詢，禁止自行改寫主題）
1. "S&P 500 Nasdaq close {{PREV_TRADE_DATE}} why" — 昨夜美股收盤驅動因子
2. "FedWatch rate cut probability {{TODAY_DATE}}" — Fed 降息定價
3. "外資 台指期 未平倉 {{TODAY_DATE}}" — 外資期貨部位
4. "TSMC ADR premium {{PREV_TRADE_DATE}}" — TSM ADR 溢價
5. "site:cnyes.com OR site:moneydj.com 台股 盤前 {{TODAY_DATE}}" — 台股盤前要聞
...

搜尋紀律：
- 價格與數字一律以系統快照為準；搜尋結果中的價格一律忽略
- 搜尋結果與快照衝突時，在報告中採用快照數字並可註記「市場報導與快照略有出入」
- 查無結果的項目直接標記「⚠️ 未取得」，禁止用相近日期或相似標的的資料替代
```

Two structural rules that measurably reduce hallucination with grounded Gemini:

- **Bound the search count** ("每項最多 1 次查詢") — unbounded search loops are
  where off-date data enters.
- **Quarantine search from prices** — search is for narrative (why / flows /
  events); the snapshot is for numbers. This is already the snapshot preamble's
  stance; repeating it inside the search-task block closes the gap where the
  model treats task-list instructions as overriding the preamble.

Verification loop already exists: `validate_report.py` price-check failures per
report type, tracked week-over-week in `validation_results.json` artifacts, is
the regression metric for this change. Target: `failed` count at 0 and
`unchecked` shrinking as snapshot coverage grows.

---

## 4. Section C — Telegram Bot Context Retention

### 4.1 Design choice: SQLite + FTS5, not a vector store

Corpus size is ~30 documents × ~15 KB. At this scale embeddings add an API
dependency, a persistence problem, and no retrieval quality over full-text
search — questions about reports ("上週五 NVDA 說什麼", "2330 這幾天的建議")
are keyword-shaped. SQLite FTS5 with the `trigram` tokenizer handles CJK
substring matching without a segmenter, ships in CPython 3.12's bundled SQLite,
and persists as a single file committed by the existing `bot.yml` git step.

### 4.2 `scripts/report_store.py`

```python
"""30-day report store on SQLite FTS5 (trigram tokenizer for CJK).
DB file: data/reports.db — committed to the repo by workflows for persistence."""

import logging
import re
import sqlite3
from pathlib import Path

log = logging.getLogger("report_store")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id          INTEGER PRIMARY KEY,
    report_type TEXT NOT NULL,
    market_date TEXT NOT NULL,           -- YYYYMMDD
    filename    TEXT NOT NULL UNIQUE,
    content     TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS reports_fts USING fts5(
    content, content='reports', content_rowid='id', tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS reports_ai AFTER INSERT ON reports BEGIN
    INSERT INTO reports_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS reports_ad AFTER DELETE ON reports BEGIN
    INSERT INTO reports_fts(reports_fts, rowid, content)
    VALUES ('delete', old.id, old.content);
END;
"""

_FNAME = re.compile(r"^(tw_open|tw_close|us_open|us_close)_(\d{8})_\d{6}\.md$")


class ReportStore:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(exist_ok=True)
        self._con = sqlite3.connect(db_path)
        self._con.executescript(_SCHEMA)

    def ingest_dir(self, reports_dir: Path) -> int:
        """Idempotently import any report files not yet in the DB."""
        added = 0
        for f in sorted(reports_dir.glob("*.md")):
            m = _FNAME.match(f.name)
            if not m:
                continue
            try:
                self._con.execute(
                    "INSERT OR IGNORE INTO reports "
                    "(report_type, market_date, filename, content) VALUES (?,?,?,?)",
                    (m.group(1), m.group(2), f.name,
                     f.read_text(encoding="utf-8")))
                added += self._con.total_changes > 0
            except sqlite3.Error:
                log.warning("ingest failed", extra={"file": f.name})
        self._con.commit()
        return added

    def latest(self) -> tuple[str, str] | None:
        """(market_date, content) of the most recent report of any type."""
        row = self._con.execute(
            "SELECT market_date, content FROM reports "
            "ORDER BY market_date DESC, id DESC LIMIT 1").fetchone()
        return row if row else None

    def search(self, query: str, exclude_latest: bool = True,
               limit: int = 2, excerpt_chars: int = 3000) -> list[str]:
        """Top FTS matches for the question, as dated excerpts for the prompt."""
        terms = [t for t in re.findall(r"[\w一-鿿]{2,}", query)][:6]
        if not terms:
            return []
        fts_query = " OR ".join(f'"{t}"' for t in terms)
        try:
            rows = self._con.execute(
                "SELECT r.report_type, r.market_date, r.content "
                "FROM reports_fts f JOIN reports r ON r.id = f.rowid "
                "WHERE reports_fts MATCH ? ORDER BY rank LIMIT ?",
                (fts_query, limit + 1)).fetchall()
        except sqlite3.OperationalError:
            log.warning("fts query failed", extra={"query": fts_query})
            return []
        latest = self.latest()
        out = []
        for rtype, mdate, content in rows:
            if exclude_latest and latest and content == latest[1]:
                continue
            out.append(f"【歷史報告 {mdate} {rtype}（節錄）】\n{content[:excerpt_chars]}")
        return out[:limit]

    def prune(self, keep_days: int = 30) -> int:
        from datetime import datetime, timedelta
        cutoff = (datetime.now() - timedelta(days=keep_days)).strftime("%Y%m%d")
        cur = self._con.execute(
            "DELETE FROM reports WHERE market_date < ?", (cutoff,))
        self._con.commit()
        return cur.rowcount

    def close(self) -> None:
        self._con.close()
```

### 4.3 Bot integration

Context assembly per question replaces the single `latest_report` string:

```python
store = ReportStore(Path(__file__).parent.parent / "data" / "reports.db")
store.ingest_dir(reports_dir)   # picks up reports committed since last run
store.prune(keep_days=30)

def build_context(question: str) -> str:
    parts = []
    if (latest := store.latest()):
        parts.append(f"【今日最新報告 {latest[0]}】\n{latest[1]}")
    parts += store.search(question, limit=2)   # multi-day reasoning context
    return "\n\n".join(parts) or "(報告內容不可用，請根據市場知識回答)"
```

Token budget: latest report (~10–15 KB) + 2 × 3 KB excerpts ≈ well under
Gemini Flash's context window; no truncation logic needed beyond
`excerpt_chars`.

Persistence: `bot.yml`'s commit step adds `data/reports.db` alongside
`bot_state.json` (both must first be un-gitignored per D-1/D-2; add a
`.gitignore` exception `!data/reports.db` since `data/*.json` stays ignored —
the db is not matched by `*.json`, so no change actually required).
The `SYSTEM_PROMPT` gains one line:

```
若引用了歷史報告節錄，回答時標明日期（例如「根據 6/28 美股收盤報告…」）。
```

---

## 5. Refactored Key Files — Assembly

### 5.1 `scripts/fetch_market_data.py` (structure after refactor)

Ticker tables, holiday checks, and exit-code contract are unchanged. The fetch
core shrinks to:

```python
from logging_config import setup_logging
from models import NamedQuote, Snapshot
from providers import fetch_with_failover

MIN_COVERAGE = 0.70

def build_snapshot(report_type: str) -> Snapshot:
    symbol_meta: dict[str, tuple[str, str, str]] = {}     # symbol → (bucket, key, name/currency…)
    for sym, name in TW_STOCKS.items():
        symbol_meta[sym] = ("tw_stocks", sym.split(".")[0], name, "TWD")
    for key, (sym, name, cur) in US_MARKETS.items():
        symbol_meta[sym] = ("us_markets", key, name, cur)
    for key, (sym, name, cur) in FOREX.items():
        symbol_meta[sym] = ("forex", key, name, cur)

    quotes, sources = fetch_with_failover(list(symbol_meta))

    snapshot = Snapshot(
        generated_at=datetime.now(tz=TPE), report_type=report_type,
        fetch_coverage=round(len(quotes) / len(symbol_meta), 3),
        sources=sources)
    for sym, q in quotes.items():
        bucket, key, name, cur = symbol_meta[sym]
        getattr(snapshot, bucket)[key] = NamedQuote(
            name=name, currency=cur, symbol=sym, **q.model_dump())
    return snapshot


def main() -> None:
    setup_logging()
    ...
    snapshot = build_snapshot(report_type)
    if snapshot.fetch_coverage < MIN_COVERAGE:
        log.error("coverage below threshold — refusing to write snapshot",
                  extra={"coverage": snapshot.fetch_coverage, "min": MIN_COVERAGE})
        sys.exit(1)          # workflow retries or report runs in pure-search mode
    snapshot_path.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
    log.info("snapshot saved", extra={
        "coverage": snapshot.fetch_coverage,
        "tw": len(snapshot.tw_stocks), "us": len(snapshot.us_markets)})
```

### 5.2 `scripts/generate_report.py` (deltas only)

- `import logging` + `setup_logging()`; all `print` → `log.*` per §2.3 table.
- Snapshot / portfolio loading through DTOs:

```python
try:
    snapshot = Snapshot.model_validate_json(
        snapshot_path.read_text(encoding="utf-8"))
    prompt = _build_snapshot_block(snapshot) + prompt
except (FileNotFoundError, ValidationError):
    log.error("snapshot missing or invalid — pure search mode", exc_info=True)
```

- `_build_snapshot_block(snapshot: Snapshot)` / `_build_portfolio_block(p:
  Portfolio)` switch from `d['price']` dict access to `q.price` attribute
  access; a low-coverage warning line is added to the preamble when
  `snapshot.fetch_coverage < 1.0` listing the buckets that are incomplete, so
  the model states "⚠️ 未取得" instead of silently searching for the gaps.
- `load_prompt()` concatenates `_common.md` + body and resolves
  `{{PREV_TRADE_DATE}}` (§3.1, §3.2).
- Everything else (retry decorator, idempotency check, Telegram/Email senders)
  is unchanged.

### 5.3 `scripts/telegram_bot.py` (deltas only)

- `setup_logging()`; `print` → `log.*`.
- Replace `get_latest_report()` + `state["latest_report"]` with `ReportStore`
  per §4.3 (state file keeps only `offset` — it shrinks from ~15 KB to one int,
  making the git-commit persistence cheap and conflict-free).
- `ask_gemini(question, context, portfolio_context, model)` unchanged except
  `context` now comes from `build_context(question)`.

---

## 6. Migration Plan

| Phase | Content | Risk | Verification |
|---|---|---|---|
| **0** | Fix D-1…D-4 (gitignore, exit-code capture, portfolio secret) | Low | Next scheduled run: report file appears as a repo commit; holiday dry-run exits green |
| **1** | `models.py` + `logging_config.py`; adopt in fetch/generate/validate | Low — pure refactor, JSON shape unchanged (`model_dump_json` field names match current dicts) | `python -m py_compile scripts/*.py`; local dry-run `fetch → generate --dry → validate` chain |
| **2** | `providers.py` failover chain + coverage gate | Medium — new code paths | Local run with network; force-fail Tier 1 (`YF_DISABLE=1` test hook) and confirm Tier 2/3 fill; check `sources` map in snapshot |
| **3** | Prompt split (`_common.md`) + search-query templates | Medium — LLM output shifts | One `workflow_dispatch` dry-run per report type; `validate_report.py` structure check must pass 19/19 (26/26 for us_close); compare `⚠️ 未取得` counts before/after |
| **4** | `report_store.py` + bot integration | Low — bot is `continue-on-error` | Ask the bot a question referencing a prior day; confirm dated citation in answer; confirm `data/reports.db` commit appears |

Ordering constraints: Phase 0 unblocks everything (Phases 1–4 assume reports
and bot state actually persist). Phase 3 is independent of 1–2 and can run in
parallel. One phase per PR; each PR must pass a `workflow_dispatch` dry run of
`tw_open` before merge.

Rollback: every phase is a plain revert; no data migrations exist (the SQLite
db regenerates from `reports/` via `ingest_dir`).

---

## 7. Acceptance Criteria

1. **Resiliency:** with Tier 1 artificially disabled, snapshot coverage stays
   ≥ 0.9 for TW symbols (TWSE OpenAPI) and ≥ 0.8 for US equities (Stooq).
   Coverage < 0.70 exits 1 and produces a `::error::` annotation.
2. **Type safety:** deleting any required field from `market_snapshot.json`
   causes `generate_report.py` to log a field-level ValidationError and fall
   back to pure-search mode instead of crashing with KeyError.
3. **Observability:** every log line in a GHA run is valid JSON
   (`jq -s '.' < run.log` succeeds); ERROR lines appear as annotations in the
   run summary.
4. **Prompts:** total prompt-file line count reduced ≥ 35%; tail modules exist
   exactly once in `_common.md`; validator structure check passes for all four
   types on three consecutive scheduled runs.
5. **Bot:** answers cite dated historical reports when relevant; `bot_state.json`
   and `data/reports.db` commits appear after each bot run; no duplicate replies
   across run boundaries for one trading week.
