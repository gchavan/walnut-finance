"""In-memory tests for Walnut handoff fixes. Stdlib unittest + sqlite3 only."""

from __future__ import annotations

import sqlite3
import unittest
from datetime import date

from walnut import db
from walnut import simplefin


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


def _kid_name(conn: sqlite3.Connection) -> str:
    kids = db.household(conn)["kids"]
    if kids:
        return kids[0]["name"]
    db.add_person(conn, "TestKid", "kid")
    return "TestKid"


def _local_rec(stream_id: str, merchant: str = "Netflix", is_subscription: int = 0) -> dict:
    return {
        "stream_id": stream_id,
        "source": "local",
        "merchant": merchant,
        "average_cents": 1599,
        "frequency": "monthly",
        "is_subscription": is_subscription,
        "category": None,
        "next_date": "2026-09-01",
        "status": "active",
        "last_seen_ms": 1,
    }


class HandoffFixesTest(unittest.TestCase):
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
        n = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        self.assertEqual(n, 1)
        row = conn.execute("SELECT transaction_id FROM transactions").fetchone()
        self.assertEqual(row["transaction_id"], "t1")
        conn.close()

    def test_subscription_flag_survives_replace_local_recurring(self):
        conn = _mem()
        sid = "local:netflix"
        db.replace_local_recurring(conn, [_local_rec(sid)])
        db.set_subscription(conn, sid, True)
        flag = conn.execute(
            "SELECT is_subscription FROM recurring WHERE stream_id = ?", (sid,)
        ).fetchone()["is_subscription"]
        self.assertEqual(flag, 1)
        db.replace_local_recurring(conn, [_local_rec(sid, is_subscription=0)])
        flag = conn.execute(
            "SELECT is_subscription FROM recurring WHERE stream_id = ?", (sid,)
        ).fetchone()["is_subscription"]
        self.assertEqual(flag, 1)

        db.set_subscription(conn, sid, False)
        db.replace_local_recurring(conn, [_local_rec(sid, is_subscription=1)])
        flag = conn.execute(
            "SELECT is_subscription FROM recurring WHERE stream_id = ?", (sid,)
        ).fetchone()["is_subscription"]
        self.assertEqual(flag, 0)

        db.replace_local_recurring(conn, [_local_rec(sid, merchant="Netflix")])
        db.ignore_recurring(conn, sid)
        db.replace_local_recurring(conn, [_local_rec(sid, merchant="Netflix")])
        listed = db.list_recurring(conn)
        self.assertFalse(any(r["stream_id"] == sid for r in listed))
        conn.close()

    def test_kid_owned_account_defaults_hidden_adult_checking_included(self):
        conn = _mem()
        kid = _kid_name(conn)
        db.upsert_account(conn, _acct("kid1", f"{kid} Checking"))
        db.upsert_account(conn, _acct("adult1", "Chase Checking"))
        kid_row = db.get_account(conn, "kid1")
        adult_row = db.get_account(conn, "adult1")
        self.assertEqual(kid_row["hidden"], 1)
        self.assertEqual(adult_row["hidden"], 0)
        self.assertEqual(kid_row["owner"], kid)
        conn.close()

    def test_reassign_owner_to_kid_hides_unless_explicit_hidden(self):
        conn = _mem()
        kid = _kid_name(conn)
        adult = db.default_owner_name(conn)
        db.upsert_account(conn, _acct("a1", "Chase Checking"))
        self.assertEqual(db.get_account(conn, "a1")["hidden"], 0)
        db.update_account_prefs(conn, "a1", owner=kid)
        self.assertEqual(db.get_account(conn, "a1")["hidden"], 1)
        self.assertEqual(db.get_account(conn, "a1")["owner"], kid)

        db.upsert_account(conn, _acct("a2", "Other Checking"))
        self.assertEqual(db.get_account(conn, "a2")["hidden"], 0)
        db.update_account_prefs(conn, "a2", owner=kid, hidden=0)
        self.assertEqual(db.get_account(conn, "a2")["hidden"], 0)
        self.assertEqual(db.get_account(conn, "a2")["owner"], kid)

        db.update_account_prefs(conn, "a1", owner=adult)
        self.assertEqual(db.get_account(conn, "a1")["hidden"], 1)
        self.assertEqual(db.get_account(conn, "a1")["owner"], adult)
        conn.close()

    def test_coverage_simplefin_partial_even_for_today_only(self):
        conn = _mem()
        today = date.today().isoformat()
        db.upsert_account(conn, _acct("a1", "Chase Checking"))
        db.upsert_transaction(conn, _txn("t1", "a1", today))
        db.set_meta(conn, "source", "simplefin")
        cov = db.coverage(conn)
        self.assertTrue(cov["partial"])
        self.assertTrue(cov["partial_reason"])
        self.assertIn("90", cov["partial_reason"])
        self.assertEqual(cov["min_date"], today)
        self.assertEqual(cov["max_date"], today)
        conn.close()

    def test_persist_account_set_coerces_invalid_balance_date(self):
        conn = _mem()
        payload = {
            "accounts": [
                {
                    "id": "good",
                    "name": "Good Checking",
                    "balance": "10.00",
                    "balance-date": 1700000000,
                    "transactions": [],
                },
                {
                    "id": "bad",
                    "name": "Bad Checking",
                    "balance": "20.00",
                    "balance-date": "not-a-unix",
                    "transactions": [],
                },
            ]
        }
        n_added, n_modified, n_removed = simplefin.persist_account_set(conn, payload)
        ids = {
            r["account_id"]
            for r in conn.execute("SELECT account_id FROM accounts").fetchall()
        }
        self.assertIn("sfin:good", ids)
        self.assertIn("sfin:bad", ids)
        self.assertEqual(len(ids), 2)
        conn.close()


if __name__ == "__main__":
    unittest.main()
