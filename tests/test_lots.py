"""Lot reconstruction from brokerage payee text. Stdlib unittest only."""

from __future__ import annotations

import json
import unittest

from walnut.lots import enrich_holdings, parse_trade


def _holding(**kwargs):
    rec = {
        "holding_id": kwargs.get("holding_id", "h1"),
        "account_id": "sfin:broker",
        "symbol": kwargs.get("symbol", "ACME"),
        "description": kwargs.get("description", "Acme Corp"),
        "shares": kwargs.get("shares", "100"),
        "market_value_cents": kwargs.get("market_value_cents", 12000),
        "cost_basis_cents": kwargs.get("cost_basis_cents", 10000),
        "purchase_price_cents": kwargs.get("purchase_price_cents", 100),
        "currency": "USD",
        "created": kwargs.get("created"),
    }
    rec.update(kwargs)
    return rec


def _txn(payee: str, day: str, amount_cents: int = 0):
    return {"payee": payee, "date": day, "amount_cents": amount_cents}


class ParseTradeTest(unittest.TestCase):
    def test_robinhood_equity_buy_and_sell(self):
        buy = parse_trade(
            "buy 1000.000000 shares of DRAM for $53.70 each",
            "2026-06-16",
            5370000,
        )
        self.assertIsNotNone(buy)
        self.assertEqual(buy.side, "buy")
        self.assertEqual(buy.qty, 1000.0)
        self.assertEqual(buy.price, 53.70)
        self.assertEqual(buy.name, "DRAM")
        self.assertEqual(buy.date, "2026-06-16")

        sell = parse_trade(
            "sell 500.000000 shares of NVIDIA for $225.90 each",
            "2026-06-26",
            -11295000,
        )
        self.assertIsNotNone(sell)
        self.assertEqual(sell.side, "sell")
        self.assertEqual(sell.qty, 500.0)
        self.assertEqual(sell.price, 225.90)
        self.assertEqual(sell.name, "NVIDIA")

    def test_skips_option_contracts(self):
        self.assertIsNone(
            parse_trade(
                "buy 10.000 DRAM call with strike of $71.00 for $1.85 each to close",
                "2026-06-16",
            )
        )

    def test_skips_reinvestment_and_dividend(self):
        self.assertIsNone(
            parse_trade("REINVESTMENT FIDELITY 500 INDEX (FXAIX)", "2026-06-01")
        )
        self.assertIsNone(
            parse_trade("DIVIDEND RECEIVED ACME CORP (ACME)", "2026-06-01")
        )

    def test_fidelity_you_bought_without_qty(self):
        t = parse_trade("YOU BOUGHT ACME CORP COM (ACME) (Cash)", "2026-04-02")
        self.assertIsNotNone(t)
        self.assertEqual(t.side, "buy")
        self.assertIsNone(t.qty)
        self.assertEqual(t.ticker, "ACME")
        self.assertEqual(t.date, "2026-04-02")


class EnrichHoldingsTest(unittest.TestCase):
    def test_fifo_match_sets_date_price_cost(self):
        holdings = [
            _holding(symbol="ACME", description="Acme Corp", shares="120",
                     cost_basis_cents=999, purchase_price_cents=1)
        ]
        txns = [
            _txn("buy 100.000000 shares of ACME for $10.00 each", "2026-01-01"),
            _txn("buy 50.000000 shares of ACME for $12.00 each", "2026-02-01"),
            _txn("sell 30.000000 shares of ACME for $15.00 each", "2026-03-01"),
        ]
        out = enrich_holdings(holdings, txns)
        self.assertEqual(len(out), 1)
        h = out[0]
        self.assertEqual(h["purchase_date"], "2026-01-01 – 2026-02-01")
        # remaining 70 @ 10 + 50 @ 12; avg 10.8333... → 1083 cents; cost $1300
        self.assertEqual(h["purchase_price_cents"], 1083)
        self.assertEqual(h["cost_basis_cents"], 130000)

    def test_alias_nvidia_matches_nvda(self):
        holdings = [_holding(symbol="NVDA", description="NVIDIA Corp", shares="500")]
        txns = [
            _txn("buy 500.000000 shares of NVIDIA for $225.90 each", "2026-06-16"),
        ]
        h = enrich_holdings(holdings, txns)[0]
        self.assertEqual(h["purchase_date"], "2026-06-16")
        self.assertEqual(h["purchase_price_cents"], 22590)
        self.assertEqual(h["cost_basis_cents"], 11295000)

    def test_incomplete_history_leaves_date_blank_keeps_mx_cost(self):
        holdings = [
            _holding(
                symbol="ACME",
                shares="100",
                cost_basis_cents=40000,
                purchase_price_cents=400,
            )
        ]
        txns = [
            _txn("buy 10.000000 shares of ACME for $4.00 each", "2026-06-01"),
        ]
        h = enrich_holdings(holdings, txns)[0]
        self.assertTrue(h.get("purchase_date") in (None, ""))
        self.assertEqual(h["cost_basis_cents"], 40000)
        self.assertEqual(h["purchase_price_cents"], 400)
        self.assertFalse(h.get("lots"))

    def test_newest_lots_when_buys_exceed_held(self):
        holdings = [_holding(symbol="ACME", shares="50", cost_basis_cents=1)]
        txns = [
            _txn("buy 100.000000 shares of ACME for $10.00 each", "2026-01-01"),
            _txn("buy 50.000000 shares of ACME for $20.00 each", "2026-06-01"),
        ]
        h = enrich_holdings(holdings, txns)[0]
        self.assertEqual(h["purchase_date"], "2026-06-01")
        self.assertEqual(h["purchase_price_cents"], 2000)
        self.assertEqual(h["cost_basis_cents"], 100000)

    def test_does_not_copy_snapshot_created(self):
        holdings = [_holding(symbol="ACME", shares="10", created="2026-08-01")]
        h = enrich_holdings(holdings, [])[0]
        self.assertTrue(h.get("purchase_date") in (None, ""))
        self.assertEqual(h["created"], "2026-08-01")

    def test_fidelity_unique_buy_sets_date_not_reinvestment(self):
        holdings = [
            _holding(
                symbol="ACME",
                shares="3.5",
                cost_basis_cents=7000,
                purchase_price_cents=2000,
            )
        ]
        txns = [
            _txn("YOU BOUGHT ACME CORP COM (ACME) (Cash)", "2026-04-02"),
            _txn("REINVESTMENT ACME CORP (ACME)", "2026-05-15"),
            _txn("DIVIDEND RECEIVED ACME CORP (ACME)", "2026-05-15"),
        ]
        h = enrich_holdings(holdings, txns)[0]
        self.assertEqual(h["purchase_date"], "2026-04-02")
        self.assertEqual(h["cost_basis_cents"], 7000)
        self.assertEqual(h["purchase_price_cents"], 2000)
        self.assertFalse(h.get("lots"))

    def test_three_matching_buys_expose_each_lot(self):
        holdings = [_holding(symbol="DRAM", description="DRAM", shares="3000")]
        txns = [
            _txn("buy 1000.000000 shares of DRAM for $53.70 each", "2026-06-30"),
            _txn("buy 1000.000000 shares of DRAM for $55.00 each", "2026-07-07"),
            _txn("buy 1000.000000 shares of DRAM for $57.10 each", "2026-07-31"),
        ]
        h = enrich_holdings(holdings, txns)[0]
        self.assertEqual(h["purchase_date"], "2026-06-30 – 2026-07-31")
        lots = h["lots"]
        self.assertEqual(len(lots), 3)
        self.assertEqual(
            [lot["date"] for lot in lots],
            ["2026-06-30", "2026-07-07", "2026-07-31"],
        )
        self.assertEqual([lot["shares"] for lot in lots], [1000.0, 1000.0, 1000.0])
        self.assertEqual(lots[0]["purchase_price_cents"], 5370)
        self.assertEqual(lots[0]["cost_basis_cents"], 5370000)
        self.assertEqual(lots[1]["purchase_price_cents"], 5500)
        self.assertEqual(lots[1]["cost_basis_cents"], 5500000)
        self.assertEqual(lots[2]["purchase_price_cents"], 5710)
        self.assertEqual(lots[2]["cost_basis_cents"], 5710000)
        # parent still shows average price / total basis
        self.assertEqual(h["purchase_price_cents"], 5527)
        self.assertEqual(h["cost_basis_cents"], 16580000)
        restored = json.loads(json.dumps(h))
        self.assertEqual(len(restored["lots"]), 3)
        self.assertEqual(
            [lot["date"] for lot in restored["lots"]],
            ["2026-06-30", "2026-07-07", "2026-07-31"],
        )

    def test_option_trade_does_not_match_equity_holding(self):
        holdings = [_holding(symbol="DRAM", description="DRAM Inc", shares="1000")]
        txns = [
            _txn(
                "buy 10.000 DRAM call with strike of $71.00 for $1.85 each to close",
                "2026-06-16",
            )
        ]
        h = enrich_holdings(holdings, txns)[0]
        self.assertTrue(h.get("purchase_date") in (None, ""))


if __name__ == "__main__":
    unittest.main()
