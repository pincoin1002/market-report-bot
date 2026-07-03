#!/usr/bin/env python3
"""Telegram Q&A bot for market reports.

Polls Telegram for replies to report messages and answers follow-up questions
using the original report as context with Google Gemini.

Usage (called by GitHub Actions every 5 minutes):
    python scripts/telegram_bot.py --duration 270
"""

import os
import sys
import json
import logging
import time
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta

import re
import requests
from google import genai
from google.genai import types

from logging_config import setup_logging
from report_store import ReportStore

log = logging.getLogger("bot")

TPE = timezone(timedelta(hours=8))
STATE_FILE = Path(__file__).parent.parent / "bot_state.json"
SYSTEM_PROMPT = """你是一位專業的台股 / 美股市場分析師助理。
使用者剛收到一份市場日報，現在想針對報告追問問題。
請根據報告內容回答，必要時可用市場知識補充。
若引用了歷史報告節錄，回答時標明日期（例如「根據 6/28 美股收盤報告…」）。

【Telegram 格式排版規則】
1. 禁止輸出任何 Markdown 標題標籤（絕對不要使用 #, ##, ###, ####）。
2. 如果要表示標題或強調重點，請直接用粗體並換行，例如：*今日操作建議*。
3. 嚴禁使用三個星號（***）或深層嵌套符號，保持字句乾淨。
4. 使用繁體中文回答，段落之間適度留空行以提升可讀性。
5. 回答請簡潔、具體、有操作性，字數控制在 350 字內。
"""

def _clean_markdown_for_tg(text: str) -> str:
    """Clean up markdown text to make it clean and readable in Telegram."""
    # 1. Convert ### Heading to bold: *Heading*
    text = re.sub(r'^\s*#+\s*(.*)$', r'*\1*', text, flags=re.MULTILINE)
    # 2. Convert *** or ** to a single asterisk * for simple Telegram bold
    text = text.replace("***", "*").replace("**", "*")
    return text


# ── State (last seen message_id + cached reports) ────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"offset": 0, "reports": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ── Telegram helpers ─────────────────────────────────────────────────────────

def tg(token: str, method: str, **kwargs) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    resp = requests.post(url, json=kwargs, timeout=30)
    resp.raise_for_status()
    return resp.json()


def send_message(token: str, chat_id: str, text: str, reply_to: int | None = None) -> dict:
    kwargs: dict = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_to:
        kwargs["reply_to_message_id"] = reply_to
    try:
        return tg(token, "sendMessage", **kwargs)
    except Exception:
        kwargs.pop("parse_mode", None)
        return tg(token, "sendMessage", **kwargs)


# ── Gemini Q&A ───────────────────────────────────────────────────────────────

def _load_portfolio_context() -> str:
    portfolio_path = Path(__file__).parent.parent / "portfolio.json"
    if portfolio_path.exists():
        try:
            portfolio = json.loads(portfolio_path.read_text(encoding="utf-8"))
            lines = ["\n=== 使用者目前持股與部位數據 ==="]
            tw = portfolio.get("tw_positions", [])
            if tw:
                lines.append("台股持股:")
                for p in tw:
                    lines.append(f"- {p['name']}({p['ticker']}): {p['shares']}股, 均價{p['cost_basis']} TWD")
            us = portfolio.get("us_positions", [])
            if us:
                lines.append("美股持股:")
                for p in us:
                    lines.append(f"- {p['name']}({p['ticker']}): {p['shares']}股, 均價{p['cost_basis']} USD")
            
            cash = portfolio.get("available_cash")
            if cash:
                lines.append(f"可用資金 (Available Cash): {cash}")
            else:
                lines.append("可用資金 (Available Cash): ⚠️ 未設定。請提示用戶，若提供可用資金，您可以計算精確的倉位加減碼數量。")
            lines.append("=== 持股資訊結束 ===\n")
            return "\n".join(lines)
        except Exception:
            pass
    return ""


def ask_gemini(question: str, report_context: str, portfolio_context: str, model: str) -> str:
    api_key = os.environ["GEMINI_API_KEY"]
    client = genai.Client(api_key=api_key)
    prompt = f"{SYSTEM_PROMPT}\n{portfolio_context}\n=== 今日市場報告 ===\n{report_context}\n=== 報告結束 ===\n\n使用者問題：{question}"
    cfg = types.GenerateContentConfig(
        response_modalities=["TEXT"],
        temperature=0.4,
    )
    resp = client.models.generate_content(model=model, contents=prompt, config=cfg)
    return resp.text


# ── Report context: latest report + FTS-matched history from report_store ────

def build_context(store: ReportStore, question: str) -> str:
    parts = []
    if (latest := store.latest()):
        mdate = latest[0]
        parts.append(f"【今日最新報告 {mdate[:4]}-{mdate[4:6]}-{mdate[6:]}】\n{latest[1]}")
    parts += store.search(question, limit=2)  # multi-day reasoning context
    return "\n\n".join(parts) or "(報告內容不可用，請根據你的市場知識回答)"


# ── Main polling loop ────────────────────────────────────────────────────────

def poll(token: str, chat_id: str, model: str, duration: int) -> None:
    state = load_state()
    state.pop("latest_report", None)  # legacy field — context now in reports.db
    offset = state.get("offset", 0)
    deadline = time.time() + duration

    base_dir = Path(__file__).parent.parent
    store = ReportStore(base_dir / "data" / "reports.db")
    store.ingest_dir(base_dir / "reports")   # pick up reports committed since last run
    store.prune(keep_days=30)

    log.info("polling started", extra={"duration": duration, "offset": offset})

    while time.time() < deadline:
        try:
            result = tg(token, "getUpdates", offset=offset, timeout=20, allowed_updates=["message"])
        except Exception:
            log.warning("getUpdates error", exc_info=True)
            time.sleep(5)
            continue

        updates = result.get("result", [])
        for upd in updates:
            offset = upd["update_id"] + 1
            msg = upd.get("message", {})
            text = msg.get("text", "").strip()
            from_chat = str(msg.get("chat", {}).get("id", ""))
            reply_to_msg = msg.get("reply_to_message", {})

            # Only respond to:
            # 1. Direct /ask command: /ask <question>
            # 2. Reply to any message in the channel/chat
            is_reply = bool(reply_to_msg)
            is_ask_cmd = text.lower().startswith("/ask ")

            if not text:
                continue
            if not (is_reply or is_ask_cmd):
                continue
            if from_chat != str(chat_id):
                # Only respond to configured chat
                log.warning("ignored unauthorized message", extra={"chat": from_chat})
                continue

            question = text[5:].strip() if is_ask_cmd else text
            if not question:
                continue

            report_ctx = build_context(store, question)
            portfolio_ctx = _load_portfolio_context()

            log.info("answering question", extra={"question": question[:60]})
            try:
                answer = ask_gemini(question, report_ctx, portfolio_ctx, model)
                cleaned_answer = _clean_markdown_for_tg(answer)
                send_message(token, from_chat, cleaned_answer, reply_to=msg.get("message_id"))
                log.info("replied", extra={"message_id": msg.get("message_id")})
            except Exception as exc:
                log.error("error answering", exc_info=True)
                send_message(token, from_chat, f"⚠️ 處理問題時發生錯誤：{exc}", reply_to=msg.get("message_id"))

        state["offset"] = offset
        save_state(state)

        # Sleep briefly to avoid hammering the API
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        time.sleep(min(2, remaining))

    store.close()
    log.info("polling done", extra={"final_offset": offset})


# ── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="Telegram Q&A bot for market reports")
    parser.add_argument("--duration", type=int, default=270,
                        help="How many seconds to poll (default: 270 = 4.5 min)")
    args = parser.parse_args()

    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    model = os.getenv("REPORT_MODEL", "gemini-2.0-flash")

    if not token or not chat_id:
        log.error("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set — exiting")
        sys.exit(1)
    if not os.getenv("GEMINI_API_KEY"):
        log.error("GEMINI_API_KEY not set — exiting")
        sys.exit(1)

    poll(token, chat_id, model, args.duration)


if __name__ == "__main__":
    main()
