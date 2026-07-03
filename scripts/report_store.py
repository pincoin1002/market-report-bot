#!/usr/bin/env python3
"""30-day report store on SQLite FTS5 (trigram tokenizer for CJK matching).

DB file: data/reports.db — committed to the repo by bot.yml for persistence.
Regenerable at any time from reports/*.md via ingest_dir().
"""

import logging
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("report_store")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id          INTEGER PRIMARY KEY,
    report_type TEXT NOT NULL,
    market_date TEXT NOT NULL,           -- YYYYMMDD
    filename    TEXT NOT NULL UNIQUE,
    content     TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS reports_fts USING fts5(
    content, content='reports', content_rowid='id', tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS reports_ai AFTER INSERT ON reports BEGIN
    INSERT INTO reports_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS reports_ad AFTER DELETE ON reports BEGIN
    INSERT INTO reports_fts(reports_fts, rowid, content)
    VALUES ('delete', old.id, old.content);
END;
"""

_FNAME = re.compile(r"^(tw_open|tw_close|us_open|us_close)_(\d{8})_\d{6}\.md$")


class ReportStore:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(exist_ok=True)
        self._con = sqlite3.connect(db_path)
        self._con.executescript(_SCHEMA)

    def ingest_dir(self, reports_dir: Path) -> int:
        """Idempotently import any report files not yet in the DB."""
        if not reports_dir.exists():
            return 0
        added = 0
        for f in sorted(reports_dir.glob("*.md")):
            m = _FNAME.match(f.name)
            if not m:
                continue
            try:
                cur = self._con.execute(
                    "INSERT OR IGNORE INTO reports "
                    "(report_type, market_date, filename, content) VALUES (?,?,?,?)",
                    (m.group(1), m.group(2), f.name,
                     f.read_text(encoding="utf-8")))
                added += cur.rowcount
            except sqlite3.Error:
                log.warning("ingest failed", extra={"file": f.name})
        self._con.commit()
        if added:
            log.info("reports ingested", extra={"added": added})
        return added

    def latest(self) -> tuple[str, str] | None:
        """(market_date, content) of the most recent report of any type."""
        row = self._con.execute(
            "SELECT market_date, content FROM reports "
            "ORDER BY market_date DESC, id DESC LIMIT 1").fetchone()
        return row if row else None

    def search(self, query: str, exclude_latest: bool = True,
               limit: int = 2, excerpt_chars: int = 3000) -> list[str]:
        """Top FTS matches for the question, as dated excerpts for the prompt."""
        terms = re.findall(r"[\w一-鿿]{2,}", query)[:6]
        if not terms:
            return []
        fts_query = " OR ".join(f'"{t}"' for t in terms)
        try:
            rows = self._con.execute(
                "SELECT r.report_type, r.market_date, r.content "
                "FROM reports_fts f JOIN reports r ON r.id = f.rowid "
                "WHERE reports_fts MATCH ? ORDER BY rank LIMIT ?",
                (fts_query, limit + 1)).fetchall()
        except sqlite3.OperationalError:
            log.warning("fts query failed", extra={"query": fts_query})
            return []
        latest = self.latest()
        out = []
        for rtype, mdate, content in rows:
            if exclude_latest and latest and content == latest[1]:
                continue
            date_fmt = f"{mdate[:4]}-{mdate[4:6]}-{mdate[6:]}"
            out.append(f"【歷史報告 {date_fmt} {rtype}（節錄）】\n{content[:excerpt_chars]}")
        return out[:limit]

    def prune(self, keep_days: int = 30) -> int:
        cutoff = (datetime.now() - timedelta(days=keep_days)).strftime("%Y%m%d")
        cur = self._con.execute(
            "DELETE FROM reports WHERE market_date < ?", (cutoff,))
        self._con.commit()
        if cur.rowcount:
            log.info("old reports pruned", extra={"removed": cur.rowcount})
        return cur.rowcount

    def close(self) -> None:
        self._con.close()
