#!/usr/bin/env python3
"""Telegram Q&A bot for market reports.

Polls Telegram and answers every text message in the authorized chat,
using recent reports (via report_store) as context with Google Gemini.

Usage (called by GitHub Actions):
    python scripts/telegram_bot.py --duration 240
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
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from logging_config import setup_logging
from report_store import ReportStore

log = logging.getLogger("bot")

TPE = timezone(timedelta(hours=8))
STATE_FILE = Path(__file__).parent.parent / "bot_state.json"
SYSTEM_PROMPT = """你是一位專業的台股 / 美股市場分析師助理。
使用者可以針對日報與持股進行追問，也可以詢問日報或持股之外的任何全球股票、宏觀經濟或市場熱點。
如果用戶詢問了日報或持股之外的股票，請善用你的搜尋工具 (Google Search) 進行即時檢索，並給出最新、客觀的市場數據、新聞與分析。
若引用了歷史報告節錄，回答時標明日期（例如「根據 6/28 美股收盤報告…」）。

【Telegram 訊息美化與排版極嚴格規則】
1. 嚴禁輸出任何標題標籤（絕對不要使用 #, ##, ###, ####），也嚴禁輸出 "## **" 這種混亂格式。
2. 善用 Emoji 來增加資訊結構與視覺舒適度：
   - 📌 表示核心重點或結論。
   - 💡 表示操作建議或關鍵提醒。
   - 📈 / 📉 / 📊 表示市場數據、股價走勢或統計。
   - 🔍 表示分析或補充判斷。
3. 善用「粗體」作為段落標題或重點標示。
4. 每一小段落之間「務必空一行」，避免字堆擠在一起，提升視覺可讀性。
5. 字數控制在 350 字以內，語氣專業、清晰、好讀。

【排版範例參考】
📌 *今日記憶體族群下跌主因*
美光 (MU) 盤後財報展望不如預期，拖累整體半導體板塊走弱...

📈 *相關個股觀察*
* 台積電 (2330)：今日回檔修正 %...
* 南亞科 (2408)：面臨均線壓力...

💡 *後續操作建議*
短線上不建議急於抄底，宜靜待融資餘額沉澱...
"""

def _clean_markdown_for_tg(text: str) -> str:
    """Clean up markdown text to make it clean, beautiful, and stable in Telegram."""
    # 1. Scrub header tags wrapped with bold stars (e.g. ## **Title** -> *Title*)
    text = re.sub(r'^\s*#+\s*\**([^*]+)\**\s*$', r'*\1*', text, flags=re.MULTILINE)
    # 2. Convert standard markdown headers to simple bold: # Title -> *Title*
    text = re.sub(r'^\s*#+\s*(.*)$', r'*\1*', text, flags=re.MULTILINE)
    # 3. Explicitly remove residual raw Markdown headers
    text = text.replace("## **", "*").replace("### **", "*").replace("#### **", "*")
    text = text.replace("** ##", "*").replace("** ###", "*")
    # 4. Convert all *** or ** to a single asterisk * for simple Telegram bold
    text = text.replace("***", "*").replace("**", "*")
    # 5. Collapse multiple consecutive newlines (3 or more) to exactly two
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


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


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=10),
    retry=retry_if_exception(lambda e: isinstance(e, Exception)),
)
def _call_gemini_api(question: str, report_context: str, portfolio_context: str, model: str, use_search: bool) -> str:
    api_key = os.environ["GEMINI_API_KEY"]
    client = genai.Client(api_key=api_key)
    prompt = f"{SYSTEM_PROMPT}\n{portfolio_context}\n=== 今日市場報告 ===\n{report_context}\n=== 報告結束 ===\n\n使用者問題：{question}"
    
    # Disable safety filters for stock vocabulary
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

    cfg = types.GenerateContentConfig(
        response_modalities=["TEXT"],
        temperature=0.4,
        safety_settings=safety_settings,
        tools=tools,
    )
    resp = client.models.generate_content(model=model, contents=prompt, config=cfg)
    if not resp or not resp.text:
        raise ValueError("Gemini API returned empty response text")
    return resp.text


def ask_gemini(question: str, report_context: str, portfolio_context: str, model: str) -> str:
    try:
        return _call_gemini_api(question, report_context, portfolio_context, model, use_search=True)
    except Exception as exc:
        log.warning("Ask Gemini with search failed. Retrying without search...", extra={"error": str(exc)})
        # Fallback to local context only if Google Search Grounding encounters an error
        return _call_gemini_api(question, report_context, portfolio_context, model, use_search=False)


# ── Report context: latest report + FTS-matched history from report_store ────

def build_context(store: ReportStore, question: str) -> str:
    parts = []
    if (latest := store.latest()):
        mdate = latest[0]
        parts.append(f"【今日最新報告 {mdate[:4]}-{mdate[4:6]}-{mdate[6:]}】\n{latest[1]}")
    parts += store.search(question, limit=2)  # multi-day reasoning context
    return "\n\n".join(parts) or "(報告內容不可用，請根據你的市場知識回答)"


# ── Message filtering ────────────────────────────────────────────────────────

def _extract_question(text: str) -> str | None:
    """Return the question in a message, or None if it should be ignored.

    Every plain text message in the authorized chat is a question — users
    naturally type questions directly instead of replying or using /ask.
    Commands other than /ask (e.g. /start) are ignored; /ask and /ask@botname
    both work.
    """
    text = text.strip()
    if not text:
        return None
    if text.startswith("/"):
        cmd, _, rest = text.partition(" ")
        cmd = cmd.split("@", 1)[0].lower()   # "/ask@MyBot" → "/ask"
        if cmd == "/ask":
            return rest.strip() or None
        return None
    return text


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
            result = tg(token, "getUpdates", offset=offset, timeout=20,
                        allowed_updates=["message", "channel_post"])
        except Exception:
            log.warning("getUpdates error", exc_info=True)
            time.sleep(5)
            continue

        for upd in result.get("result", []):
            # Advance + persist the offset FIRST: a message that crashes the
            # handler must never be re-fetched forever (poison-message loop).
            offset = upd["update_id"] + 1
            state["offset"] = offset
            save_state(state)

            try:
                msg = upd.get("message") or upd.get("channel_post") or {}
                text = msg.get("text", "")
                from_chat = str(msg.get("chat", {}).get("id", ""))

                if from_chat != str(chat_id):
                    if from_chat:
                        log.warning("ignored unauthorized message", extra={"chat": from_chat})
                    continue

                question = _extract_question(text)
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
                    send_message(token, from_chat,
                                 f"⚠️ 處理問題時發生錯誤：{exc}", reply_to=msg.get("message_id"))
            except Exception:
                # Never let one bad update kill the polling loop
                log.error("update handling failed — skipped", exc_info=True,
                          extra={"update_id": upd.get("update_id")})

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
