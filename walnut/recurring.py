"""Recurring subscriptions/bills.

SimpleFIN has no scheduled-transactions feed — use the local detector
(same payee, similar amount, regular interval).
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import date, timedelta
from statistics import mean

from . import db

logger = logging.getLogger("walnut.recurring")


_INTERVALS = (
    (7, 2, "WEEKLY"),
    (14, 3, "BIWEEKLY"),
    (30, 6, "MONTHLY"),
    (90, 12, "QUARTERLY"),
    (365, 25, "ANNUALLY"),
)

_FREQ_DAYS = {
    "WEEKLY": 7,
    "BIWEEKLY": 14,
    "SEMI_MONTHLY": 15,
    "EVERY_4_WEEKS": 28,
    "MONTHLY": 30,
    "EVERY_2_MONTHS": 60,
    "QUARTERLY": 90,
    "EVERY_4_MONTHS": 120,
    "SEMI_ANNUALLY": 182,
    "ANNUALLY": 365,
    "EVERY_2_YEARS": 730,
    "UNKNOWN": 30,
}


def _next_date(last: str | None, frequency: str | None) -> str | None:
    if not last:
        return None
    try:
        d = date.fromisoformat(str(last)[:10])
    except ValueError:
        return None
    days = _FREQ_DAYS.get((frequency or "MONTHLY").upper(), 30)
    nxt = d + timedelta(days=days)
    today = date.today()
    while nxt < today:
        nxt += timedelta(days=days)
    return nxt.isoformat()


def _infer_subscription(category: str | None, merchant: str | None) -> bool:
    blob = f"{category or ''} {merchant or ''}".upper()
    return "SUBSCRIPTION" in blob or "STREAMING" in blob


def _classify_interval(gaps: list[int]) -> str | None:
    if not gaps:
        return None
    avg = mean(gaps)
    for target, slop, label in _INTERVALS:
        if abs(avg - target) <= slop:
            return label
    return None


def detect_local(conn, *, skip_payees: set[str] | None = None) -> list[dict]:
    """Group posted spending by payee; keep streams with a regular cadence."""
    skip = {s.strip().lower() for s in (skip_payees or set()) if s}
    rows = conn.execute(
        """
        SELECT payee, date, amount_cents, category_name
        FROM transactions
        WHERE pending = 0 AND is_spending = 1
          AND COALESCE(NULLIF(payee, ''), '') != ''
        ORDER BY payee, date
        """
    ).fetchall()
    groups: dict[str, list] = defaultdict(list)
    for r in rows:
        key = (r["payee"] or "").strip()
        if not key or key.lower() in skip:
            continue
        groups[key].append(r)

    ts = db.now_ms()
    out: list[dict] = []
    for merchant, txns in groups.items():
        if len(txns) < 3:
            continue
        dates = []
        for t in txns:
            try:
                dates.append(date.fromisoformat(t["date"]))
            except ValueError:
                continue
        dates.sort()
        if len(dates) < 3:
            continue
        gaps = [(b - a).days for a, b in zip(dates, dates[1:]) if (b - a).days > 0]
        freq = _classify_interval(gaps)
        if not freq:
            continue
        amounts = [t["amount_cents"] for t in txns]
        avg_amt = mean(amounts)
        tol = max(abs(avg_amt) * 0.20, 200)
        similar = [a for a in amounts if abs(a - avg_amt) <= tol]
        if len(similar) < 3:
            continue
        last = dates[-1].isoformat()
        category = txns[-1]["category_name"]
        rec = {
            "stream_id": "local:" + re.sub(r"[^a-z0-9]+", "-", merchant.lower())[:80].strip("-"),
            "source": "local",
            "merchant": merchant,
            "average_cents": int(round(mean(similar))),
            "frequency": freq,
            "is_subscription": 1 if _infer_subscription(category, merchant) else 0,
            "category": category,
            "next_date": _next_date(last, freq),
            "status": "DETECTED",
            "last_seen_ms": ts,
        }
        out.append(rec)
    return out


def refresh(conn) -> dict:
    """Local detector: same payee, similar amount, regular interval."""
    recs = detect_local(conn)
    db.replace_local_recurring(conn, recs)
    logger.info("local detector %s streams", len(recs))
    return {"source": "local", "n_local": len(recs)}
