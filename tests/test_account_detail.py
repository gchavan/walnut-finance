"""Holdings persist, account detail API, schema guard. Stdlib unittest + sqlite3."""

from __future__ import annotations

import asyncio
import sqlite3
import unittest
from urllib.parse import quote

from walnut import db
from walnut import simplefin
from walnut.config import Config
from walnut.server import build_app


def _mem() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    return conn


def _acct(account_id: str, name: str, group_key: str = "cash") -> dict:
    return {
        "account_id": account_id,
        "name": name,
        "type": "checking",
        "group_key": group_key,
        "on_budget": 1,
        "closed": 0,
        "note": None,
        "current_cents": 10000,
        "cleared_cents": 10000,
        "iso_currency": "USD",
        "updated_at_ms": 1,
    }


def _txn(tid: str, account_id: str, day: str) -> dict:
    return {
        "transaction_id": tid,
        "account_id": account_id,
        "date": day,
        "amount_cents": 500,
        "iso_currency": "USD",
        "pending": 0,
        "payee": "Store",
        "memo": None,
        "category_id": None,
        "category_name": None,
        "transfer_account_id": None,
        "is_transfer": 0,
        "is_spending": 1,
        "cleared": "cleared",
        "approved": 1,
        "parent_transaction_id": None,
        "first_seen_ms": 1,
        "last_seen_ms": 1,
    }


def _broker_payload(holdings=None, extra_accounts=None):
    body = {
        "accounts": [
            {
                "id": "broker",
                "name": "Robinhood Brokerage",
                "balance": "1500.00",
                "balance-date": 1700000000,
                "org": {"name": "Robinhood"},
                "holdings": holdings if holdings is not None else [
                    {
                        "id": "h1",
                        "created": 1700000000,
                        "currency": "USD",
                        "cost_basis": "1000.00",
                        "description": "Apple Inc",
                        "market_value": "1234.56",
                        "purchase_price": "100.00",
                        "shares": "10.5",
                        "symbol": "AAPL",
                    },
                    {
                        "id": "h2",
                        "created": 1700000100,
                        "currency": "USD",
                        "cost_basis": "200.00",
                        "description": "Vanguard ETF",
                        "market_value": "250.00",
                        "purchase_price": "50.00",
                        "shares": "4",
                        "symbol": "VOO",
                    },
                ],
                "transactions": [
                    {
                        "id": "tx1",
                        "posted": 1700000000,
                        "amount": "-12.34",
                        "description": "Buy AAPL",
                        "pending": False,
                    },
                ],
            }
        ]
    }
    if extra_accounts:
        body["accounts"].extend(extra_accounts)
    return body


class AccountDetailTest(unittest.TestCase):
    def test_schema_mismatch_does_not_drop_transactions(self):
        conn = _mem()
        db.upsert_account(conn, _acct("a1", "Checking"))
        db.upsert_transaction(conn, _txn("t1", "a1", "2026-08-01"))
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            ("old-schema",),
        )
        with self.assertRaises(RuntimeError) as ctx:
            db.init_db(conn)
        msg = str(ctx.exception)
        self.assertIn("untouched", msg.lower())
        self.assertIn("old-schema", msg)
        self.assertIn(db.SCHEMA_VERSION, msg)
        n = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        self.assertEqual(n, 1)
        row = conn.execute("SELECT transaction_id FROM transactions").fetchone()
        self.assertEqual(row["transaction_id"], "t1")
        conn.close()

    def test_legacy_schema_id_accepted_and_rewritten(self):
        conn = _mem()
        db.upsert_account(conn, _acct("a1", "Checking"))
        db.upsert_transaction(conn, _txn("t1", "a1", "2026-08-01"))
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            ("ynab-1",),
        )
        db.init_db(conn)
        ver = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        self.assertEqual(ver, db.SCHEMA_VERSION)
        self.assertEqual(db.SCHEMA_VERSION, "walnut-1")
        self.assertEqual(db.LEGACY_SCHEMA_VERSIONS, ("ynab-1",))
        n = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        self.assertEqual(n, 1)
        row = conn.execute("SELECT transaction_id FROM transactions").fetchone()
        self.assertEqual(row["transaction_id"], "t1")
        conn.close()

    def test_persist_simplefin_holdings(self):
        conn = _mem()
        simplefin.persist_account_set(conn, _broker_payload())
        rows = conn.execute(
            "SELECT * FROM holdings WHERE account_id = ? ORDER BY symbol",
            ("sfin:broker",),
        ).fetchall()
        self.assertEqual(len(rows), 2)
        aapl = {r["symbol"]: r for r in rows}["AAPL"]
        self.assertEqual(aapl["holding_id"], "sfin-hold:broker:h1")
        self.assertEqual(aapl["description"], "Apple Inc")
        self.assertEqual(aapl["shares"], "10.5")
        self.assertEqual(aapl["market_value_cents"], 123456)
        self.assertEqual(aapl["cost_basis_cents"], 100000)
        self.assertEqual(aapl["purchase_price_cents"], 10000)
        self.assertEqual(aapl["currency"], "USD")
        voo = {r["symbol"]: r for r in rows}["VOO"]
        self.assertEqual(voo["holding_id"], "sfin-hold:broker:h2")
        self.assertEqual(voo["market_value_cents"], 25000)

        simplefin.persist_account_set(conn, _broker_payload(holdings=[
            {
                "id": "h3",
                "currency": "USD",
                "cost_basis": "50.00",
                "description": "Cash-like",
                "market_value": "50.00",
                "purchase_price": "1.00",
                "shares": "50",
                "symbol": "USD",
            },
        ]))
        ids = [
            r["holding_id"]
            for r in conn.execute(
                "SELECT holding_id FROM holdings WHERE account_id = ?",
                ("sfin:broker",),
            )
        ]
        self.assertEqual(ids, ["sfin-hold:broker:h3"])

        simplefin.persist_account_set(conn, _broker_payload(holdings=[]))
        n = conn.execute(
            "SELECT COUNT(*) FROM holdings WHERE account_id = ?",
            ("sfin:broker",),
        ).fetchone()[0]
        self.assertEqual(n, 0)
        conn.close()

    def test_cost_basis_from_purchase_price_times_shares(self):
        conn = _mem()
        simplefin.persist_account_set(conn, _broker_payload(holdings=[
            {
                "id": "h4",
                "currency": "USD",
                "cost_basis": "0.00",
                "description": "Vanguard S&P",
                "market_value": "500.00",
                "purchase_price": "40.00",
                "shares": "10.5",
                "symbol": "VOO",
            },
        ]))
        row = conn.execute(
            "SELECT cost_basis_cents, purchase_price_cents, shares FROM holdings WHERE symbol = ?",
            ("VOO",),
        ).fetchone()
        self.assertEqual(row["purchase_price_cents"], 4000)
        self.assertEqual(row["shares"], "10.5")
        self.assertEqual(row["cost_basis_cents"], 42000)
        conn.close()

    def test_money_string_to_cents(self):
        self.assertEqual(db.money_string_to_cents("1234.56"), 123456)
        self.assertEqual(db.money_string_to_cents("1,000.00"), 100000)
        self.assertEqual(db.money_string_to_cents(10), 1000)
        self.assertIsNone(db.money_string_to_cents("not-a-dollar"))
        self.assertIsNone(db.money_string_to_cents(None))
        self.assertIsNone(db.money_string_to_cents(""))

    def test_detail_api_returns_holdings(self):
        conn = _mem()
        simplefin.persist_account_set(conn, _broker_payload())

        async def _run():
            app = build_app(Config(), conn)
            from aiohttp.test_utils import TestClient, TestServer
            async with TestClient(TestServer(app)) as client:
                missing = await client.get("/api/accounts/" + quote("sfin:nope", safe=""))
                self.assertEqual(missing.status, 404)
                body = await missing.json()
                self.assertIn("error", body)

                resp = await client.get("/api/accounts/" + quote("sfin:broker", safe=""))
                self.assertEqual(resp.status, 200)
                data = await resp.json()
                acct = data["account"]
                self.assertEqual(acct["account_id"], "sfin:broker")
                self.assertEqual(acct["current_cents"], 150000)
                holds = {h["symbol"]: h for h in data["holdings"]}
                self.assertEqual(holds["AAPL"]["market_value_cents"], 123456)
                self.assertEqual(holds["AAPL"]["cost_basis_cents"], 100000)
                self.assertEqual(holds["AAPL"]["shares"], "10.5")
                self.assertTrue(data["transactions"])
                self.assertTrue(
                    any(t["account_id"] == "sfin:broker" for t in data["transactions"])
                )

        asyncio.run(_run())
        conn.close()

    def test_mx_created_unix_is_not_purchase_date(self):
        conn = _mem()
        simplefin.persist_account_set(conn, _broker_payload())
        rows = {
            r["symbol"]: r
            for r in conn.execute("SELECT * FROM holdings").fetchall()
        }
        self.assertTrue(rows["AAPL"]["created"] in (None, ""))
        self.assertTrue(rows["VOO"]["created"] in (None, ""))
        detail = db.get_account_detail(conn, "sfin:broker")
        holds = {h["symbol"]: h for h in detail["holdings"]}
        self.assertTrue(holds["AAPL"].get("created") in (None, ""))
        self.assertTrue(holds["AAPL"].get("purchase_date") in (None, ""))
        self.assertTrue(holds["VOO"].get("purchase_date") in (None, ""))
        conn.close()

    def test_robinhood_buy_enriches_purchase_date_after_detail(self):
        conn = _mem()
        posted = 1718496000  # 2024-06-16 UTC
        payload = _broker_payload(holdings=[
            {
                "id": "h-acme",
                "created": 1700000000,
                "currency": "USD",
                "cost_basis": "0.00",
                "description": "Acme Corp",
                "market_value": "600.00",
                "purchase_price": "10.00",
                "shares": "50",
                "symbol": "ACME",
            },
        ])
        payload["accounts"][0]["transactions"] = [
            {
                "id": "tx-buy",
                "posted": posted,
                "amount": "-500.00",
                "description": "buy 50.000000 shares of ACME for $10.00 each",
                "pending": False,
            },
        ]
        simplefin.persist_account_set(conn, payload)
        row = conn.execute(
            "SELECT created, purchase_price_cents, cost_basis_cents FROM holdings WHERE symbol = ?",
            ("ACME",),
        ).fetchone()
        self.assertTrue(row["created"] in (None, ""))
        self.assertEqual(row["purchase_price_cents"], 1000)
        self.assertEqual(row["cost_basis_cents"], 50000)

        detail = db.get_account_detail(conn, "sfin:broker")
        h = detail["holdings"][0]
        self.assertTrue(h.get("created") in (None, ""))
        day = simplefin.unix_to_date(posted)
        self.assertEqual(h["purchase_date"], day)
        self.assertEqual(h["purchase_price_cents"], 1000)
        self.assertEqual(h["cost_basis_cents"], 50000)
        conn.close()


if __name__ == "__main__":
    unittest.main()
