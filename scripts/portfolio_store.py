#!/usr/bin/env python3
"""Encrypted portfolio storage.

The portfolio lives in the repo as Fernet ciphertext (portfolio.json.enc) so it
can be versioned and updated by CI, while the plaintext never touches the
public repo. The key lives ONLY in the PORTFOLIO_KEY GitHub Secret (and the
user's local .env for development).

Precedence on load:
  1. portfolio.json  (plaintext, gitignored — local development override)
  2. portfolio.json.enc + PORTFOLIO_KEY env var

CLI (local, one-time):
  python scripts/portfolio_store.py genkey          # print a new Fernet key
  python scripts/portfolio_store.py init            # encrypt portfolio.json → portfolio.json.enc
  python scripts/portfolio_store.py show            # decrypt and pretty-print
"""

import json
import logging
import os
import sys
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger("portfolio_store")

BASE_DIR = Path(__file__).parent.parent
PLAIN_PATH = BASE_DIR / "portfolio.json"
ENC_PATH = BASE_DIR / "portfolio.json.enc"

EMPTY: dict = {
    "tw_positions": [],
    "us_positions": [],
    "available_cash": None,
    "portfolio_notes": "",
}


def _key() -> bytes | None:
    key = os.getenv("PORTFOLIO_KEY", "").strip()
    return key.encode() if key else None


def load_portfolio() -> dict | None:
    """Return the portfolio dict, or None if not configured."""
    if PLAIN_PATH.exists():
        try:
            return json.loads(PLAIN_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log.error("plaintext portfolio.json unreadable", exc_info=True)
            return None
    key = _key()
    if not (key and ENC_PATH.exists()):
        return None
    try:
        raw = Fernet(key).decrypt(ENC_PATH.read_bytes())
        return json.loads(raw.decode("utf-8"))
    except (InvalidToken, ValueError, OSError):
        log.error("failed to decrypt portfolio.json.enc (wrong PORTFOLIO_KEY?)",
                  exc_info=True)
        return None


def save_portfolio(portfolio: dict) -> bool:
    """Encrypt and write portfolio.json.enc. Returns False if no key present."""
    key = _key()
    if not key:
        log.error("PORTFOLIO_KEY not set — cannot save portfolio")
        return False
    data = json.dumps(portfolio, ensure_ascii=False, indent=2).encode("utf-8")
    ENC_PATH.write_bytes(Fernet(key).encrypt(data))
    return True


def has_positions(portfolio: dict | None) -> bool:
    return bool(portfolio and (portfolio.get("tw_positions")
                               or portfolio.get("us_positions")))


def format_portfolio(portfolio: dict) -> str:
    """Human-readable summary for Telegram."""
    lines = ["💼 *目前持股部位*", ""]
    tw = portfolio.get("tw_positions", [])
    if tw:
        lines.append("*台股*")
        for p in tw:
            lines.append(f"• {p['name']}({p['ticker']})：{p['shares']:g} 股，均價 {p['cost_basis']:g}")
        lines.append("")
    us = portfolio.get("us_positions", [])
    if us:
        lines.append("*美股*")
        for p in us:
            lines.append(f"• {p['name']}({p['ticker']})：{p['shares']:g} 股，均價 {p['cost_basis']:g}")
        lines.append("")
    if not (tw or us):
        lines.append("（尚無持股 — 用 /buy 建立，例如 /buy 2330 1000 1050 台積電）")
        lines.append("")
    cash = portfolio.get("available_cash")
    lines.append(f"可用資金：{cash if cash is not None else '未設定（用 /cash 設定）'}")
    return "\n".join(lines)


# ── Position mutation (used by the Telegram bot) ─────────────────────────────

def _bucket(portfolio: dict, ticker: str) -> str:
    """TW tickers are numeric (2330); US tickers are alphabetic (NVDA)."""
    return "tw_positions" if ticker.isdigit() else "us_positions"


def apply_buy(portfolio: dict, ticker: str, shares: float, price: float,
              name: str | None = None) -> str:
    bucket = _bucket(portfolio, ticker)
    positions = portfolio.setdefault(bucket, [])
    for p in positions:
        if p["ticker"] == ticker:
            total_cost = p["shares"] * p["cost_basis"] + shares * price
            p["shares"] += shares
            p["cost_basis"] = round(total_cost / p["shares"], 4)
            return (f"✅ 已加碼 {p['name']}({ticker}) {shares:g} 股 @ {price:g}\n"
                    f"現持 {p['shares']:g} 股，新均價 {p['cost_basis']:g}")
    positions.append({"ticker": ticker, "name": name or ticker,
                      "shares": shares, "cost_basis": price, "note": ""})
    return f"✅ 新增持股 {name or ticker}({ticker})：{shares:g} 股 @ {price:g}"


def apply_sell(portfolio: dict, ticker: str, shares: float) -> str:
    bucket = _bucket(portfolio, ticker)
    for p in portfolio.get(bucket, []):
        if p["ticker"] == ticker:
            if shares >= p["shares"]:
                portfolio[bucket].remove(p)
                return f"✅ 已全部出清 {p['name']}({ticker})（原持 {p['shares']:g} 股）"
            p["shares"] -= shares
            return (f"✅ 已賣出 {p['name']}({ticker}) {shares:g} 股，"
                    f"剩餘 {p['shares']:g} 股（均價不變 {p['cost_basis']:g}）")
    return f"⚠️ 找不到持股 {ticker}，目前部位可用 /portfolio 查看"


def apply_cash(portfolio: dict, amount: float) -> str:
    portfolio["available_cash"] = amount
    return f"✅ 可用資金已更新為 {amount:g}"


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "genkey":
        print(Fernet.generate_key().decode())
    elif cmd == "init":
        if not PLAIN_PATH.exists():
            sys.exit("portfolio.json not found — create it first (it stays gitignored)")
        portfolio = json.loads(PLAIN_PATH.read_text(encoding="utf-8"))
        if not save_portfolio(portfolio):
            sys.exit("PORTFOLIO_KEY env var not set")
        print(f"encrypted → {ENC_PATH}")
    elif cmd == "show":
        p = load_portfolio()
        print(json.dumps(p, ensure_ascii=False, indent=2) if p else "(no portfolio)")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
