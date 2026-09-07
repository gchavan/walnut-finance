"""90-day cap is a coverage notice; last-sync dump redacts secrets. Stdlib unittest."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from walnut import db
from walnut import simplefin
from walnut.config import Config


def _mem():
    import sqlite3
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    return conn


class SimplefinNoticesTest(unittest.TestCase):
    def test_default_history_is_89_inclusive_days(self):
        self.assertEqual(simplefin._DEFAULT_HISTORY_DAYS, 89)
        start = simplefin.default_start_unix()
        now = int(datetime.now(tz=timezone.utc).timestamp())
        delta = now - start
        self.assertGreater(delta, 88 * 86400 - 2)
        self.assertLess(delta, 89 * 86400 + 2)
        ninety = int((datetime.now(tz=timezone.utc) - timedelta(days=90)).timestamp())
        self.assertGreater(start, ninety)

    def test_90_day_errlist_item_is_notice_not_error(self):
        payload = {
            "errlist": [
                "Requested date range exceeds limit of 90 days and was capped.",
                {
                    "code": "cap",
                    "msg": "Requested date range exceeds limit of 90 days and was capped.",
                },
                {"code": "conn", "msg": "Connection XYZ is broken"},
            ]
        }
        items = simplefin.normalize_errlist(payload)
        errors, notices = simplefin.split_errlist(items)
        self.assertEqual(len(notices), 2)
        self.assertEqual(len(errors), 1)
        self.assertIn("broken", errors[0]["msg"])
        self.assertTrue(
            simplefin.is_coverage_notice(
                "Requested date range exceeds limit of 90 days and was capped."
            )
        )
        self.assertTrue(simplefin.is_coverage_notice("range was capped at 90 days"))
        self.assertFalse(simplefin.is_coverage_notice("Connection XYZ is broken"))
        self.assertFalse(bool(errors) is False)
        self.assertEqual(bool(errors), True)
        only_cap = simplefin.normalize_errlist({
            "errlist": ["Requested date range exceeds limit of 90 days and was capped."]
        })
        err_only, notice_only = simplefin.split_errlist(only_cap)
        self.assertEqual(err_only, [])
        self.assertTrue(notice_only)
        self.assertFalse(bool(err_only))

    def test_last_sync_dump_writes_without_secrets(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = Config()
        cfg.data_dir = tmp
        payload = {
            "access_url": "https://user:secretpass@bridge.example/simplefin",
            "password": "hunter2",
            "setup_token": "abc123setup",
            "userinfo": "user:pass",
            "SIMPLEFIN_ACCESS_URL": "https://user:secretpass@bridge.example/simplefin",
            "accounts": [
                {
                    "id": "acct1",
                    "name": "Brokerage",
                    "balance": "100.00",
                    "org": {
                        "name": "Broker",
                        "url": "https://alice:s3cret@bank.example/path",
                    },
                    "holdings": [
                        {
                            "id": "h1",
                            "symbol": "AAPL",
                            "cost_basis": "0",
                            "created": 1700000000,
                        }
                    ],
                }
            ],
            "errlist": [
                "Requested date range exceeds limit of 90 days and was capped."
            ],
        }
        simplefin.write_last_sync_dumps(cfg, payload)
        sync_path = tmp / "simplefin-last-sync.json"
        hold_path = tmp / "simplefin-last-holdings.json"
        self.assertTrue(sync_path.is_file())
        self.assertTrue(hold_path.is_file())
        self.assertEqual(stat.S_IMODE(os.stat(sync_path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(hold_path).st_mode), 0o600)
        sync_text = sync_path.read_text(encoding="utf-8")
        hold_text = hold_path.read_text(encoding="utf-8")
        for blob in (sync_text, hold_text):
            self.assertNotIn("secretpass", blob)
            self.assertNotIn("hunter2", blob)
            self.assertNotIn("abc123setup", blob)
            self.assertNotIn("user:pass", blob)
            self.assertNotIn("s3cret", blob)
        self.assertIn("[redacted]", sync_text)
        hold = json.loads(hold_text)
        self.assertEqual(hold[0]["id"], "acct1")
        self.assertEqual(hold[0]["name"], "Brokerage")
        self.assertEqual(hold[0]["balance"], "100.00")
        self.assertIn("holdings", hold[0])
        self.assertNotIn("org", hold[0])
        self.assertEqual(hold[0]["holdings"][0]["symbol"], "AAPL")

    def test_persist_holdings_maps_alt_date_and_basis_fields(self):
        conn = _mem()
        payload = {
            "accounts": [
                {
                    "id": "broker",
                    "name": "Robinhood Brokerage",
                    "balance": "1500.00",
                    "balance-date": 1700000000,
                    "holdings": [
                        {
                            "id": "h-alt",
                            "symbol": "MSFT",
                            "description": "Microsoft",
                            "shares": "2",
                            "market_value": "800.00",
                            "basis": "400.00",
                            "purchase_date": "2024-03-15",
                        },
                        {
                            "id": "h-extra",
                            "symbol": "GOOG",
                            "description": "Alphabet",
                            "shares": "1",
                            "market_value": "150.00",
                            "extra": {
                                "cost_basis": "100.00",
                                "acquired": 1700000000,
                            },
                        },
                        {
                            "id": "h-zero",
                            "symbol": "CASH",
                            "description": "Cash",
                            "shares": "10",
                            "market_value": "10.00",
                            "cost_basis": "0.00",
                        },
                    ],
                    "transactions": [],
                }
            ]
        }
        simplefin.persist_account_set(conn, payload)
        rows = {
            r["symbol"]: r
            for r in conn.execute("SELECT * FROM holdings").fetchall()
        }
        self.assertEqual(rows["MSFT"]["cost_basis_cents"], 40000)
        self.assertEqual(rows["MSFT"]["created"], "2024-03-15")
        self.assertEqual(rows["GOOG"]["cost_basis_cents"], 10000)
        self.assertEqual(rows["GOOG"]["created"], "2023-11-14")
        self.assertEqual(rows["CASH"]["cost_basis_cents"], 0)
        conn.close()


if __name__ == "__main__":
    unittest.main()
