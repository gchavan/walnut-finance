"""Spend subcategory slots under category tags."""

from __future__ import annotations

import asyncio
import sqlite3
import unittest

from walnut import categories as cats
from walnut import db
from walnut.config import Config
from walnut.server import build_app


def _mem() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    return conn


def _acct(conn):
    db.upsert_account(conn, {
        "account_id": "sfin:a",
        "name": "Checking",
        "type": "checking",
        "group_key": "cash",
        "on_budget": 1,
        "closed": 0,
        "note": None,
        "current_cents": 10000,
        "cleared_cents": 10000,
        "iso_currency": "USD",
        "updated_at_ms": 1,
    })


def _txn(tid, day, cents, payee="Store", cat=None, sub=None):
    return {
        "transaction_id": tid,
        "account_id": "sfin:a",
        "date": day,
        "amount_cents": cents,
        "iso_currency": "USD",
        "pending": 0,
        "payee": payee,
        "memo": None,
        "category_id": None,
        "category_name": cat,
        "subcategory": sub,
        "transfer_account_id": None,
        "is_transfer": 0,
        "is_spending": 1,
        "cleared": "cleared",
        "approved": 1,
        "parent_transaction_id": None,
        "first_seen_ms": 1,
        "last_seen_ms": 1,
    }


class SubcategoryTest(unittest.TestCase):
    def test_migrate_columns(self):
        conn = _mem()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(transactions)")}
        self.assertIn("subcategory", cols)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(category_rules)")}
        self.assertIn("subcategory", cols)

    def test_rule_apply_sets_slot(self):
        conn = _mem()
        _acct(conn)
        db.upsert_transaction(conn, _txn("t1", "2026-09-01", 12500, payee="SPOTIFY USA"))
        out = cats.apply_to_matching(
            conn, category="Entertainment & Rec.", pattern="SPOTIFY", subcategory="Music",
        )
        self.assertEqual(out["n"], 1)
        row = conn.execute(
            "SELECT category_name, subcategory FROM transactions WHERE transaction_id='t1'"
        ).fetchone()
        self.assertEqual(row["category_name"], "Entertainment & Rec.")
        self.assertEqual(row["subcategory"], "Music")

    def test_rule_update_api_forwards_subcategory(self):
        conn = _mem()
        _acct(conn)
        db.upsert_transaction(conn, _txn("t1", "2026-09-01", 12500, payee="AVILA CREATIVE SOCCER AUSTIN TX"))
        db.upsert_transaction(conn, _txn("t2", "2026-09-02", 3200, payee="AVILA CREATIVE SOCCER #2"))
        db.upsert_transaction(conn, _txn("t3", "2026-09-03", 1800, payee="AVILA CREATIVE SOCCER JUNIOR"))
        cats.apply_to_matching(conn, category="Kids", pattern="AVILA CREATIVE SOCCER")
        rule_id = conn.execute(
            "SELECT rule_id FROM category_rules WHERE pattern = ?", ("AVILA CREATIVE SOCCER",)
        ).fetchone()[0]

        async def _run():
            app = build_app(Config(), conn)
            from aiohttp.test_utils import TestClient, TestServer
            async with TestClient(TestServer(app)) as client:
                response = await client.patch(
                    "/api/categories/rules/" + rule_id,
                    json={"subcategory": "Soccer"},
                )
                self.assertEqual(response.status, 200)
                self.assertEqual((await response.json())["subcategory"], "Soccer")
                added = await client.post(
                    "/api/categories/apply",
                    json={"pattern": "AVILA CREATIVE SOCCER JUNIOR", "category": "Kids", "subcategory": "Junior"},
                )
                self.assertEqual(added.status, 200)
                self.assertEqual((await added.json())["subcategory"], "Junior")

        asyncio.run(_run())
        rows = conn.execute(
            "SELECT category_name, subcategory FROM transactions ORDER BY transaction_id"
        ).fetchall()
        self.assertEqual([(row["category_name"], row["subcategory"]) for row in rows], [
            ("Kids", "Soccer"), ("Kids", "Soccer"), ("Kids", "Junior"),
        ])
        conn.close()

    def test_spending_segments(self):
        conn = _mem()
        _acct(conn)
        db.upsert_transaction(conn, _txn("t1", "2026-09-01", 12500, cat="Kids", sub="Music"))
        db.upsert_transaction(conn, _txn("t2", "2026-09-02", 3200, cat="Kids", sub=None))
        rep = db.spending_report(conn, period="custom", start="2026-09-01", end="2026-09-30")
        kids = next(r for r in rep["by_category"] if r["category"] == "Kids")
        names = {s["name"]: s["cents"] for s in kids["subcategories"]}
        self.assertEqual(names["Music"], 12500)
        self.assertEqual(names["Other"], 3200)


if __name__ == "__main__":
    unittest.main()
