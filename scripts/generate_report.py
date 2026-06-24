#!/usr/bin/env python3
"""Institutional-grade market report generator.

Uses Google Gemini API with Google Search grounding to generate daily market
reports for Taiwan and US equity markets, then distributes via Telegram and Email.
"""

import json
import os
import re
import sys
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
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

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

# ── Core ───────────────────────────────────────────────────────────────────────

def load_prompt(report_type: str) -> str:
    prompt_path = Path(__file__).parent.parent / "prompts" / f"{report_type}.md"
    if not prompt_path.exists():
        raise FileNotFoundError(f"Prompt not found: {prompt_path}")
    text = prompt_path.read_text(encoding="utf-8")
    today = datetime.now(tz=TPE).strftime("%Y-%m-%d")
    weekday_map = {0: "週一", 1: "週二", 2: "週三", 3: "週四", 4: "週五", 5: "週六", 6: "週日"}
    weekday = weekday_map[datetime.now(tz=TPE).weekday()]
    return text.replace("{{TODAY_DATE}}", today).replace("{{TODAY_WEEKDAY}}", weekday)


@retry(
        reraise=True,
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=2, min=4, max=60),
        retry=retry_if_exception(lambda e: isinstance(e, Exception) and "503" in str(e)),
)
def generate_report(prompt: str, model: str, report_type: str) -> str:
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    max_tokens = MAX_OUTPUT_TOKENS.get(report_type, 16000)
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            temperature=0.1,
            max_output_tokens=max_tokens,
        ),
    )
    return response.text


def _build_snapshot_block(snapshot: dict) -> str:
    """Format a market snapshot dict as a strongly-worded preamble for the prompt."""
    fetched_at = snapshot.get("generated_at", "")[:19].replace("T", " ")
    lines = [
        "## ⚠️ 系統提供的市場快照 — 禁止修改，禁止估算，禁止重新搜尋價格",
        f"（抓取時間：{fetched_at} TPE | 來源：yfinance）",
        "",
        "以下所有數字為已驗證的最新收盤價。在報告中引用時必須完全一致，不得四捨五入或改動。",
        "若 Google Search 返回衝突數字，以本快照為準。",
        "搜尋工具應用於新聞、分析、上下文，而非已提供的價格。",
        "",
    ]

    tw = snapshot.get("tw_stocks", {})
    if tw:
        lines += ["**台股**",
                  "| 代號 | 名稱 | 收盤 (TWD) | 漲跌% |",
                  "|------|------|-----------|-------|"]
        for code, d in tw.items():
            lines.append(f"| {code} | {d['name']} | {d['price']:,.1f} | {d['change_pct']:+.2f}% |")
        lines.append("")

    us = snapshot.get("us_markets", {})
    if us:
        lines += ["**美股 / 指數 / 宏觀**",
                  "| Symbol | 名稱 | 最新 | 漲跌% |",
                  "|--------|------|------|-------|"]
        for key, d in us.items():
            val = f"{d['price']:.2f}%" if d.get("currency") == "percent" else f"{d['price']:,.2f}"
            lines.append(f"| {key} | {d['name']} | {val} | {d['change_pct']:+.2f}% |")
        lines.append("")

    forex = snapshot.get("forex", {})
    if forex:
        lines.append("**外匯**")
        for key, d in forex.items():
            lines.append(f"- {d['name']}: {d['price']:.4f} ({d['change_pct']:+.2f}%)")
        lines.append("")

    lines += [
        "**重要：上述快照數字即為最終答案，撰寫報告時直接使用，禁止搜尋或修改。**",
        "---",
        "",
    ]
    return "\n".join(lines)


def save_report(report: str, report_type: str) -> Path:
    reports_dir = Path(__file__).parent.parent / "reports"
    reports_dir.mkdir(exist_ok=True)
    now = datetime.now(tz=TPE)
    filepath = reports_dir / f"{report_type}_{now.strftime('%Y%m%d_%H%M%S')}.md"
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


def send_telegram(report: str, report_type: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("[Telegram] not configured — skipping")
        return

    title = REPORT_TITLES[report_type]
    now_str = datetime.now(tz=TPE).strftime("%Y-%m-%d %H:%M TPE")
    header = f"📊 {title}｜{now_str}\n{'─' * 38}\n\n"
    chunks = _split_message(header + report)

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for i, chunk in enumerate(chunks):
        try:
            resp = requests.post(url, json={"chat_id": chat_id, "text": chunk}, timeout=30)
            resp.raise_for_status()
            print(f"[Telegram] chunk {i+1}/{len(chunks)} sent")
        except requests.RequestException as exc:
            print(f"[Telegram] error on chunk {i+1}: {exc}", file=sys.stderr)
        if i < len(chunks) - 1:
            time.sleep(0.5)

# ── Email ──────────────────────────────────────────────────────────────────────

def _md_to_html(md: str) -> str:
    """Minimal Markdown to HTML conversion (no external dependencies)."""
    h = md

    # Escape HTML special chars in code blocks first, then restore tags
    def escape_code(m: re.Match) -> str:
        inner = m.group(1).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return f"<pre><code>{inner}</code></pre>"

    h = re.sub(r"```[^\n]*\n(.*?)```", escape_code, h, flags=re.DOTALL)
    h = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", h)

    # Headings
    for n in range(6, 0, -1):
        h = re.sub(r"^#{" + str(n) + r"} (.+)$", rf"<h{n}>\1</h{n}>", h, flags=re.MULTILINE)

    # Bold / italic
    h = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", h)
    h = re.sub(r"\*(.+?)\*", r"<em>\1</em>", h)

    # Horizontal rule
    h = re.sub(r"^-{3,}$", "<hr>", h, flags=re.MULTILINE)

    # Markdown tables
    def table_replace(m: re.Match) -> str:
        lines = [l for l in m.group(0).strip().split("\n") if l.strip() and "|" in l]
        rows_html: list[str] = []
        is_header = True
        for i, line in enumerate(lines):
            cells = [c.strip() for c in line.strip("|").split("|")]
            # Detect separator row (e.g. |---|---|)
            if i == 1 and all(re.match(r"^[-: ]+$", c) for c in cells if c):
                continue
            tag = "th" if is_header else "td"
            rows_html.append(
                "<tr>" + "".join(f"<{tag}>{c}</{tag}>" for c in cells) + "</tr>"
            )
            is_header = False
        return "<table>" + "".join(rows_html) + "</table>"

    h = re.sub(r"(\|.+\|\n?)+", table_replace, h)

    # Line breaks and paragraphs
    h = re.sub(r"\n\n+", "</p>\n<p>", h)
    h = h.replace("\n", "<br>\n")

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
p{{margin:.4em 0}}
strong{{color:#1a1a2e}}
</style>
</head>
<body>
<p>{h}</p>
</body>
</html>"""


def send_email(report: str, report_type: str) -> None:
    smtp_server = os.getenv("EMAIL_SMTP_SERVER", "").strip()
    if not smtp_server:
        print("[Email] not configured — skipping")
        return

    smtp_port = int(os.getenv("EMAIL_SMTP_PORT", "587") or "587")
    username = os.getenv("EMAIL_USERNAME", "").strip()
    password = os.getenv("EMAIL_PASSWORD", "").strip()
    email_to_raw = os.getenv("EMAIL_TO", "").strip()

    if not (username and password and email_to_raw):
        print("[Email] incomplete credentials — skipping")
        return

    recipients = [e.strip() for e in email_to_raw.split(",") if e.strip()]
    title = REPORT_TITLES[report_type]
    date_str = datetime.now(tz=TPE).strftime("%Y-%m-%d")

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
        print("Error: GEMINI_API_KEY is not set", file=sys.stderr)
        sys.exit(1)

    model = (os.getenv("REPORT_MODEL") or "gemini-2.0-flash").strip()
    report_type: str = args.report_type

    print(f"[{report_type}] loading prompt …")
    prompt = load_prompt(report_type)

    snapshot_path = Path(__file__).parent.parent / "data" / "market_snapshot.json"
    if snapshot_path.exists():
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        prompt = _build_snapshot_block(snapshot) + prompt
        print(f"[{report_type}] snapshot injected "
              f"(TW:{len(snapshot.get('tw_stocks', {}))} "
              f"US:{len(snapshot.get('us_markets', {}))} "
              f"FX:{len(snapshot.get('forex', {}))})")
    else:
        print(f"[{report_type}] no snapshot found — pure search mode")

    max_tokens = MAX_OUTPUT_TOKENS.get(report_type, 16000)
    print(f"[{report_type}] calling Gemini API  model={model}  max_tokens={max_tokens} …")
    report = generate_report(prompt, model, report_type)

    filepath = save_report(report, report_type)
    print(f"[{report_type}] saved → {filepath}")

    send_telegram(report, report_type)
    send_email(report, report_type)

    print(f"[{report_type}] done")


if __name__ == "__main__":
    main()
