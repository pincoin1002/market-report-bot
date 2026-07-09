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
from models import Portfolio, Snapshot

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
    now = datetime.now(tz=TPE)
    if report_type == "us_close":
        return now - timedelta(days=1)
    elif report_type == "us_open":
        # If us_open runs past midnight TPE (00:00-06:00 next day) due to delays,
        # its market date belongs to the previous day.
        if now.hour < 6:
            return now - timedelta(days=1)
    return now

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
        # If search failed (e.g. search quota or network issues), retry without search grounding
        return _call_gemini_api(prompt, model, report_type, use_search=False)


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

    lines += [
        "**重要：上述快照數字即為最終答案，撰寫報告時直接使用，禁止搜尋或修改。**",
        "---",
        "",
    ]
    return "\n".join(lines)



def _build_portfolio_block(portfolio: Portfolio) -> str:
    """Format the portfolio as a prompt preamble."""
    lines = [
        "## ⚠️ 您的目前持股與投資部位 — 供今日交易計畫決策參考",
        "請務必針對以下持股進行具體的走勢評估，並給出加減碼或出場的操作建議：",
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
        lines.append("**可用資金 (Available Cash)**: ⚠️ 用戶未設定可用資金。請在日報的交易計畫中，主動詢問用戶目前可操作的金額，例如提示：'如果您能提供目前的可操作金額，我將能為您估算更精確的加減碼股數與部位配比。'")
    lines.append("")

    if portfolio.portfolio_notes:
        lines.append(f"**持股說明**: {portfolio.portfolio_notes}")
        lines.append("")

    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def check_report_already_generated(report_type: str) -> bool:
    """Return True if a report of report_type for the same market date already exists."""
    reports_dir = Path(__file__).parent.parent / "reports"
    if not reports_dir.exists():
        return False
    mdate = get_market_date(report_type)
    date_str = mdate.strftime("%Y%m%d")
    files = list(reports_dir.glob(f"{report_type}_{date_str}_*.md"))
    if not files:
        files = list(reports_dir.glob(f"{report_type}_{date_str}*.md"))
    if files:
        log.info("report already exists for market date — skipping duplicate delivery",
                 extra={"report_type": report_type, "market_date": date_str,
                        "existing": files[0].name})
        return True
    return False


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
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        log.info("Telegram not configured — skipping")
        return

    title = REPORT_TITLES[report_type]
    now_str = datetime.now(tz=TPE).strftime("%Y-%m-%d %H:%M TPE")
    header = f"📊 *{title}* ｜ {now_str}\n{'─' * 30}\n\n"
    
    cleaned_report = _clean_markdown_for_telegram_report(report)
    chunks = _split_message(header + cleaned_report)

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for i, chunk in enumerate(chunks):
        try:
            resp = requests.post(url, json={"chat_id": chat_id, "text": chunk, "parse_mode": "Markdown"}, timeout=30)
            resp.raise_for_status()
            log.info("Telegram chunk sent", extra={"chunk": i + 1, "total": len(chunks)})
        except requests.RequestException:
            log.error("Telegram send failed", exc_info=True,
                      extra={"chunk": i + 1, "total": len(chunks)})
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

# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="Institutional market report generator")
    parser.add_argument("report_type", choices=REPORT_TYPES, help="Report type to generate")
    args = parser.parse_args()

    if not os.getenv("GEMINI_API_KEY"):
        log.error("GEMINI_API_KEY is not set")
        sys.exit(1)

    model = (os.getenv("REPORT_MODEL") or "gemini-2.0-flash").strip()
    report_type: str = args.report_type

    # Prevent duplicate runs for the same market date
    if check_report_already_generated(report_type):
        sys.exit(0)

    prompt = load_prompt(report_type)

    portfolio_path = Path(__file__).parent.parent / "portfolio.json"
    if portfolio_path.exists():
        try:
            portfolio = Portfolio.model_validate_json(
                portfolio_path.read_text(encoding="utf-8"))
            prompt = _build_portfolio_block(portfolio) + prompt
            log.info("portfolio injected", extra={"report_type": report_type})
        except ValidationError:
            log.error("portfolio.json invalid — skipping portfolio injection",
                      exc_info=True)

    snapshot_path = Path(__file__).parent.parent / "data" / "market_snapshot.json"
    try:
        snapshot = Snapshot.model_validate_json(
            snapshot_path.read_text(encoding="utf-8"))
        prompt = _build_snapshot_block(snapshot) + prompt
        log.info("snapshot injected", extra={
            "report_type": report_type,
            "coverage": snapshot.fetch_coverage,
            "tw": len(snapshot.tw_stocks),
            "us": len(snapshot.us_markets),
            "fx": len(snapshot.forex)})
    except FileNotFoundError:
        log.warning("no snapshot found — pure search mode",
                    extra={"report_type": report_type})
    except ValidationError:
        log.error("snapshot invalid — pure search mode", exc_info=True)

    max_tokens = MAX_OUTPUT_TOKENS.get(report_type, 16000)
    log.info("calling Gemini API", extra={
        "report_type": report_type, "model": model, "max_tokens": max_tokens})
    report = generate_report(prompt, model, report_type)

    filepath = save_report(report, report_type)
    log.info("report saved", extra={"path": str(filepath)})

    send_telegram(report, report_type)
    send_email(report, report_type)

    log.info("done", extra={"report_type": report_type})


if __name__ == "__main__":
    main()
