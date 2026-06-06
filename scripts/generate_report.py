#!/usr/bin/env python3
"""Institutional-grade market report generator.

Uses Google Gemini API with Google Search grounding to generate daily market
reports for Taiwan and US equity markets, then distributes via Telegram and Email.
"""

import os
import re
import sys
import json
import smtplib
import time
import argparse
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from google import genai
from google.genai import types
import requests

# ── Constants ──────────────────────────────────────────────────────────────────

REPORT_TYPES = ("tw_open", "us_close", "tw_close", "us_open")

REPORT_TITLES = {
    "tw_open":  "台股開盤戰報",
    "us_close": "美股收盤日報",
    "tw_close": "台股收盤日報",
    "us_open":  "美股開盤日報",
}

TPE = timezone(timedelta(hours=8))

# ── Portfolio ──────────────────────────────────────────────────────────────────

def load_portfolio() -> str:
    """Load portfolio.json and format as a context block for prompt injection."""
    portfolio_path = Path(__file__).parent.parent / "portfolio.json"
    if not portfolio_path.exists():
        return ""
    try:
        with open(portfolio_path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return ""

    lines = ["=== 使用者持倉與觀察名單 ==="]
    lines.append(f"最後更新：{data.get('last_updated', '未知')}")

    tw_pos = [p for p in data.get("tw_positions", []) if p.get("shares", 0) > 0]
    if tw_pos:
        lines.append("\n【台股持倉】")
        for p in tw_pos:
            pnl_hint = ""
            lines.append(f"  {p['ticker']} {p['name']}：{p['shares']} 股，成本 {p['cost_basis']} TWD/股{' — ' + p['note'] if p.get('note') else ''}")

    us_pos = [p for p in data.get("us_positions", []) if p.get("shares", 0) > 0]
    if us_pos:
        lines.append("\n【美股持倉】")
        for p in us_pos:
            lines.append(f"  {p['ticker']} {p['name']}：{p['shares']} 股，成本 {p['cost_basis']} USD/股{' — ' + p['note'] if p.get('note') else ''}")

    watchlist = data.get("watchlist", {})
    tw_watch = watchlist.get("tw", [])
    us_watch = watchlist.get("us", [])
    if tw_watch:
        lines.append(f"\n【台股觀察清單】：{', '.join(tw_watch)}")
    if us_watch:
        lines.append(f"【美股觀察清單】：{', '.join(us_watch)}")

    notes = data.get("portfolio_notes", "")
    if notes:
        lines.append(f"\n【備注】：{notes}")

    lines.append("=== 持倉資訊結束 ===")

    if len(lines) <= 3:
        return ""

    return "\n".join(lines)

# ── Core ───────────────────────────────────────────────────────────────────────

def load_prompt(report_type: str) -> str:
    now = datetime.now(TPE)
    weekdays = ("一", "二", "三", "四", "五", "六", "日")
    date_str = now.strftime("%Y-%m-%d")
    weekday_str = f"週{weekdays[now.weekday()]}"

    prompt_path = Path(__file__).parent.parent / "prompts" / f"{report_type}.md"
    with open(prompt_path, encoding="utf-8") as f:
        prompt = f.read()

    prompt = prompt.replace("{{TODAY_DATE}}", date_str)
    prompt = prompt.replace("{{TODAY_WEEKDAY}}", weekday_str)

    # Inject portfolio context
    portfolio_context = load_portfolio()
    if portfolio_context:
        prompt = prompt + "\n\n" + portfolio_context

    return prompt


def generate_report(prompt: str, model: str) -> str:
    api_key = os.environ["GEMINI_API_KEY"]
    client = genai.Client(api_key=api_key)

    google_search_tool = types.Tool(google_search=types.GoogleSearch())

    cfg = types.GenerateContentConfig(
        tools=[google_search_tool],
        response_modalities=["TEXT"],
        temperature=0.3,
    )

    from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
    from google.genai.errors import ServerError, ClientError

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=2, min=10, max=60),
        retry=retry_if_exception_type((ServerError,)),
        reraise=True,
    )
    def _call():
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=cfg,
        )
        return response.text

    return _call()


def save_report(report: str, report_type: str) -> Path:
    now = datetime.now(TPE)
    reports_dir = Path(__file__).parent.parent / "reports"
    reports_dir.mkdir(exist_ok=True)
    filename = f"{report_type}_{now.strftime('%Y%m%d_%H%M%S')}.md"
    path = reports_dir / filename
    with open(path, "w", encoding="utf-8") as f:
        f.write(report)
    return path


def _chunk_text(text: str, max_len: int = 4000) -> list[str]:
    if len(text) <= max_len:
        return [text]
    chunks, start = [], 0
    while start < len(text):
        end = start + max_len
        if end < len(text):
            cut = text.rfind("\n", start, end)
            if cut > start:
                end = cut + 1
        chunks.append(text[start:end])
        start = end
    return chunks


def send_telegram(report: str, report_type: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print("[Telegram] not configured — skipping")
        return

    title = REPORT_TITLES[report_type]
    full_text = report
    chunks = _chunk_text(full_text)
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    for i, chunk in enumerate(chunks, 1):
        payload = {"chat_id": chat_id, "text": chunk, "parse_mode": "Markdown"}
        try:
            resp = requests.post(url, json=payload, timeout=30)
            resp.raise_for_status()
            print(f"[Telegram] chunk {i}/{len(chunks)} sent")
        except requests.RequestException as exc:
            payload2 = {"chat_id": chat_id, "text": chunk}
            try:
                resp2 = requests.post(url, json=payload2, timeout=30)
                resp2.raise_for_status()
                print(f"[Telegram] chunk {i}/{len(chunks)} sent (plain)")
            except requests.RequestException as exc2:
                print(f"[Telegram] failed chunk {i}: {exc2}", file=sys.stderr)
        if i < len(chunks):
            time.sleep(1)


def _md_to_html(md: str) -> str:
    html = md.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    html = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html)
    html = re.sub(r"\*(.+?)\*", r"<em>\1</em>", html)
    html = html.replace("\n", "<br>")
    return f"<html><body><pre style='font-family:monospace'>{html}</pre></body></html>"


def send_email(report: str, report_type: str) -> None:
    smtp_server = os.getenv("EMAIL_SMTP_SERVER", "")
    smtp_port_str = os.getenv("EMAIL_SMTP_PORT", "587")
    username = os.getenv("EMAIL_USERNAME", "")
    password = os.getenv("EMAIL_PASSWORD", "")
    email_to = os.getenv("EMAIL_TO", "")

    if not smtp_server or not username or not email_to:
        print("[Email] not configured — skipping")
        return

    try:
        smtp_port = int(smtp_port_str)
    except ValueError:
        smtp_port = 587

    recipients = [e.strip() for e in email_to.split(",") if e.strip()]
    title = REPORT_TITLES[report_type]
    now = datetime.now(TPE)
    date_str = now.strftime("%Y-%m-%d")

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
        print(f"[Email] sent to {recipients}")
    except smtplib.SMTPException as exc:
        print(f"[Email] SMTP error: {exc}", file=sys.stderr)


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Institutional market report generator")
    parser.add_argument("report_type", choices=REPORT_TYPES, help="Report type to generate")
    args = parser.parse_args()

    if not os.getenv("GEMINI_API_KEY"):
        print("GEMINI_API_KEY is not set", file=sys.stderr)
        sys.exit(1)

    model = os.getenv("REPORT_MODEL", "gemini-2.5-flash")
    report_type: str = args.report_type

    print(f"[{report_type}] loading prompt …")
    prompt = load_prompt(report_type)

    print(f"[{report_type}] calling Gemini API  model={model} …")
    report = generate_report(prompt, model)

    filepath = save_report(report, report_type)
    print(f"[{report_type}] saved → {filepath}")

    send_telegram(report, report_type)
    send_email(report, report_type)

    print(f"[{report_type}] done")


if __name__ == "__main__":
    main()
