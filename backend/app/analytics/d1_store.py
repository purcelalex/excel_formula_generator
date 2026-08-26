"""
d1_store.py — the analytics store for Cloudflare Workers.

Same job as store.py, different storage. A Worker has no filesystem, so the
SQLite file is replaced by D1, Cloudflare's managed SQLite. The schema is
identical; only the access is — D1's client is asynchronous and its results
come back as JavaScript objects.

Two things differ from the file-backed store and both are deliberate:

  * Every method is async. D1 calls cross into the JavaScript runtime.
  * Recording a request never raises. Analytics failing must not fail the
    user's request, which is the same rule store.py follows.
"""

from __future__ import annotations

import time
from typing import Any

INSERT = (
    "INSERT INTO requests (created_at, language, platform, locale, source,"
    " description, intent, function, status, column_count, column_types)"
    " VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11)"
)


def _rows(result: Any) -> list[dict]:
    """Turn a D1 result into a list of plain dicts.

    The object arriving from D1 is a JavaScript proxy. Pyodide exposes `.to_py()`
    on it; the fallbacks cover shape differences between runtime versions rather
    than hypothetical ones — this is the seam most likely to shift.
    """
    if result is None:
        return []

    payload = getattr(result, "results", result)

    to_py = getattr(payload, "to_py", None)
    if callable(to_py):
        payload = to_py()

    out: list[dict] = []
    for row in payload or []:
        row_to_py = getattr(row, "to_py", None)
        if callable(row_to_py):
            row = row_to_py()
        out.append(dict(row))
    return out


class D1AnalyticsStore:
    """D1-backed analytics. Schema lives in worker/migrations/."""

    def __init__(self, database: Any):
        self.db = database

    async def record(self, description: str, result, source: str) -> None:
        """Log one request. Never raises — see the module docstring."""
        try:
            columns = result.columns or []
            now = time.time()
            await (
                self.db.prepare(INSERT)
                .bind(
                    now,
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
                )
                .run()
            )
        except Exception:
            pass

    async def summary(self, limit: int = 20) -> dict:
        async def query(sql: str, *params) -> list[dict]:
            statement = self.db.prepare(sql)
            if params:
                statement = statement.bind(*params)
            return _rows(await statement.all())

        total_rows = await query("SELECT COUNT(*) AS total FROM requests")
        total = int(total_rows[0]["total"]) if total_rows else 0

        unanswered = await query(
            "SELECT description, COUNT(*) AS uses, MAX(created_at) AS last_seen"
            " FROM requests WHERE status IN ('no_match','template')"
            " GROUP BY LOWER(description) ORDER BY uses DESC, last_seen DESC LIMIT ?1",
            limit,
        )
        popular = await query(
            "SELECT description, COUNT(*) AS uses FROM requests"
            " GROUP BY LOWER(description) ORDER BY uses DESC LIMIT ?1",
            limit,
        )
        functions = await query(
            "SELECT function, COUNT(*) AS uses FROM requests"
            " WHERE function IS NOT NULL GROUP BY function ORDER BY uses DESC LIMIT ?1",
            limit,
        )
        by_language = await query(
            "SELECT language, COUNT(*) AS uses FROM requests GROUP BY language"
        )
        by_status = await query(
            "SELECT status, COUNT(*) AS uses FROM requests GROUP BY status"
        )
        by_source = await query(
            "SELECT source, COUNT(*) AS uses FROM requests GROUP BY source"
        )

        answered = sum(r["uses"] for r in by_status if r.get("status") == "filled")
        return {
            "total_requests": total,
            "answer_rate": round(answered / total, 3) if total else None,
            "unmet_needs": unanswered,
            "popular_requests": popular,
            "top_functions": functions,
            "by_language": by_language,
            "by_status": by_status,
            "by_source": by_source,
        }
