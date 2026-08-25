"""
store.py — what users asked for, and whether we could answer it.

The product reason this exists: the requests the engine could NOT answer are a
ranked list of what to build next. A `no_match` or a `template` result is a
missing knowledge-base entry or a missing intent rule, named by a real user in
their own words. That is more valuable than the successful requests.

Privacy: the description is stored because the whole point is reading what
people asked. Table contents are NOT stored — not the cells, not the headers,
which can name a customer or a project. Column count and types are enough to
understand usage and carry no personal data.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    REAL    NOT NULL,
    language      TEXT,
    platform      TEXT,
    locale        TEXT,
    source        TEXT,            -- none | paste | xlsx | csv | described
    description   TEXT    NOT NULL,
    intent        TEXT,
    function      TEXT,
    status        TEXT,            -- filled | template | no_match
    column_count  INTEGER,
    column_types  TEXT             -- e.g. "text,text,number"
);
CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status);
CREATE INDEX IF NOT EXISTS idx_requests_created ON requests(created_at);
"""


class AnalyticsStore:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def record(self, description: str, result, source: str) -> None:
        """Log one request. Never raises: analytics must not break the product."""
        try:
            columns = result.columns or []
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO requests (created_at, language, platform, locale, source,"
                    " description, intent, function, status, column_count, column_types)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        time.time(),
                        result.language,
                        result.platform,
                        result.locale,
                        source,
                        description,
                        result.intent,
                        result.function,
                        result.status,
                        len(columns),
                        ",".join(c.get("type", "") for c in columns),
                    ),
                )
        except sqlite3.Error:
            # A failed write loses one analytics row. Failing the user's request
            # over it would be the wrong trade.
            pass

    # ---- reporting -------------------------------------------------------

    def summary(self, limit: int = 20) -> dict:
        with self._connect() as connection:
            total = connection.execute("SELECT COUNT(*) FROM requests").fetchone()[0]

            unanswered = connection.execute(
                "SELECT description, COUNT(*) AS uses, MAX(created_at) AS last_seen"
                " FROM requests WHERE status IN ('no_match','template')"
                " GROUP BY LOWER(description) ORDER BY uses DESC, last_seen DESC LIMIT ?",
                (limit,),
            ).fetchall()

            popular = connection.execute(
                "SELECT description, COUNT(*) AS uses FROM requests"
                " GROUP BY LOWER(description) ORDER BY uses DESC LIMIT ?",
                (limit,),
            ).fetchall()

            functions = connection.execute(
                "SELECT function, COUNT(*) AS uses FROM requests"
                " WHERE function IS NOT NULL GROUP BY function ORDER BY uses DESC LIMIT ?",
                (limit,),
            ).fetchall()

            by_language = connection.execute(
                "SELECT language, COUNT(*) AS uses FROM requests GROUP BY language"
            ).fetchall()

            by_status = connection.execute(
                "SELECT status, COUNT(*) AS uses FROM requests GROUP BY status"
            ).fetchall()

            by_source = connection.execute(
                "SELECT source, COUNT(*) AS uses FROM requests GROUP BY source"
            ).fetchall()

        answered = sum(r["uses"] for r in by_status if r["status"] == "filled")
        return {
            "total_requests": total,
            "answer_rate": round(answered / total, 3) if total else None,
            "unmet_needs": [dict(row) for row in unanswered],
            "popular_requests": [dict(row) for row in popular],
            "top_functions": [dict(row) for row in functions],
            "by_language": [dict(row) for row in by_language],
            "by_status": [dict(row) for row in by_status],
            "by_source": [dict(row) for row in by_source],
        }
