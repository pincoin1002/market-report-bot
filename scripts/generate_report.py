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
from market_session import human_session_label, report_market_date
from models import MarketContext, Portfolio, Snapshot
import portfolio_store
from portfolio_context import EncryptedPortfolioProvider
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

def _prev_trade_date(mdate: datetime) -> datetime:
    """Previous weekday. Approximation for search queries only — holidays are
    acceptable noise since prices come from the snapshot, not from search."""
    prev = mdate - timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= timedelta(days=1)
    return prev


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
    return (text
            .replace("{{TODAY_DATE}}", mdate.strftime("%Y-%m-%d"))
            .replace("{{TODAY_WEEKDAY}}", weekday_map[mdate.weekday()])
            .replace("{{PREV_TRADE_DATE}}", _prev_trade_date(mdate).strftime("%Y-%m-%d")))


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
        log.warning("Generate report with Google Search failed. Retrying without search...", extra={"error": str(exc)})
        if os.getenv("ALLOW_UNGROUNDED_NEWS_FALLBACK", "").lower() == "true":
            return _call_gemini_api(prompt, model, report_type, use_search=False)
        return _verified_data_only_report(prompt, report_type)


def _verified_data_only_report(prompt: str, report_type: str) -> str:
    title = REPORT_TITLES[report_type]
    now_str = datetime.now(tz=TPE).strftime("%Y-%m-%d %H:%M TPE")
    return (
        f"# {title} {now_str}\n\n"
        "⚠️ 新聞搜尋目前不可用。本次不使用未 grounding 的模型記憶生成即時市場敘事。\n\n"
        "以下僅保留系統已抓取並注入 prompt 的驗證行情資料；缺少新聞脈絡時，不產生事件歸因或交易推論。\n\n"
        f"{prompt[:6000]}"
    )


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

    if snapshot.quote_observations:
        lines += [
            "**Quote Semantics / Data Quality**",
            "| Symbol | Session | Market Date | Retrieved | Provider | Quality | Quote ID |",
            "|--------|---------|-------------|-----------|----------|---------|----------|",
        ]
        for key, obs in snapshot.quote_observations.items():
            lines.append(
                f"| {key} | {obs.session} | {obs.market_date} | "
                f"{obs.retrieved_at.strftime('%Y-%m-%d %H:%M %Z')} | "
                f"{obs.provider} | {obs.quality_status} | {obs.quote_id} |"
            )
        lines.append("")

    lines += [
        "**重要：上述快照數字即為最終答案，撰寫報告時直接使用，禁止搜尋或修改。**",
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
        log.info("report already exists for market date — skipping duplicate delivery",
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
    chunks: list[str] = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        idx = text.rfind("\n", 0, max_len)
        if idx <= 0:
            idx = max_len
        chunks.append(text[:idx])
        text = text[idx:].lstrip("\n")
    return chunks


def _clean_markdown_for_telegram_report(text: str) -> str:
    """Format markdown report specifically to look beautiful, table-free, and header-clean on Telegram."""
    import re
    # 1. Parse tables into structured bullet points
    lines = text.splitlines()
    cleaned_lines = []
    in_table = False
    headers = []
    
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
                in_table = True
                cleaned_lines.append("")
                continue
            else:
                # This is a data row, convert to bulleted text
                row_items = []
                for header, val in zip(headers, parts):
                    if val and val != "⚠️ 未取得" and val != "None":
                        row_items.append(f"{header}: *{val}*")
                if row_items:
                    cleaned_lines.append(f"• " + ", ".join(row_items))
                continue
        else:
            if in_table:
                in_table = False
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

    cleaned_text = "\n".join(cleaned_lines)
    # Collapse multiple blank lines
    cleaned_text = re.sub(r'\n{3,}', '\n\n', cleaned_text)
    return cleaned_text.strip()


def send_telegram(report: str, report_type: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id_raw = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id_raw:
        log.info("Telegram not configured — skipping")
        return

    # Support multiple comma-separated chat/user IDs
    chat_ids = [cid.strip() for cid in chat_id_raw.split(",") if cid.strip()]

    title = REPORT_TITLES[report_type]
    now_str = datetime.now(tz=TPE).strftime("%Y-%m-%d %H:%M TPE")
    header = f"📊 *{title}* ｜ {now_str}\n{'─' * 30}\n\n"
    
    cleaned_report = _clean_markdown_for_telegram_report(report)
    chunks = _split_message(header + cleaned_report)

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for cid in chat_ids:
        for i, chunk in enumerate(chunks):
            try:
                resp = requests.post(url, json={"chat_id": cid, "text": chunk, "parse_mode": "Markdown"}, timeout=30)
                if resp.status_code != 200:
                    log.warning("Telegram Markdown send failed; retrying as plain text",
                                extra={"chat_id": cid, "chunk": i + 1,
                                       "total": len(chunks), "status": resp.status_code,
                                       "response": resp.text[:500]})
                    resp = requests.post(url, json={"chat_id": cid, "text": chunk}, timeout=30)
                resp.raise_for_status()
                log.info("Telegram chunk sent", extra={"chat_id": cid, "chunk": i + 1, "total": len(chunks)})
            except requests.RequestException:
                log.error("Telegram send failed", exc_info=True,
                          extra={"chat_id": cid, "chunk": i + 1, "total": len(chunks)})
            if i < len(chunks) - 1:
                time.sleep(0.5)

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


def send_advice_telegram(advice: str, report_type: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id_raw = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id_raw:
        return
    chat_ids = [cid.strip() for cid in chat_id_raw.split(",") if cid.strip()]
    now_str = datetime.now(tz=TPE).strftime("%Y-%m-%d %H:%M TPE")
    header = f"💼 *持股操作建議*（私訊限定）｜ {now_str}\n{'─' * 30}\n\n"
    chunks = _split_message(header + _clean_markdown_for_telegram_report(advice))
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for cid in chat_ids:
        for i, chunk in enumerate(chunks):
            try:
                resp = requests.post(url, json={"chat_id": cid, "text": chunk,
                                                "parse_mode": "Markdown"}, timeout=30)
                if resp.status_code != 200:   # Markdown parse issues → plain text
                    resp = requests.post(url, json={"chat_id": cid, "text": chunk}, timeout=30)
                resp.raise_for_status()
            except requests.RequestException:
                log.error("advice Telegram send failed", exc_info=True, extra={"chat_id": cid})
            if i < len(chunks) - 1:
                time.sleep(0.5)
    log.info("portfolio advice sent via Telegram", extra={"chats": len(chat_ids)})


def send_advice_email(advice: str, report_type: str) -> None:
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
    send_telegram(msg, report_type)
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


def validate_portfolio_quotes(raw: dict | None, snapshot: "Snapshot | None") -> tuple[bool, str]:
    if not portfolio_store.has_positions(raw):
        return False, "加密持股不存在或沒有持股，略過持股操作建議。"
    if snapshot is None:
        return False, "缺少 market_snapshot.json，無法驗證持股行情。"
    if snapshot.portfolio_quote_coverage is None:
        return False, "snapshot 未包含 portfolio_quote_coverage，無法確認持股行情覆蓋。"
    if snapshot.portfolio_quote_coverage < 1.0:
        return False, f"持股行情覆蓋率 {snapshot.portfolio_quote_coverage:.0%}，低於 100%。"
    bad: list[str] = []
    for ticker in _portfolio_tickers(raw):
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
    if not any(status in advice for status in ADVICE_STATUSES):
        return False, "private advice 未包含 PortfolioMonitoringStatus，可能仍是舊式任意交易建議。"

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
        "portfolio_quote_coverage": snapshot.portfolio_quote_coverage if snapshot else None,
        "data_quality": snapshot.data_quality if snapshot else {},
    }
    (data_dir / "portfolio_advice_audit.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def run_portfolio_advice(report: str, report_type: str,
                         snapshot: "Snapshot | None", model: str) -> None:
    """Generate position-aware advice and deliver privately. The advice text is
    never written to reports/ nor committed — the repo is public."""
    raw = portfolio_store.load_portfolio()
    ok, reason = validate_portfolio_quotes(raw, snapshot)
    if not ok:
        log.warning("portfolio advice blocked", extra={"reason": reason})
        write_advice_audit(report_type, "BLOCKED", reason, snapshot)
        if portfolio_store.ENC_PATH.exists() or portfolio_store.has_positions(raw):
            send_operational_notice(f"{reason}\n\n已停止產生持股建議，避免用錯誤或缺漏價格下判斷。", report_type)
        return
    try:
        portfolio = Portfolio.model_validate(raw)
    except ValidationError:
        log.error("portfolio data invalid — advice stage skipped", exc_info=True)
        return

    del portfolio, model
    context = load_market_context(report_type, snapshot)
    provider = EncryptedPortfolioProvider()
    portfolio_context = provider.load()
    brief = build_action_brief(context, portfolio_context)
    ok, reason = validate_action_brief(brief, context, portfolio_context)
    if not ok:
        log.warning("private advice validation failed", extra={"reason": reason})
        write_advice_audit(report_type, "BLOCKED", reason, snapshot)
        send_operational_notice(f"{reason}\n\n已停止傳送持股建議。", report_type)
        return
    write_advice_audit(report_type, "VALIDATED", reason, snapshot)
    data_dir = Path(__file__).parent.parent / "data"
    (data_dir / "portfolio_action_brief.json").write_text(brief.model_dump_json(indent=2), encoding="utf-8")
    advice = render_action_brief(brief)
    ok, reason = validate_private_advice_text(advice, raw, snapshot)
    if not ok:
        log.warning("rendered private advice validation failed", extra={"reason": reason})
        write_advice_audit(report_type, "BLOCKED", reason, snapshot)
        send_operational_notice(f"{reason}\n\n已停止傳送持股建議。", report_type)
        return
    send_advice_telegram(advice, report_type)
    send_advice_email(advice, report_type)


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="Institutional market report generator")
    parser.add_argument("report_type", choices=REPORT_TYPES, help="Report type to generate")
    parser.add_argument("--generate-only", action="store_true",
                        help="Generate and save report without delivery")
    parser.add_argument("--deliver-existing", action="store_true",
                        help="Deliver latest saved report after validation")
    args = parser.parse_args()

    if not os.getenv("GEMINI_API_KEY"):
        log.error("GEMINI_API_KEY is not set")
        sys.exit(1)

    model = (os.getenv("REPORT_MODEL") or "gemini-2.0-flash").strip()
    report_type: str = args.report_type

    if args.generate_only and args.deliver_existing:
        log.error("--generate-only and --deliver-existing are mutually exclusive")
        sys.exit(2)

    if args.deliver_existing:
        reports_dir = Path(__file__).parent.parent / "reports"
        files = sorted(reports_dir.glob(f"{report_type}_*.md"), reverse=True)
        if not files:
            log.error("no saved report to deliver", extra={"report_type": report_type})
            sys.exit(1)
        same_day_reports = reports_for_market_date(report_type)
        if len(same_day_reports) > 1:
            duplicate_path = same_day_reports[0]
            duplicate_path.unlink(missing_ok=True)
            log.info("same-day report already existed before this run — skipping duplicate delivery",
                     extra={"report_type": report_type, "removed_duplicate": duplicate_path.name,
                            "existing": same_day_reports[-1].name})
            return
        report = files[0].read_text(encoding="utf-8")
        snapshot = None
        snapshot_path = Path(__file__).parent.parent / "data" / "market_snapshot.json"
        try:
            snapshot = Snapshot.model_validate_json(snapshot_path.read_text(encoding="utf-8"))
        except Exception:
            log.warning("snapshot unavailable during delivery", exc_info=True)
        idempotency_key = f"{report_type}:{get_market_date(report_type).strftime('%Y%m%d')}"
        if delivery_state.already_delivered(idempotency_key):
            log.info("delivery already completed for idempotency key", extra={"key": idempotency_key})
            return
        delivery_state.mark_state(idempotency_key, "VALIDATING")
        try:
            context = load_market_context(report_type, snapshot)
            draft_path = Path(__file__).parent.parent / "data" / "market_report_draft.json"
            if draft_path.exists():
                from models import MarketReportDraft
                draft = MarketReportDraft.model_validate_json(draft_path.read_text(encoding="utf-8"))
                ok, reason = validate_public_draft(draft, context)
                if not ok:
                    delivery_state.mark_state(idempotency_key, "BLOCKED")
                    log.error("public draft validation failed before delivery", extra={"reason": reason})
                    sys.exit(1)
        except Exception:
            delivery_state.mark_state(idempotency_key, "BLOCKED")
            log.error("market context unavailable before delivery", exc_info=True)
            sys.exit(1)
        delivery_state.mark_state(idempotency_key, "VALIDATED")
        delivery_state.mark_state(idempotency_key, "DELIVERING")
        send_telegram(report, report_type)
        send_email(report, report_type)
        run_portfolio_advice(report, report_type, snapshot, model)
        delivery_state.mark_state(idempotency_key, "DELIVERED")
        log.info("delivered validated report", extra={"report_type": report_type, "path": str(files[0])})
        return

    # Prevent duplicate runs for the same market date
    if not args.generate_only and check_report_already_generated(report_type):
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
            "coverage": snapshot.fetch_coverage,
            "tw": len(snapshot.tw_stocks),
            "us": len(snapshot.us_markets),
            "fx": len(snapshot.forex)})
    except FileNotFoundError:
        log.warning("no snapshot found — blocking report generation",
                    extra={"report_type": report_type})
    except ValidationError:
        log.error("snapshot invalid — blocking report generation", exc_info=True)

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
    log.info("report saved", extra={"path": str(filepath)})

    if args.generate_only:
        log.info("generate-only mode: delivery deferred until validation passes")
        return

    send_telegram(report, report_type)
    send_email(report, report_type)

    # ── Private portfolio advice — Telegram/Email only, never committed ──────
    try:
        run_portfolio_advice(report, report_type, snapshot, model)
    except Exception:
        log.error("portfolio advice stage failed (report already delivered)",
                  exc_info=True)

    log.info("done", extra={"report_type": report_type})


if __name__ == "__main__":
    main()
