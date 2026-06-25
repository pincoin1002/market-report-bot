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
import time
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta

import requests
from google import genai
from google.genai import types

TPE = timezone(timedelta(hours=8))
STATE_FILE = Path(__file__).parent.parent / "bot_state.json"
SYSTEM_PROMPT = """你是一位專業的台股 / 美股市場分析師助理。
使用者剛收到一份市場日報（內容已附在下方），現在想針對報告內容追問問題。
請根據報告內容回答，必要時可用你的市場知識補充，但要明確標示哪些是報告內容、哪些是你的補充判斷。
回答請簡潔、具體、有操作性，不要廢話。
"""


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

def ask_gemini(question: str, report_context: str, model: str) -> str:
    api_key = os.environ["GEMINI_API_KEY"]
    client = genai.Client(api_key=api_key)
    prompt = f"{SYSTEM_PROMPT}\n\n=== 今日市場報告 ===\n{report_context}\n=== 報告結束 ===\n\n使用者問題：{question}"
    cfg = types.GenerateContentConfig(
        response_modalities=["TEXT"],
        temperature=0.4,
    )
    resp = client.models.generate_content(model=model, contents=prompt, config=cfg)
    return resp.text


# ── Report cache: look for today's report in reports/ dir ────────────────────

def get_latest_report() -> str | None:
    reports_dir = Path(__file__).parent.parent / "reports"
    if not reports_dir.exists():
        return None
    files = sorted(reports_dir.glob("*.md"), reverse=True)
    if not files:
        return None
    return files[0].read_text(encoding="utf-8")


# ── Main polling loop ────────────────────────────────────────────────────────

def poll(token: str, chat_id: str, model: str, duration: int) -> None:
    state = load_state()
    offset = state.get("offset", 0)
    deadline = time.time() + duration

    # Also store the latest report text in state for context
    latest_report = get_latest_report()
    if latest_report:
        state["latest_report"] = latest_report

    print(f"[bot] polling for {duration}s, offset={offset}")

    while time.time() < deadline:
        try:
            result = tg(token, "getUpdates", offset=offset, timeout=20, allowed_updates=["message"])
        except Exception as exc:
            print(f"[bot] getUpdates error: {exc}")
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
                print(f"[bot] ignored message from unauthorized chat: {from_chat}")
                continue

            question = text[5:].strip() if is_ask_cmd else text
            if not question:
                continue

            # Get report context: prefer the report from the replied-to message,
            # fall back to state["latest_report"]
            report_ctx = state.get("latest_report", "")
            if not report_ctx:
                report_ctx = "(報告內容不可用，請根據你的市場知識回答)"

            print(f"[bot] answering: {question[:60]}...")
            try:
                answer = ask_gemini(question, report_ctx, model)
                send_message(token, from_chat, answer, reply_to=msg.get("message_id"))
                print(f"[bot] replied to message {msg.get('message_id')}")
            except Exception as exc:
                print(f"[bot] error answering: {exc}")
                send_message(token, from_chat, f"⚠️ 處理問題時發生錯誤：{exc}", reply_to=msg.get("message_id"))

        state["offset"] = offset
        save_state(state)

        # Sleep briefly to avoid hammering the API
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        time.sleep(min(2, remaining))

    print(f"[bot] done, final offset={offset}")


# ── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram Q&A bot for market reports")
    parser.add_argument("--duration", type=int, default=270,
                        help="How many seconds to poll (default: 270 = 4.5 min)")
    args = parser.parse_args()

    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    model = os.getenv("REPORT_MODEL", "gemini-2.5-flash")

    if not token or not chat_id:
        print("[bot] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set — exiting")
        sys.exit(1)
    if not os.getenv("GEMINI_API_KEY"):
        print("[bot] GEMINI_API_KEY not set — exiting")
        sys.exit(1)

    poll(token, chat_id, model, args.duration)


if __name__ == "__main__":
    main()
