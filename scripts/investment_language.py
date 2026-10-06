"""Traditional-Chinese display text; raw evidence remains in private audit JSON."""
import re
from decimal import Decimal
from opencc import OpenCC
from urllib.parse import urlparse

from event_contract import event_data

_TRADITIONAL = OpenCC("s2twp")


def chinese_text(value: str | None, fallback: str) -> str:
    text = str(value or "").strip()
    if (not re.search(r"[\u4e00-\u9fff]", text) or re.search(r"\b[A-Za-z]+(?:\s+[A-Za-z]+){2,}\b", text)
            or any(token in text for token in ("EVENT_CHECK_FAILED", "EVENT_UNCHECKED", "ACTION_REVIEW", "DATA_BLOCKED", "ROLLING_24H", "PREVIOUS_CLOSE", "FULL", "WATCH"))):
        return fallback
    return _TRADITIONAL.convert(text)


def format_price(value: float) -> str:
    number = Decimal(str(value))
    return format(number, ",f").rstrip("0").rstrip(".") if "." in format(number, "f") else format(number, ",f")


def interval_label(interval: str) -> str:
    return {"PREVIOUS_CLOSE": "單日", "ROLLING_24H": "24 小時",
            "SESSION_TO_SESSION": "相鄰交易時段", "UNKNOWN": "報價比較期間"}.get(interval, "報價比較期間")


def asset_next_step(asset_type: str) -> str:
    if asset_type in {"CRYPTO", "TOKEN", "STABLECOIN"}:
        subjects = "協議升級、安全事件、代幣經濟、交易所上下架、監管或流動性變化"
    elif asset_type == "ETF":
        subjects = "指數調整、成分股再平衡、配息、費率或基金規則變動"
    else:
        subjects = "財報、營運展望、公司公告、產品或監管事件"
    return f"確認是否有{subjects}支持此次波動；在事件完成驗證前，不直接產生交易結論。"


def event_display(event, asset_type: str = "EQUITY") -> dict[str, str]:
    data = event_data(event)
    raw = " ".join(str(data.get(key) or "") for key in ("title", "summary", "fact_summary")).lower()
    event_type = str(data.get("event_type") or "").upper()
    noun = "基金" if asset_type == "ETF" else "資產" if asset_type in {"CRYPTO", "TOKEN", "STABLECOIN"} else "公司"
    title = {"EARNINGS": "公司公布財報", "GUIDANCE": "公司更新營運展望", "PRODUCT": "公司發布產品或技術公告",
             "REGULATORY": "監管機關發布相關公告", "M&A": "公司發布併購公告",
             "MANAGEMENT": "公司發布管理層異動公告", "CAPITAL_ALLOCATION": "公司更新資本配置計畫"}.get(event_type, f"{noun}發布新的已驗證公告")
    fact = "已確認來源刊載此公告；具體內容尚未完成中文核對，不做額外事實推論。"
    interpretation = "這項公告可能改變原先假設，仍需核對執行內容與後續發展；目前不推論價格方向。"
    uncertainty = "事件已確認，不代表投資結果或價格方向已確定。"
    authorization = any(word in raw for word in ("repurchase", "buyback", "回購", "庫藏股")) and any(word in raw for word in ("authoriz", "授權"))
    if authorization:
        title = "董事會提高庫藏股回購授權額度"
        fact = "董事會提高既有庫藏股回購計畫的授權額度。"
        interpretation = "提高公司未來執行回購與資本回饋的彈性，但授權不代表立即買回股票，也不能單獨推論股價一定上漲。"
        uncertainty = "實際買回金額與執行時點仍須核對後續公告。"
    source = str(data.get("source_name") or "")
    if "nvidia" in source.lower():
        source_fallback = "NVIDIA 投資人關係網站" if "investor.nvidia.com" in str(data.get("source_url")) else "NVIDIA 官方新聞室"
    else:
        source_fallback = {"SEC": "美國證券交易委員會", "Reuters": "路透社", "Bloomberg": "彭博", "WSJ": "華爾街日報"}.get(source)
        if source_fallback is None:
            name = re.sub(r"\b(?:Investor Relations|IR|Newsroom)\b", "", source, flags=re.I).strip()
            source_fallback = f"{name or urlparse(data.get('source_url') or '').hostname or '已驗證'} 原始公告來源"
    fact_text = data.get("fact_summary") or data.get("summary") or ""
    if authorization or re.search(r"可能|有望|或將|推升|帶動股價", fact_text):
        fact_text = fact
    interpretation_text = data.get("investment_interpretation") or ""
    if authorization or re.search(r"一定上漲|保證|必然|穩賺", interpretation_text):
        interpretation_text = interpretation
    return {"title": chinese_text(data.get("display_title_zh") or data.get("title"), title),
            "fact": chinese_text(fact_text, fact),
            "interpretation": chinese_text(interpretation_text, interpretation),
            "uncertainty": chinese_text(data.get("uncertainty_note"), uncertainty),
            "source": chinese_text(source, source_fallback),
            "date": str(data.get("source_published_at") or data.get("published_at") or data.get("event_date") or "")[:10]}
