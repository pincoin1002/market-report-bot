#!/usr/bin/env python3
"""Institutional-grade market report generator.

Uses Google Gemini API with Google Search grounding to generate daily market
reports for Taiwan and US equity markets, then distributes via Telegram and Email.
"""

import logging
import os
import sys
import smtplib
import time
import argparse
import hashlib
import json
import re
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
from google import genai
from google.genai import types
import markdown
from pydantic import ValidationError
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from logging_config import setup_logging
import delivery_state
from instrument_registry import resolve_instrument
from market_context import build_market_context
from market_session import human_session_label, report_market_date, get_target_market_date
from adr_engine import calculate_tsm_adr_premium
from models import MarketContext, Portfolio, Snapshot
import portfolio_store
from portfolio_context import EncryptedPortfolioProvider, load_authoritative_portfolio
from portfolio_events import fetch_portfolio_events
from structured_reports import (
    build_action_brief, build_public_draft, render_action_brief,
    validate_action_brief, validate_public_draft,
)

log = logging.getLogger("generate")

# ── Constants ──────────────────────────────────────────────────────────────────

REPORT_TYPES = ("tw_open", "us_close", "tw_close", "us_open")

# us_close has 22 sections + 4 tail modules — needs a higher ceiling
MAX_OUTPUT_TOKENS: dict[str, int] = {
    "tw_open":  16000,
    "tw_close": 16000,
    "us_open":  16000,
    "us_close": 24000,
}

REPORT_TITLES = {
    "tw_open":  "台股開盤戰報",
    "us_close": "美股收盤日報",
    "tw_close": "台股收盤日報",
    "us_open":  "美股開盤日報",
}

TPE = timezone(timedelta(hours=8))

def get_market_date(report_type: str) -> datetime:
    """Return the financial market date for this report type relative to TPE time."""
    date_str = report_market_date(report_type)
    return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=TPE)

# ── Core ───────────────────────────────────────────────────────────────────────

def _prev_trade_date(mdate: datetime, market: str = "US") -> str:
    """Return the previous completed trading date using calendar-aware resolution."""
    return get_target_market_date("tw_open", market, now=mdate)


def load_prompt(report_type: str) -> str:
    prompts_dir = Path(__file__).parent.parent / "prompts"
    prompt_path = prompts_dir / f"{report_type}.md"
    if not prompt_path.exists():
        raise FileNotFoundError(f"Prompt not found: {prompt_path}")
    common = (prompts_dir / "_common.md").read_text(encoding="utf-8")
    body = prompt_path.read_text(encoding="utf-8")
    text = common + "\n\n" + body
    mdate = get_market_date(report_type)
    weekday_map = {0: "週一", 1: "週二", 2: "週三", 3: "週四", 4: "週五", 5: "週六", 6: "週日"}
    target_us_date = get_target_market_date(report_type, "US", now=datetime.now(tz=TPE))
    return (text
            .replace("{{TODAY_DATE}}", mdate.strftime("%Y-%m-%d"))
            .replace("{{TODAY_WEEKDAY}}", weekday_map[mdate.weekday()])
            .replace("{{PREV_TRADE_DATE}}", target_us_date))


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=4, max=30),
    retry=retry_if_exception(lambda e: isinstance(e, Exception)),
)
def _call_gemini_api(prompt: str, model: str, report_type: str, use_search: bool) -> str:
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    max_tokens = MAX_OUTPUT_TOKENS.get(report_type, 16000)
    
    # Disable safety filters to prevent false positives on stock market terms (e.g. crash, sell-off)
    safety_settings = [
        types.SafetySetting(
            category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
            threshold=types.HarmBlockThreshold.BLOCK_NONE,
        ),
        types.SafetySetting(
            category=types.HarmCategory.HARM_CATEGORY_HARASSMENT,
            threshold=types.HarmBlockThreshold.BLOCK_NONE,
        ),
        types.SafetySetting(
            category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
            threshold=types.HarmBlockThreshold.BLOCK_NONE,
        ),
        types.SafetySetting(
            category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
            threshold=types.HarmBlockThreshold.BLOCK_NONE,
        ),
    ]

    tools = [types.Tool(google_search=types.GoogleSearch())] if use_search else None

    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            tools=tools,
            temperature=0.1,
            max_output_tokens=max_tokens,
            safety_settings=safety_settings,
        ),
    )
    if not response or not response.text:
        raise ValueError("Gemini API returned empty response text")
    return response.text


def generate_report(prompt: str, model: str, report_type: str) -> str:
    try:
        return _call_gemini_api(prompt, model, report_type, use_search=True)
    except Exception as exc:
        log.warning("Google Search generation failed; using verified-data path", extra={"error_type": type(exc).__name__})
        if os.getenv("ALLOW_UNGROUNDED_NEWS_FALLBACK", "").lower() == "true":
            return _call_gemini_api(prompt, model, report_type, use_search=False)
        return _verified_data_only_report(prompt, report_type)


def _verified_data_only_report(prompt: str, report_type: str) -> str:
    # The structured renderer owns all public sections and quote facts. Never
    # copy the internal prompt into a fallback report or its artifacts.
    return "新聞搜尋目前不可用；以下報告僅使用已驗證行情，不推論未核實的事件原因。"


def _build_snapshot_block(snapshot: Snapshot) -> str:
    """Format the market snapshot as a strongly-worded preamble for the prompt."""
    fetched_at = snapshot.generated_at.strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "## ⚠️ 系統提供的市場快照 — 禁止修改，禁止估算，禁止重新搜尋價格",
        f"（抓取時間：{fetched_at} TPE | 來源：已驗證行情資料）",
        "",
        "以下所有數字為已驗證的最新收盤價。在報告中引用時必須完全一致，不得四捨五入或改動。",
        "若 Google Search 返回衝突數字，以本快照為準。",
        "搜尋工具應用於新聞、分析、上下文，而非已提供的價格。",
        "",
    ]

    if snapshot.fetch_coverage < 1.0:
        lines += [
            f"⚠️ 本快照涵蓋率為 {snapshot.fetch_coverage:.0%}。"
            "快照中未列出的標的，報告中對應欄位一律填「⚠️ 未取得」，禁止搜尋或估算其價格。",
            "",
        ]

    if snapshot.tw_stocks:
        lines += ["**台股**",
                  "| 代號 | 名稱 | 收盤 (TWD) | 漲跌% |",
                  "|------|------|-----------|-------|"]
        for code, q in snapshot.tw_stocks.items():
            lines.append(f"| {code} | {q.name} | {q.price:,.1f} | {q.change_pct:+.2f}% |")
        lines.append("")

    if snapshot.us_markets:
        lines += ["**美股 / 指數 / 宏觀**",
                  "| Symbol | 名稱 | 最新 | 漲跌% |",
                  "|--------|------|------|-------|"]
        for key, q in snapshot.us_markets.items():
            val = f"{q.price:.2f}%" if q.currency == "percent" else f"{q.price:,.2f}"
            lines.append(f"| {key} | {q.name} | {val} | {q.change_pct:+.2f}% |")
        lines.append("")

    if snapshot.forex:
        lines.append("**外匯**")
        for key, q in snapshot.forex.items():
            lines.append(f"- {q.name}: {q.price:.4f} ({q.change_pct:+.2f}%)")
        lines.append("")


    # Deterministic ADR premium calculation for tw_open
    if snapshot.report_type == "tw_open":
        target_us_date = get_target_market_date("tw_open", "US", now=snapshot.generated_at)
        target_tw_date = get_target_market_date("tw_open", "TW", now=snapshot.generated_at)
        tsm_obs = snapshot.quote_observations.get("TSM")
        tw_obs = snapshot.quote_observations.get("2330")
        fx_obs = snapshot.quote_observations.get("USDTWD")
        _, adr_text, _ = calculate_tsm_adr_premium(
            tsm_obs, tw_obs, fx_obs, target_us_date, target_tw_date, report_as_of=snapshot.generated_at
        )
        lines += [
            "### 系統已驗證 ADR 溢折價（唯一真相 — 禁止修改、禁止重新計算）",
            adr_text,
            "",
        ]

    blocked_quotes = [
        obs for obs in snapshot.quote_observations.values()
        if obs.quality_status != "VALID"
    ]
    if blocked_quotes:
        lines += [
            "### ⚠️ 數據阻斷清單（DATA_BLOCKED / 嚴禁使用舊價或猜測）",
            "以下標的未通過時間一致性或資料驗證，報告中對應欄位一律填「⚠️ 未取得 (DATA_BLOCKED)」，禁止計算漲跌幅或納入映射：",
        ]
        for obs in blocked_quotes:
            notes_str = ", ".join(obs.quality_notes) if obs.quality_notes else obs.quality_status
            lines.append(f"- **{obs.canonical_symbol}**: {obs.quality_status} (交易日: {obs.market_date} | 說明: {notes_str})")
        lines.append("")

    lines += [
        "**重要紀律守則：**",
        "1. 僅有 Quality 為 VALID 且列於上方快照表格的數字為有效收盤價，報告中引用時必須完全一致。",
        "2. 列於「數據阻斷清單」或快照中未列出的標的，報告中對應欄位一律填「⚠️ 未取得 (DATA_BLOCKED)」，嚴禁使用相近交易日價格、嚴禁稱舊資料為「昨收」、嚴禁推估其漲跌幅。",
        "3. ADR 表現與溢折價分析段落必須嚴格採用上方「系統已驗證 ADR 溢折價」內容；若呈現「DATA_BLOCKED — TEMPORAL_MISMATCH」，必須完整保留該阻斷訊息，嚴禁自行尋找資料推算溢價！",
        "---",
        "",
    ]
    return "\n".join(lines)


def load_market_context(report_type: str, snapshot: Snapshot | None = None) -> MarketContext:
    context_path = Path(__file__).parent.parent / "data" / "market_context.json"
    if context_path.exists():
        return MarketContext.model_validate_json(context_path.read_text(encoding="utf-8"))
    if snapshot is None:
        snapshot_path = Path(__file__).parent.parent / "data" / "market_snapshot.json"
        snapshot = Snapshot.model_validate_json(snapshot_path.read_text(encoding="utf-8"))
    return build_market_context(snapshot, report_type)



def _build_portfolio_block(portfolio: Portfolio) -> str:
    """Format the portfolio as a prompt preamble."""
    lines = [
        "## ⚠️ 您的目前持股與投資部位 — 供今日交易計畫決策參考",
        "請針對以下持股輸出 monitoring status，不要生成每日買賣指令：",
        "",
    ]

    if portfolio.tw_positions:
        lines += [
            "### 台股持股",
            "| Ticker | 名稱 | 持股數量 | 買進均價 | 備註 |",
            "|--------|------|---------|---------|------|"
        ]
        for pos in portfolio.tw_positions:
            lines.append(f"| {pos.ticker} | {pos.name} | {pos.shares} | {pos.cost_basis} | {pos.note} |")
        lines.append("")

    if portfolio.us_positions:
        lines += [
            "### 美股持股",
            "| Ticker | 名稱 | 持股數量 | 買進均價 | 備註 |",
            "|--------|------|---------|---------|------|"
        ]
        for pos in portfolio.us_positions:
            lines.append(f"| {pos.ticker} | {pos.name} | {pos.shares} | {pos.cost_basis} | {pos.note} |")
        lines.append("")

    if portfolio.available_cash:
        lines.append(f"**可用資金 (Available Cash)**: {portfolio.available_cash}")
    else:
        lines.append("**可用資金 (Available Cash)**: ⚠️ 未設定；禁止換算精確買進股數，標示 SIZE_NOT_COMPUTED。")
    lines.append("")

    if portfolio.portfolio_notes:
        lines.append(f"**持股說明**: {portfolio.portfolio_notes}")
        lines.append("")

    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def check_report_already_generated(report_type: str) -> bool:
    """Return True if a report of report_type for the same market date already exists."""
    return bool(reports_for_market_date(report_type))


def reports_for_market_date(report_type: str) -> list[Path]:
    reports_dir = Path(__file__).parent.parent / "reports"
    if not reports_dir.exists():
        return []
    mdate = get_market_date(report_type)
    date_str = mdate.strftime("%Y%m%d")
    files = list(reports_dir.glob(f"{report_type}_{date_str}_*.md"))
    if not files:
        files = list(reports_dir.glob(f"{report_type}_{date_str}*.md"))
    if files:
        log.info("report file exists for market date; delivery state remains independent",
                 extra={"report_type": report_type, "market_date": date_str,
                        "existing": files[0].name})
    return sorted(files, reverse=True)


def save_report(report: str, report_type: str) -> Path:
    reports_dir = Path(__file__).parent.parent / "reports"
    reports_dir.mkdir(exist_ok=True)
    now = datetime.now(tz=TPE)
    mdate = get_market_date(report_type)
    filepath = reports_dir / f"{report_type}_{mdate.strftime('%Y%m%d')}_{now.strftime('%H%M%S')}.md"
    filepath.write_text(report, encoding="utf-8")
    return filepath

# ── Telegram ───────────────────────────────────────────────────────────────────

def _split_message(text: str, max_len: int = 4096) -> list[str]:
    """Split at paragraph/line boundaries without dropping report content."""
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    current = ""
    paragraphs = re.split(r"(\n\n+)", text)
    for part in paragraphs:
        if not part:
            continue
        if len(current) + len(part) <= max_len:
            current += part
            continue
        if current.strip():
            chunks.append(current.rstrip())
            current = ""
        while len(part) > max_len:
            boundary = part.rfind("\n", 0, max_len)
            if boundary <= 0:
                boundary = part.rfind(" ", 0, max_len)
            if boundary <= 0:
                boundary = max_len
            chunks.append(part[:boundary].rstrip())
            part = part[boundary:].lstrip()
        current = part
    if current.strip():
        chunks.append(current.rstrip())
    return chunks


def _clean_markdown_for_telegram_report(text: str) -> str:
    """Format markdown report specifically to look beautiful, table-free, and header-clean on Telegram."""
    import re

    def _flush_table(headers: list[str], rows: list[list[str]], out_lines: list[str]):
        if not headers or not rows:
            return
        is_quote_table = (
            len(headers) >= 3 and headers[0] in ("標的", "Symbol", "代號", "股票")
            and headers[1] in ("最新報價", "收盤價", "最新", "收盤", "Quote")
        )
        if not is_quote_table:
            for parts in rows:
                row_items = []
                for header, val in zip(headers, parts):
                    if val and val != "⚠️ 未取得" and val != "None":
                        row_items.append(f"{header}: *{val}*")
                if row_items:
                    out_lines.append("• " + ", ".join(row_items))
            return

        # If table has more than 7 rows (like the 12-item watchlist dump):
        if len(rows) > 7:
            benchmark_kw = ("TAIEX", "加權指數", "2330", "台積電", "SPX", "S&P", "NDX", "Nasdaq", "納斯達克", "DJI", "道瓊", "SOX", "費半", "費城半導體", "NVDA", "輝達")
            benchmark_rows = []
            mover_candidates = []
            for parts in rows:
                name = parts[0]
                price = parts[1]
                chg_str = parts[2]
                extra = " ｜ ".join(p for p in parts[3:] if p and p not in ("⚠️ 未取得", "None", "—"))
                row_str = f"• *{name}*: {price} ({chg_str})" + (f" ｜ {extra}" if extra else "")
                m = re.search(r'([+-]?\d+(?:\.\d+)?)%?', chg_str)
                pct_val = abs(float(m.group(1))) if m else 0.0

                if any(kw in name for kw in benchmark_kw):
                    benchmark_rows.append(row_str)
                else:
                    mover_candidates.append((pct_val, row_str))

            mover_candidates.sort(key=lambda x: x[0], reverse=True)
            selected_movers = [r for val, r in mover_candidates if val >= 1.0]
            if not selected_movers and mover_candidates:
                selected_movers = [r for _, r in mover_candidates[:3]]

            combined = benchmark_rows + [r for r in selected_movers if r not in benchmark_rows]
            final_rows = combined[:6] if len(combined) > 6 else combined
            out_lines.extend(final_rows)
        else:
            for parts in rows:
                name = parts[0]
                price = parts[1]
                chg = parts[2]
                extra = " ｜ ".join(p for p in parts[3:] if p and p not in ("⚠️ 未取得", "None", "—"))
                row_str = f"• *{name}*: {price} ({chg})" + (f" ｜ {extra}" if extra else "")
                out_lines.append(row_str)

    lines = text.splitlines()
    cleaned_lines = []
    in_table = False
    headers = []
    table_rows = []

    for line in lines:
        stripped = line.strip()
        # Detect table separator row (e.g. |---|---|)
        if stripped.startswith("|") and "-" in stripped and not any(c.isalnum() for c in stripped):
            continue
        # Parse active table data row
        if stripped.startswith("|") and stripped.endswith("|"):
            parts = [p.strip() for p in stripped.split("|")[1:-1]]
            if not in_table:
                # This is the header row
                headers = parts
                table_rows = []
                in_table = True
                cleaned_lines.append("")
                continue
            else:
                table_rows.append(parts)
                continue
        else:
            if in_table:
                in_table = False
                _flush_table(headers, table_rows, cleaned_lines)
                headers = []
                table_rows = []
                cleaned_lines.append("")

        # 2. Scrub headers wrapped with bold (e.g. ## **Title** -> *Title*)
        line = re.sub(r'^\s*#+\s*\**([^*]+)\**\s*$', r'*\1*', line)
        # 3. Scrub standard markdown headers (e.g. ## Title -> *Title*)
        line = re.sub(r'^\s*#+\s*(.*)$', r'*\1*', line)
        # 4. Filter residual raw heading artifacts
        line = line.replace("## **", "*").replace("### **", "*").replace("#### **", "*")
        line = line.replace("** ##", "*").replace("** ###", "*")
        # 5. Convert *** and ** to simple asterisk * for TG bold
        line = line.replace("***", "*").replace("**", "*")
        cleaned_lines.append(line)

    if in_table:
        _flush_table(headers, table_rows, cleaned_lines)

    # 6. Scrub forbidden engineering terms and drop unresolved diagnostic lines
    filtered_lines = []
    for line in cleaned_lines:
        if "UNRESOLVED" in line:
            continue
        filtered_lines.append(line)

    cleaned_text = "\n".join(filtered_lines)

    forbidden_terms = [
        "[OBSERVED] ", "[SUPPORTED_ASSOCIATION] ",
        "[OBSERVED]", "[SUPPORTED_ASSOCIATION]",
        "；此為關聯性觀察，未推定單一因果。", "；此為關聯性觀察，未推定單一因果",
        "；比較基準為前一正式收盤。", "；比較基準為前一正式收盤",
        "；皆以各自前一正式收盤為基準。", "；皆以各自前一正式收盤為基準",
        "canonical", "confidence contract", "artifact",
    ]
    for ft in forbidden_terms:
        cleaned_text = cleaned_text.replace(ft, "")
    cleaned_text = cleaned_text.replace("session-over-session", "單日變化")
    cleaned_text = cleaned_text.replace("前一已完成 TWSE session", "前一交易日")
    cleaned_text = cleaned_text.replace("較前一 session", "較前一交易日")
    cleaned_text = cleaned_text.replace("億台幣", "億元")

    # Collapse multiple blank lines
    cleaned_text = re.sub(r'\n{3,}', '\n\n', cleaned_text)
    return cleaned_text.strip()


def telegram_destination_fingerprint(chat_id: str) -> str:
    return hashlib.sha256(str(chat_id).strip().encode("utf-8")).hexdigest()[:8]


def _send_telegram_product(text: str, *, product: str, dry_run: bool = False,
                           chunks_override: list[str] | None = None, plain_text: bool = False) -> dict:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_ids = [cid.strip() for cid in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if cid.strip()]
    if not dry_run and (not token or not chat_ids):
        raise RuntimeError("Telegram delivery credentials missing")
    chunks = chunks_override if chunks_override is not None else _split_message(text)
    fingerprints = [telegram_destination_fingerprint(cid) for cid in chat_ids]
    result = {"ok": False, "product": product, "destination_fingerprints": fingerprints,
              "message_ids": [], "message_lengths": [len(chunk) for chunk in chunks],
              "chunk_count": len(chunks), "destination_count": len(chat_ids)}
    if dry_run:
        result.update({"ok": True, "simulated": True})
        log.info("Telegram delivery simulated", extra={"product": product,
                                                       "destination_fingerprints": fingerprints,
                                                       "chunk_count": len(chunks),
                                                       "message_lengths": result["message_lengths"]})
        return result
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for cid in chat_ids:
        fingerprint = telegram_destination_fingerprint(cid)
        for i, chunk in enumerate(chunks):
            try:
                body = {"chat_id": cid, "text": chunk}
                if not plain_text:
                    body["parse_mode"] = "Markdown"
                response = requests.post(url, json=body, timeout=30)
                if response.status_code != 200:
                    log.warning("Telegram Markdown rejected; retrying plain text", extra={"product": product,
                                "destination_fingerprint": fingerprint, "chunk": i + 1,
                                "chunk_count": len(chunks), "http_status": response.status_code})
                    response = requests.post(url, json={"chat_id": cid, "text": chunk}, timeout=30)
                response.raise_for_status()
                payload = response.json()
                if payload.get("ok") is not True or not isinstance((payload.get("result") or {}).get("message_id"), int):
                    raise RuntimeError("Telegram response lacked confirmed message_id")
                returned_chat = (payload.get("result") or {}).get("chat") or {}
                if cid.lstrip("-").isdigit() and str(returned_chat.get("id")) != cid:
                    raise RuntimeError("Telegram confirmed a different destination")
                message_id = payload["result"]["message_id"]
                result["message_ids"].append(message_id)
                log.info("Telegram chunk confirmed", extra={"product": product, "ok": True,
                         "message_id": message_id, "destination_fingerprint": fingerprint,
                         "message_length": len(chunk), "chunk": i + 1, "chunk_count": len(chunks)})
            except (requests.RequestException, ValueError, RuntimeError) as exc:
                log.error("Telegram chunk unconfirmed", extra={"product": product,
                          "destination_fingerprint": fingerprint, "chunk": i + 1,
                          "chunk_count": len(chunks), "error_type": type(exc).__name__})
                raise RuntimeError(f"{product} Telegram delivery unconfirmed") from None
            if i < len(chunks) - 1:
                time.sleep(0.5)
    result["ok"] = True
    return result


def public_telegram_payload(report: str, report_type: str, *, at: datetime | None = None) -> str:
    title = REPORT_TITLES[report_type]
    now_str = (at or datetime.now(tz=TPE)).astimezone(TPE).strftime("%Y-%m-%d %H:%M TPE")
    header = f"📊 *{title}* ｜ {now_str}\n{'─' * 30}\n\n"
    return header + _clean_markdown_for_telegram_report(report)


def send_telegram(report: str, report_type: str, *, dry_run: bool = False,
                  at: datetime | None = None) -> dict:
    return _send_telegram_product(public_telegram_payload(report, report_type, at=at),
                                  product="PUBLIC_REPORT", dry_run=dry_run)

# ── Email ──────────────────────────────────────────────────────────────────────

def _md_to_html(md: str) -> str:
    """Convert Markdown to HTML using the standard markdown library."""
    html_content = markdown.markdown(md, extensions=["tables", "fenced_code"])
    return f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{{font-family:'PingFang TC','Noto Sans TC','Microsoft JhengHei',sans-serif;
     max-width:860px;margin:0 auto;padding:24px 20px;background:#f8f9fa;color:#1a1a2e;line-height:1.75}}
h1{{color:#1a1a2e;border-bottom:2px solid #3d5a99;padding-bottom:8px;margin-top:0}}
h2{{color:#2c3e50;border-bottom:1px solid #bdc3c7;padding-bottom:4px;margin-top:32px}}
h3{{color:#34495e;margin-top:20px}}
pre{{background:#272822;color:#f8f8f2;padding:14px;border-radius:6px;overflow-x:auto;font-size:.87em}}
code{{background:#f0f0f0;color:#c0392b;padding:2px 5px;border-radius:3px;font-size:.88em}}
pre code{{background:transparent;color:inherit;padding:0}}
table{{border-collapse:collapse;width:100%;margin:14px 0;font-size:.92em}}
th{{background:#3d5a99;color:#fff;padding:8px 12px;text-align:left}}
td{{border:1px solid #dce1e7;padding:7px 12px}}
tr:nth-child(even){{background:#f2f4f8}}
hr{{border:none;border-top:1px solid #ddd;margin:22px 0}}
p{{margin:.8em 0}}
ul, ol{{padding-left:20px;margin:.8em 0}}
li{{margin:.4em 0}}
strong{{color:#1a1a2e}}
</style>
</head>
<body>
{html_content}
</body>
</html>"""


def send_email(report: str, report_type: str) -> None:
    if os.getenv("ENABLE_EMAIL", "false").lower() not in ("true", "1", "yes"):
        log.info("Email delivery disabled (ENABLE_EMAIL not set to true) — skipping")
        return

    smtp_server = os.getenv("EMAIL_SMTP_SERVER", "").strip()
    if not smtp_server:
        log.info("Email not configured — skipping")
        return

    smtp_port = int(os.getenv("EMAIL_SMTP_PORT", "587") or "587")
    username = os.getenv("EMAIL_USERNAME", "").strip()
    password = os.getenv("EMAIL_PASSWORD", "").strip()
    email_to_raw = os.getenv("EMAIL_TO", "").strip()

    if not (username and password and email_to_raw):
        log.info("Email incomplete credentials — skipping")
        return

    recipients = [e.strip() for e in email_to_raw.split(",") if e.strip()]
    title = REPORT_TITLES[report_type]
    mdate = get_market_date(report_type)
    date_str = mdate.strftime("%Y-%m-%d")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"【市場報告】{title} {date_str}"
    msg["From"] = username
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(report, "plain", "utf-8"))
    msg.attach(MIMEText(_md_to_html(report), "html", "utf-8"))

    try:
        with smtplib.SMTP(smtp_server, smtp_port, timeout=30) as srv:
            srv.ehlo()
            srv.starttls()
            srv.login(username, password)
            srv.sendmail(username, recipients, msg.as_string())
        log.info("Email sent", extra={"recipients": recipients})
    except Exception:
        log.error("Email send failed", exc_info=True)

# ── Private portfolio advice (Telegram/Email only — NEVER saved to reports/) ──

ADVICE_MARKET_FOCUS = {
    "tw_open":  "台股持股為主（給出今日開盤後的操作計畫），美股持股一句話帶過",
    "tw_close": "台股持股為主（給出隔日操作計畫），美股持股一句話帶過",
    "us_open":  "美股持股為主（給出今晚開盤後的操作計畫），台股持股一句話帶過",
    "us_close": "美股持股為主（檢討昨夜表現並給出後續計畫），台股持股一句話帶過",
}
ADVICE_STATUSES = ("NO_MATERIAL_CHANGE", "WATCH", "ACTION_REVIEW", "DATA_BLOCKED")
ZH_ADVICE_SECTIONS = ("持股監控", "需要關注", "值得重新檢視", "建議重新檢視", "【今天結論】")


def _build_advice_prompt(report: str, portfolio: Portfolio,
                         snapshot: "Snapshot | None", report_type: str) -> str:
    parts = [
        "你是專業投資組合監控助理。根據已驗證市場快照、使用者實際持股與今日市場報告，"
        "產出短版 PORTFOLIO ACTION BRIEF。",
        "",
        "規則（必須遵守）：",
        f"- 本次重點：{ADVICE_MARKET_FOCUS.get(report_type, '全部持股')}",
        "- 只能使用以下狀態：NO_MATERIAL_CHANGE / WATCH / ACTION_REVIEW / DATA_BLOCKED",
        "- 不要每天硬給買進、賣出、停損；多數持股可歸入 NO_MATERIAL_CHANGE",
        "- ACTION_REVIEW 僅用於新資訊、財報、估值、組合風險或重大事件已明顯改變時",
        "- 數字觸發條件必須有 trigger_basis 與 source；不可憑直覺編支撐/壓力/停損",
        "- available_cash 缺失時，不得換算精確加碼股數，標示 SIZE_NOT_COMPUTED",
        "- cost_basis 僅供紀錄，不得把帳面損益本身當主要買賣理由",
        "- 所有持股價格只能引用快照 QuoteObservation；缺失或 DATA_BLOCKED 時不得產生數字建議",
        "- Telegram 格式：禁止 # 標題，用粗體與 📌💡📈📉 emoji 分段，段落間空行，600 字內",
        "",
        "輸出結構固定：",
        "💼 持股 Action Brief",
        "As of / Market session / Data quality",
        "1. ACTION QUEUE（只列需要決策的持股）",
        "2. WATCHLIST（有事件但不需立即動作）",
        "3. NO MATERIAL CHANGE（ticker 簡表）",
        "4. UPCOMING PORTFOLIO EVENTS",
        "5. DATA QUALITY（只有限制時顯示）",
        "",
    ]
    if snapshot:
        parts.append(_build_snapshot_block(snapshot))
    parts += [
        _build_portfolio_block(portfolio),
        "=== 今日市場報告 ===",
        report,
        "=== 報告結束 ===",
        "",
        "請輸出持股操作建議：",
    ]
    return "\n".join(parts)


def private_telegram_payload(advice: str, report_type: str) -> str:
    now_str = datetime.now(tz=TPE).strftime("%Y-%m-%d %H:%M TPE")
    header = f"💼 *持股操作建議*（私訊限定）｜ {now_str}\n{'─' * 30}\n\n"
    return header + _clean_markdown_for_telegram_report(advice)


def send_advice_telegram(advice: str, report_type: str, *, dry_run: bool = False) -> dict:
    return _send_telegram_product(private_telegram_payload(advice, report_type),
                                  product="PRIVATE_ADVICE", dry_run=dry_run)


def send_decision_telegram(brief, *, dry_run: bool = False) -> dict:
    from portfolio_decisions import decision_telegram_chunks
    return _send_telegram_product("", product="PRIVATE_DECISIONS", dry_run=dry_run,
                                  chunks_override=decision_telegram_chunks(brief), plain_text=True)


def send_advice_email(advice: str, report_type: str) -> None:
    if os.getenv("ENABLE_EMAIL", "false").lower() not in ("true", "1", "yes"):
        log.info("Email advice delivery disabled (ENABLE_EMAIL not set to true) — skipping")
        return
    smtp_server = os.getenv("EMAIL_SMTP_SERVER", "").strip()
    username = os.getenv("EMAIL_USERNAME", "").strip()
    password = os.getenv("EMAIL_PASSWORD", "").strip()
    email_to_raw = os.getenv("EMAIL_TO", "").strip()
    if not (smtp_server and username and password and email_to_raw):
        return
    recipients = [e.strip() for e in email_to_raw.split(",") if e.strip()]
    smtp_port = int(os.getenv("EMAIL_SMTP_PORT", "587") or "587")
    date_str = get_market_date(report_type).strftime("%Y-%m-%d")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"【持股建議】{REPORT_TITLES[report_type]} {date_str}"
    msg["From"] = username
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(advice, "plain", "utf-8"))
    msg.attach(MIMEText(_md_to_html(advice), "html", "utf-8"))
    try:
        with smtplib.SMTP(smtp_server, smtp_port, timeout=30) as srv:
            srv.ehlo()
            srv.starttls()
            srv.login(username, password)
            srv.sendmail(username, recipients, msg.as_string())
        log.info("portfolio advice sent via Email")
    except Exception:
        log.error("advice Email send failed", exc_info=True)


def send_operational_notice(text: str, report_type: str) -> None:
    title = REPORT_TITLES[report_type]
    msg = f"⚠️ {title}｜持股操作建議暫停\n\n{text}"
    _send_telegram_product(msg, product="PRIVATE_ADVICE_NOTICE")
    send_email(msg, report_type)


def _portfolio_tickers(raw: dict | None) -> set[str]:
    if not raw:
        return set()
    tickers: set[str] = set()
    for bucket in ("tw_positions", "us_positions"):
        for pos in raw.get(bucket, []):
            ticker = str(pos.get("ticker", "")).upper().strip()
            if ticker:
                tickers.add(resolve_instrument(ticker).canonical_symbol)
    return tickers


def validate_portfolio_quotes(raw: dict | None, snapshot: "Snapshot | None",
                              portfolio_context=None) -> tuple[bool, str]:
    positions = portfolio_context.positions if portfolio_context is not None else []
    if not positions and not portfolio_store.has_positions(raw):
        return False, "加密持股不存在或沒有持股，略過持股操作建議。"
    if snapshot is None:
        return False, "缺少 market_snapshot.json，無法驗證持股行情。"
    coverage = snapshot.portfolio_quote_coverage
    if coverage is None:
        return False, "snapshot 未包含 portfolio_quote_coverage，無法確認持股行情覆蓋。"
    if not coverage.is_full:
        unresolved = [*coverage.stale, *coverage.missing, *coverage.unsupported]
        reasons = []
        for item in unresolved:
            reason = item.reason
            if "differs from expected" in reason or "session" in reason:
                r_short = "session mismatch"
            elif item.state == "UNSUPPORTED":
                r_short = "unsupported provider"
            else:
                r_short = "provider failure"
            reasons.append(f"{item.canonical_symbol}（{r_short}）")
        missing_text = f"\n未取得：{'、'.join(reasons)}" if reasons else ""
        return False, (
            f"持股行情：{coverage.covered_positions}/{coverage.expected_positions}，"
            f"覆蓋率 {coverage.coverage_ratio:.0%} 低於 100%（{coverage.status}）。"
            f"{missing_text}"
        )
    expected = len(positions) if positions else len(_portfolio_tickers(raw))
    if coverage.expected_positions != expected:
        return False, f"持股 quote coverage 預期部位數 {coverage.expected_positions} 與 canonical active positions {expected} 不一致。"
    bad: list[str] = []
    tickers = [position.ticker for position in positions] if positions else sorted(_portfolio_tickers(raw))
    for ticker in tickers:
        obs = snapshot.quote_observations.get(ticker)
        if obs is None:
            bad.append(f"{ticker}: missing quote observation")
        elif obs.quality_status not in ("VALID",):
            bad.append(f"{ticker}: {obs.quality_status}")
    if bad:
        return False, "持股行情驗證未通過：" + "; ".join(sorted(bad))
    return True, "OK"


def _position_quantities(raw: dict | None) -> dict[str, float]:
    quantities: dict[str, float] = {}
    if not raw:
        return quantities
    for bucket in ("tw_positions", "us_positions"):
        for pos in raw.get(bucket, []):
            ticker = str(pos.get("ticker", "")).upper().strip()
            if ticker:
                key = resolve_instrument(ticker).canonical_symbol
                quantities[key] = quantities.get(key, 0.0) + float(pos.get("shares", 0) or 0)
    return quantities


def validate_private_advice_text(advice: str, raw: dict | None,
                                 snapshot: "Snapshot | None") -> tuple[bool, str]:
    valid_markers = ADVICE_STATUSES + ZH_ADVICE_SECTIONS
    if not any(status in advice for status in valid_markers):
        return False, "private advice 未包含監控狀態標記，可能仍是舊式任意交易建議。"

    cash_missing = not raw or raw.get("available_cash") in (None, "", "未設定")
    if cash_missing and re.search(r"(加碼|買進)\s*[0-9,.]+(?:\s*)(股|shares?)", advice, flags=re.I):
        return False, "available_cash 缺失，但 private advice 仍產生精確買進股數。"

    quantities = _position_quantities(raw)
    for ticker, owned in quantities.items():
        pattern = rf"{re.escape(ticker)}[\s\S]{{0,80}}(?:賣出|減碼)[^\d]{{0,20}}([0-9,.]+)\s*(?:股|shares?)"
        for match in re.finditer(pattern, advice, flags=re.I):
            proposed = float(match.group(1).replace(",", ""))
            if proposed > owned:
                return False, f"{ticker} 建議賣出 {proposed:g} 股，超過持股 {owned:g} 股。"

    if snapshot:
        missing_refs = []
        for ticker in quantities:
            if ticker not in snapshot.quote_observations:
                missing_refs.append(ticker)
        if missing_refs:
            return False, "private advice 引用缺少 QuoteObservation 的持股：" + ", ".join(sorted(missing_refs))

    return True, "OK"


def write_advice_audit(report_type: str, status: str, reason: str,
                       snapshot: "Snapshot | None") -> None:
    data_dir = Path(__file__).parent.parent / "data"
    data_dir.mkdir(exist_ok=True)
    payload = {
        "report_type": report_type,
        "status": status,
        "reason": reason,
        "generated_at": datetime.now(tz=TPE).isoformat(),
        "portfolio_quote_coverage": snapshot.portfolio_quote_coverage.model_dump(mode="json") if snapshot and snapshot.portfolio_quote_coverage else None,
        "data_quality": snapshot.data_quality if snapshot else {},
    }
    (data_dir / "portfolio_advice_audit.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def run_portfolio_advice(report: str, report_type: str,
                         snapshot: "Snapshot | None", model: str,
                         *, deliver: bool = True) -> str:
    """Generate private portfolio artifacts; only send when ``deliver`` is true.

    Generate-only workflows must still leave an audit trail in the protected
    Actions artifact.  Holdings never enter the committed public report.
    """
    portfolio_context = load_authoritative_portfolio()
    if portfolio_context.source != "PIOS_PORTFOLIO_SNAPSHOT" or not portfolio_context.positions:
        write_advice_audit(report_type, "BLOCKED", "authoritative active positions unavailable", snapshot)
        return "BLOCKED"
    try:
        context = load_market_context(report_type, snapshot)
    except (OSError, ValueError):
        context = MarketContext(run_id="private-missing-quotes", report_type=report_type,
            market_date=get_market_date(report_type).strftime("%Y-%m-%d"),
            generated_at=datetime.now(tz=timezone.utc), market_session="PREVIOUS_CLOSE", quotes={})
    force_event_refresh = {
        pos.ticker.upper()
        for pos in portfolio_context.positions
        if (
            (context.quotes.get(pos.instrument_id) or context.quotes.get(pos.ticker))
            and abs((context.quotes.get(pos.instrument_id) or context.quotes.get(pos.ticker)).change_pct) >= 7.0
        )
    }
    try:
        event_facts, upcoming_events = fetch_portfolio_events(
            portfolio=portfolio_context, model=model, force_refresh_tickers=force_event_refresh,
            network_scope="forced_only",
        )
    except Exception as exc:
        log.warning("private event evidence unavailable", extra={"error_type": type(exc).__name__})
        event_facts, upcoming_events = [], []
    consumed_at = datetime.now(tz=timezone.utc)
    # Retain the old anomaly artifact as supplementary audit only.
    brief = build_action_brief(
        context,
        portfolio_context,
        verified_events=event_facts,
        upcoming_events=upcoming_events,
        as_of=consumed_at,
    )
    from portfolio_decisions import (build_decision_brief, load_decision_evidence,
                                     render_decision_brief, validate_decision_brief)
    try:
        decision_evidence = load_decision_evidence()
    except (OSError, ValueError) as exc:
        decision_evidence = []
        log.warning("private research input unavailable", extra={"error_type": type(exc).__name__})
    decisions = build_decision_brief(context, portfolio_context, events=event_facts,
                                    evidence=decision_evidence, as_of=consumed_at)
    ok, reason = validate_decision_brief(decisions, context, portfolio_context)
    if not ok:
        log.warning("private advice validation failed", extra={"reason": reason})
        write_advice_audit(report_type, "BLOCKED", reason, snapshot)
        if deliver:
            send_operational_notice(f"{reason}\n\n已停止傳送持股建議。", report_type)
        return "BLOCKED"
    write_advice_audit(report_type, "VALIDATED", reason, snapshot)
    data_dir = Path(__file__).parent.parent / "data"
    (data_dir / "portfolio_action_brief.json").write_text(brief.model_dump_json(indent=2), encoding="utf-8")
    payload = decisions.model_dump_json(indent=2)
    (data_dir / "portfolio_decision_brief.json").write_text(payload, encoding="utf-8")
    key = os.getenv("PORTFOLIO_KEY", "").strip()
    if key:
        from cryptography.fernet import Fernet
        try:
            (data_dir / "portfolio_decision_brief.json.enc").write_bytes(Fernet(key.encode()).encrypt(payload.encode()))
        except ValueError:
            log.warning("private decision artifact encryption key invalid; plaintext is not uploaded")
    else:
        log.warning("private decision artifact not uploaded without encryption key")
    advice = render_decision_brief(decisions)
    if deliver:
        send_decision_telegram(decisions)
        send_advice_email(advice, report_type)
        return "SENT"
    return "SKIPPED"


def _report_result(report_type: str, market_date: str, validation: str, delivery: str,
                   message_ids: list[int], private_advice: str, terminal_state: str, reason: str) -> None:
    result = {"report_type": report_type, "market_date": market_date,
              "public_validation": validation, "public_delivery": delivery,
              "telegram_message_ids": message_ids, "private_advice": private_advice,
              "terminal_state": terminal_state, "reason": reason}
    path = Path(__file__).parent.parent / "data" / "report_result.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    if not os.getenv("GITHUB_ACTIONS"):
        print("REPORT_RESULT " + " ".join(f"{key}={','.join(map(str, value)) or 'none' if isinstance(value, list) else value}"
                                              for key, value in result.items()), flush=True)


def deliver_validated_report(report: str, report_type: str, context: MarketContext,
                             draft, snapshot: Snapshot | None, model: str,
                             idempotency_key: str, *, dry_run: bool = False,
                             simulated_deliveries: set[str] | None = None,
                             at: datetime | None = None) -> dict:
    """Use the same validation and payload path for production and no-send replay."""
    from validate_report import (validate_numeric_provenance,
                                 validate_rendered_report_structure, validate_render_matches_draft)

    market_date = idempotency_key.split(":", 1)[-1]
    simulated_deliveries = simulated_deliveries if simulated_deliveries is not None else set()
    if idempotency_key in simulated_deliveries or (not dry_run and delivery_state.already_delivered(idempotency_key)):
        _report_result(report_type, market_date, "PASS", "SKIPPED", [], "SKIPPED",
                       "SIMULATED" if dry_run else "DELIVERED",
                       "ALREADY_SIMULATED" if dry_run else "ALREADY_DELIVERED")
        return {"public_delivery": "SKIPPED", "private_advice": "SKIPPED", "message_ids": []}

    draft_ok, draft_reason = validate_public_draft(draft, context)
    structure_ok, structure_errors = validate_rendered_report_structure(report, report_type)
    numeric_ok, numeric_errors = validate_numeric_provenance(report, context)
    render_ok, render_reason = validate_render_matches_draft(report, draft)
    if report != draft.rendered_markdown or not all((draft_ok, structure_ok, numeric_ok, render_ok)):
        reason = "DRAFT_MISMATCH" if report != draft.rendered_markdown else "DRAFT_INVALID" if not draft_ok else "STRUCTURE_INVALID" if not structure_ok else "NUMERIC_PROVENANCE_INVALID" if not numeric_ok else "RENDER_MISMATCH"
        log.error("Public report validation blocked", extra={"reason": reason, "draft_reason": draft_reason,
                                                           "structure_errors": structure_errors[:3],
                                                           "numeric_errors": numeric_errors[:3],
                                                           "render_reason": render_reason})
        if not dry_run:
            delivery_state.mark_state(idempotency_key, "BLOCKED")
        _report_result(report_type, market_date, "FAIL", "SKIPPED", [], "SKIPPED", "VALIDATION_BLOCKED", reason)
        return {"public_delivery": "SKIPPED", "private_advice": "SKIPPED", "message_ids": [], "reason": reason}

    if not dry_run:
        delivery_state.mark_state(idempotency_key, "VALIDATED")
        delivery_state.mark_state(idempotency_key, "DELIVERING")
    try:
        telegram = send_telegram(report, report_type, dry_run=dry_run, at=at)
        if not telegram.get("ok"):
            raise RuntimeError("public Telegram delivery unconfirmed")
    except Exception as exc:
        if not dry_run:
            delivery_state.mark_state(idempotency_key, "FAILED")
        log.error("Public report delivery failed", extra={"error_type": type(exc).__name__})
        _report_result(report_type, market_date, "PASS", "FAILED", [], "SKIPPED", "DELIVERY_FAILED", "TELEGRAM_UNCONFIRMED")
        return {"public_delivery": "FAILED", "private_advice": "SKIPPED", "message_ids": [], "reason": "TELEGRAM_UNCONFIRMED"}

    message_ids = list(telegram.get("message_ids", []))
    if dry_run:
        simulated_deliveries.add(idempotency_key)
        _report_result(report_type, market_date, "PASS", "SKIPPED", [], "SKIPPED", "SIMULATED", "NO_SEND")
        return {"public_delivery": "SIMULATED", "private_advice": "SKIPPED", "message_ids": [],
                "telegram": telegram}

    delivery_state.mark_state(idempotency_key, "DELIVERED", telegram_result=telegram)
    private_status = "SKIPPED"
    try:
        send_email(report, report_type)
        private_status = run_portfolio_advice(report, report_type, snapshot, model, deliver=True)
    except Exception as exc:
        private_status = "BLOCKED"
        log.error("Private advice degraded after public delivery", extra={"error_type": type(exc).__name__})
    _report_result(report_type, market_date, "PASS", "SENT", message_ids, private_status, "DELIVERED", "OK")
    return {"public_delivery": "SENT", "private_advice": private_status, "message_ids": message_ids,
            "telegram": telegram}


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="Institutional market report generator")
    parser.add_argument("report_type", choices=REPORT_TYPES, help="Report type to generate")
    parser.add_argument("--generate-only", action="store_true",
                        help="Generate and save report without delivery")
    parser.add_argument("--deliver-existing", action="store_true",
                        help="Deliver latest saved report after validation")
    parser.add_argument("--dry-run", action="store_true",
                        help="Exercise validation and Telegram payload without external send")
    args = parser.parse_args()

    if not args.deliver_existing and not os.getenv("GEMINI_API_KEY"):
        log.error("GEMINI_API_KEY is not set")
        sys.exit(1)

    model = (os.getenv("REPORT_MODEL") or "gemini-2.0-flash").strip()
    report_type: str = args.report_type

    if args.generate_only and args.deliver_existing:
        log.error("--generate-only and --deliver-existing are mutually exclusive")
        sys.exit(2)

    if args.deliver_existing:
        same_day_reports = reports_for_market_date(report_type)
        if not same_day_reports:
            log.error("no same-market-date report to deliver", extra={"report_type": report_type})
            sys.exit(1)
        report = same_day_reports[0].read_text(encoding="utf-8")
        snapshot = None
        snapshot_path = Path(__file__).parent.parent / "data" / "market_snapshot.json"
        try:
            snapshot = Snapshot.model_validate_json(snapshot_path.read_text(encoding="utf-8"))
        except Exception:
            log.warning("snapshot unavailable during delivery", exc_info=True)
        idempotency_key = f"{report_type}:{get_market_date(report_type).strftime('%Y%m%d')}"
        if not args.dry_run:
            delivery_state.mark_state(idempotency_key, "VALIDATING")
        try:
            context = load_market_context(report_type, snapshot)
            draft_path = Path(__file__).parent.parent / "data" / "market_report_draft.json"
            from models import MarketReportDraft
            draft = MarketReportDraft.model_validate_json(draft_path.read_text(encoding="utf-8"))
        except Exception:
            if not args.dry_run:
                delivery_state.mark_state(idempotency_key, "BLOCKED")
            log.error("market context unavailable before delivery", exc_info=True)
            sys.exit(1)
        result = deliver_validated_report(report, report_type, context, draft, snapshot, model,
                                          idempotency_key, dry_run=args.dry_run)
        if result["public_delivery"] not in {"SENT", "SKIPPED", "SIMULATED"}:
            sys.exit(1)
        return

    # A generated file is not a delivery receipt.
    idempotency_key = f"{report_type}:{get_market_date(report_type).strftime('%Y%m%d')}"
    if not args.generate_only and not args.dry_run and delivery_state.already_delivered(idempotency_key):
        sys.exit(0)

    prompt = load_prompt(report_type)

    # NOTE: the portfolio is deliberately NOT injected into the main report —
    # reports are committed to a public repo. Position-aware advice is generated
    # separately below and delivered via Telegram/Email only.

    snapshot: Snapshot | None = None
    context: MarketContext | None = None
    snapshot_path = Path(__file__).parent.parent / "data" / "market_snapshot.json"
    try:
        snapshot = Snapshot.model_validate_json(
            snapshot_path.read_text(encoding="utf-8"))
        context = load_market_context(report_type, snapshot)
        prompt = _build_snapshot_block(snapshot) + prompt
        log.info("snapshot injected", extra={
            "report_type": report_type,
            "requested_universe_coverage": snapshot.requested_universe_coverage,
            "validated_universe_coverage": snapshot.validated_universe_coverage,
            "tw": len(snapshot.tw_stocks),
            "us": len(snapshot.us_markets),
            "fx": len(snapshot.forex)})
    except FileNotFoundError:
        log.warning("no snapshot found — blocking report generation",
                    extra={"report_type": report_type})
        sys.exit(1)
    except ValidationError:
        log.error("snapshot invalid — blocking report generation", exc_info=True)
        sys.exit(1)

    max_tokens = MAX_OUTPUT_TOKENS.get(report_type, 16000)
    log.info("calling Gemini API", extra={
        "report_type": report_type, "model": model, "max_tokens": max_tokens})
    report = generate_report(prompt, model, report_type)
    if context is None:
        log.error("MarketContext missing — blocking report generation")
        sys.exit(1)

    if "新聞搜尋目前不可用" in report:
        context.degraded_mode = True
    session_note = f"\n\nMarket session: {human_session_label(report_type, context.market_session)}"
    draft = build_public_draft(context, report + session_note)
    ok, reason = validate_public_draft(draft, context)
    if not ok:
        log.error("public draft validation failed", extra={"reason": reason})
        sys.exit(1)
    data_dir = Path(__file__).parent.parent / "data"
    (data_dir / "market_report_draft.json").write_text(draft.model_dump_json(indent=2), encoding="utf-8")
    report = draft.rendered_markdown

    filepath = save_report(report, report_type)
    if not args.dry_run:
        delivery_state.mark_state(idempotency_key, "GENERATED")
    log.info("report saved", extra={"path": str(filepath)})

    # Produce audit/brief artifacts during generation as well as delivery.
    # This makes a manually dispatched dry run observable without sending a
    # portfolio message or placing private holdings in the public report.
    try:
        run_portfolio_advice(report, report_type, snapshot, model, deliver=False)
    except Exception:
        log.error("portfolio artifact stage failed", exc_info=True)

    if args.generate_only:
        log.info("generate-only mode: delivery deferred until validation passes")
        return

    result = deliver_validated_report(report, report_type, context, draft, snapshot, model,
                                      idempotency_key, dry_run=args.dry_run)
    if result["public_delivery"] not in {"SENT", "SKIPPED", "SIMULATED"}:
        sys.exit(1)


if __name__ == "__main__":
    main()
