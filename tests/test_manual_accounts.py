"""In-memory tests for manual loans and properties. Stdlib unittest + sqlite3 only."""

from __future__ import annotations

import sqlite3
import unittest

from walnut import db


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


class ManualAccountsTest(unittest.TestCase):
    def test_loan_math_30y_mortgage_split(self):
        original = 40000000
        owed = 38000000
        rate_bps = 625
        term_months = 360
        math = db.loan_math(
            original_cents=original,
            current_cents=owed,
            rate_bps=rate_bps,
            term_months=term_months,
            originated_on="2020-01",
            category="Mortgage",
        )
        interest = int(round(owed * 0.0625 / 12))
        payment = db.amortizing_payment_cents(original, rate_bps, term_months)
        self.assertEqual(math["interest_cents"], interest)
        self.assertEqual(math["payment_cents"], payment)
        self.assertEqual(math["principal_cents"], payment - interest)

    def test_property_and_mortgage_change_net_worth(self):
        conn = _mem()
        base = db.net_worth(conn)
        db.create_property(
            conn,
            name="Primary",
            kind="Primary Home",
            value_cents=50000000,
        )
        db.create_manual_loan(
            conn,
            name="Mortgage",
            category="Mortgage",
            owed_cents=30000000,
            original_cents=40000000,
            originated_on="2020-01",
            rate_bps=625,
            term_years=30,
        )
        nw = db.net_worth(conn)
        self.assertEqual(nw["assets_cents"], base["assets_cents"] + 50000000)
        self.assertEqual(nw["liabilities_cents"], base["liabilities_cents"] + 30000000)
        self.assertEqual(
            nw["net_worth_cents"],
            base["net_worth_cents"] + 50000000 - 30000000,
        )
        conn.close()

    def test_linked_piti_then_unlink(self):
        conn = _mem()
        prop = db.create_property(
            conn,
            name="House",
            kind="Primary Home",
            value_cents=50000000,
            tax_cents=40000,
            insurance_cents=15000,
            hoa_cents=5000,
        )
        loan = db.create_manual_loan(
            conn,
            name="Mortgage",
            category="Mortgage",
            owed_cents=30000000,
            original_cents=40000000,
            originated_on="2020-01",
            rate_bps=625,
            term_years=30,
            property_id=prop["property_id"],
        )
        extra = 40000 + 15000 + 5000
        self.assertEqual(
            loan["loan"]["piti_cents"],
            loan["loan"]["payment_cents"] + extra,
        )
        self.assertEqual(loan["loan"]["property_id"], prop["property_id"])
        updated = db.update_manual_loan(conn, loan["account_id"], property_id=None)
        self.assertIsNone(updated["loan"]["property_id"])
        self.assertEqual(updated["loan"]["piti_cents"], updated["loan"]["payment_cents"])
        conn.close()

    def test_wipe_ledger_keeps_property_and_manual_loan(self):
        conn = _mem()
        prop = db.create_property(
            conn,
            name="Land lot",
            kind="Land",
            value_cents=1000000,
        )
        loan = db.create_manual_loan(
            conn,
            name="Auto",
            category="Auto loan",
            owed_cents=800000,
            original_cents=1200000,
            originated_on="2024-06",
            rate_bps=599,
            term_years=5,
        )
        db.upsert_account(conn, _acct("sfin:fake", "Fake Checking"))
        ids = {
            r["account_id"]
            for r in conn.execute("SELECT account_id FROM accounts").fetchall()
        }
        self.assertIn("sfin:fake", ids)
        db.wipe_ledger(conn)
        ids = {
            r["account_id"]
            for r in conn.execute("SELECT account_id FROM accounts").fetchall()
        }
        self.assertNotIn("sfin:fake", ids)
        self.assertIn(loan["account_id"], ids)
        found = db.get_property(conn, prop["property_id"])
        self.assertIsNotNone(found)
        self.assertEqual(found["name"], "Land lot")
        kept = db.get_account(conn, loan["account_id"])
        self.assertIsNotNone(kept)
        self.assertIsNotNone(kept.get("loan"))
        conn.close()

    def test_delete_property_nulls_loan_link(self):
        conn = _mem()
        prop = db.create_property(
            conn,
            name="House",
            kind="Primary Home",
            value_cents=25000000,
        )
        loan = db.create_manual_loan(
            conn,
            name="Mortgage",
            category="Mortgage",
            owed_cents=18000000,
            original_cents=20000000,
            originated_on="2019-03",
            rate_bps=400,
            term_years=30,
            property_id=prop["property_id"],
        )
        self.assertEqual(loan["loan"]["property_id"], prop["property_id"])
        db.delete_property(conn, prop["property_id"])
        self.assertIsNone(db.get_property(conn, prop["property_id"]))
        kept = db.get_account(conn, loan["account_id"])
        self.assertIsNotNone(kept)
        self.assertIsNone(kept["loan"]["property_id"])
        conn.close()

    def test_rename_person_updates_property_owner(self):
        conn = _mem()
        hh = db.household(conn)
        self_person = hh["self"]
        old = self_person["name"]
        prop = db.create_property(
            conn,
            name="Cabin",
            kind="Vacation Home",
            value_cents=9000000,
            owner=old,
        )
        self.assertEqual(prop["owner"], old)
        db.update_person(conn, self_person["id"], "RenamedOwner")
        found = db.get_property(conn, prop["property_id"])
        self.assertEqual(found["owner"], "RenamedOwner")
        conn.close()

    def test_simplefin_mortgage_can_link_property(self):
        conn = _mem()
        db.upsert_account(conn, _acct("sfin:1", "Bank Mortgage"))
        prop = db.create_property(
            conn,
            name="Home",
            kind="Primary Home",
            value_cents=1000,
        )
        acct = db.update_account_prefs(
            conn, "sfin:1", category="Mortgage", property_id=prop["property_id"]
        )
        self.assertEqual(acct["property_id"], prop["property_id"])
        db.update_account_prefs(conn, "sfin:1", category="Checking")
        cleared = db.get_account(conn, "sfin:1")
        self.assertIsNone(cleared["property_id"])
        db.update_account_prefs(conn, "sfin:1", category="Mortgage", property_id=prop["property_id"])
        db.delete_property(conn, prop["property_id"])
        gone = db.get_account(conn, "sfin:1")
        self.assertIsNone(gone["property_id"])
        conn.close()


if __name__ == "__main__":
    unittest.main()
