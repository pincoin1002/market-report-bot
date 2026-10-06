"""Bounded publication-date verification during event ingestion, never rendering."""
import re
from datetime import datetime, timezone
from html import unescape
from urllib.parse import urlparse
import ipaddress

import requests


def parse_publication_evidence(html: str, expected_title: str) -> datetime | None:
    titles = re.findall(r"<(?:title|h1)[^>]*>(.*?)</(?:title|h1)>", html, re.I | re.S)
    actual_title = " ".join(unescape(re.sub(r"<[^>]+>", " ", text)) for text in titles).lower()
    words = set(re.findall(r"[a-z]{4,}", expected_title.lower())) - {"announces", "nvidia", "launches"}
    if words:
        if len(words & set(re.findall(r"[a-z]{4,}", actual_title))) / len(words) < 0.5:
            return None
    else:
        chars = set(re.findall(r"[\u4e00-\u9fff]", expected_title))
        if not chars or len(chars & set(actual_title)) / len(chars) < 0.8:
            return None
    dates = []
    for tag in re.findall(r"<meta\b[^>]*>", html, re.I):
        if re.search(r'(?:article:published_time|datepublished|pubdate|publication_date)', tag, re.I):
            match = re.search(r'content=["\']([^"\']+)', tag, re.I)
            if match:
                dates.append(match.group(1))
    dates += re.findall(r'"datePublished"\s*:\s*"([^"]+)"', html, re.I)
    dates += re.findall(r'<time[^>]*datetime=["\']([^"\']+)', html, re.I)
    # Official IR templates sometimes expose only a visible publication date.
    dates += re.findall(r'\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+20\d{2}\b', re.sub(r"<[^>]+>", " ", html))
    for date in dates:
        try:
            parsed = datetime.fromisoformat(date.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = datetime.strptime(date, "%B %d, %Y")
            except ValueError:
                continue
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    return None


def verify_event_publication(result: dict) -> dict:
    out = dict(result)
    # Model output cannot self-certify a publication timestamp.
    out["publication_date_verified"] = False
    out["source_published_at"] = None
    url = str(out.get("source_url") or "")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return out
    if "grounding-api-redirect" in parsed.path or parsed.hostname in {"localhost", "vertexaisearch.cloud.google.com"}:
        return out
    try:
        if ipaddress.ip_address(parsed.hostname).is_private:
            return out
    except ValueError:
        pass
    try:
        response = requests.get(url, timeout=10, headers={"User-Agent": "market-report-bot/1.0"}, allow_redirects=False)
        response.raise_for_status()
        if response.status_code != 200 or len(response.content) > 1_000_000:
            return out
        published = parse_publication_evidence(response.text, str(out.get("raw_source_title") or out.get("title") or ""))
        if published:
            out.update(source_published_at=published.isoformat(), publication_date_verified=True)
    except (requests.RequestException, ValueError):
        pass
    return out
