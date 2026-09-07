"""Infer holding purchase dates and lot cost from investment transactions.

SimpleFIN/MX holding `created` is a snapshot day, not a purchase date.
When brokerage payee text is enough to rebuild lots for a holding, attach
purchase_date / price / cost at read time. Otherwise leave the date blank
and keep whatever cost the snapshot already stored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Generic broker wording → ticker. No household names.
_NAME_ALIASES: dict[str, str] = {
    "invesco qqq": "qqq",
    "coreweave": "crwv",
    "coinbase": "coin",
    "nvidia": "nvda",
    "amazon": "amzn",
    "alphabet": "googl",
    "google": "googl",
    "microsoft": "msft",
    "facebook": "meta",
    "netflix": "nflx",
    "berkshire": "brk.b",
    "palantir": "pltr",
    "apple": "aapl",
    "tesla": "tsla",
    "meta": "meta",
    "amd": "amd",
}

_OPTION_RE = re.compile(
    r"\b(?:call|put)\b.*\bstrike\b|\bto (?:open|close)\b|\boptions?\b",
    re.I,
)
_REINVEST_DIV_RE = re.compile(r"\b(?:reinvestment|dividend)\b", re.I)
_RH_EQUITY_RE = re.compile(
    r"^(buy|sell)\s+([\d,.]+)\s+shares?\s+of\s+(.+?)\s+for\s+\$?([\d,.]+)\s+each\b",
    re.I,
)
_FIDELITY_RE = re.compile(
    r"^you\s+(bought|sold)\s+(?:([\d,.]+)\s+)?(.+)$",
    re.I,
)
_PAREN_TOKEN_RE = re.compile(r"\(([A-Za-z][A-Za-z0-9.\-]{0,9})\)")
_PRICE_AT_RE = re.compile(r"@\s*\$?\s*([\d,.]+)")
_SKIP_PARENS = frozenset({
    "cash", "margin", "stock", "to", "from", "new", "old", "drip",
})
_QTY_MATCH_ABS = 0.02
_QTY_MATCH_REL = 0.01


@dataclass(frozen=True)
class Trade:
    side: str
    qty: float | None
    price: float | None
    date: str
    name: str
    ticker: str | None
    payee: str


def _fold(s: str | None) -> str:
    return re.sub(r"[^a-z0-9.]+", " ", (s or "").lower()).strip()


def _alias_ticker(name: str) -> str | None:
    n = _fold(name)
    if not n:
        return None
    if n in _NAME_ALIASES:
        return _NAME_ALIASES[n]
    for alias, ticker in sorted(_NAME_ALIASES.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"\b{re.escape(alias)}\b", n):
            return ticker
    return None


def _looks_like_ticker(token: str) -> bool:
    t = (token or "").strip()
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9.]{0,5}", t))


def _ticker_from_parens(text: str) -> str | None:
    for m in _PAREN_TOKEN_RE.finditer(text or ""):
        tok = m.group(1)
        if tok.lower() in _SKIP_PARENS:
            continue
        if _looks_like_ticker(tok) or re.fullmatch(r"[A-Za-z][A-Za-z0-9.\-]{0,9}", tok):
            return tok.upper()
    return None


def _is_option(text: str) -> bool:
    return bool(_OPTION_RE.search(text or ""))


def parse_trade(
    payee: str | None,
    date: str | None,
    amount_cents: int | None = None,
) -> Trade | None:
    """Parse one Walnut investment row into a buy/sell trade, or None."""
    del amount_cents  # qty/price come from payee text, not the cash amount
    text = (payee or "").strip()
    day = (date or "").strip()[:10]
    if not text or not day:
        return None
    if _is_option(text):
        return None
    if _REINVEST_DIV_RE.search(text):
        return None

    m = _RH_EQUITY_RE.match(text)
    if m:
        name = m.group(3).strip()
        ticker = _alias_ticker(name)
        if ticker is None and _looks_like_ticker(name):
            ticker = name.upper()
        try:
            qty = float(m.group(2).replace(",", ""))
            price = float(m.group(4).replace(",", ""))
        except ValueError:
            return None
        return Trade(
            side=m.group(1).lower(),
            qty=qty,
            price=price,
            date=day,
            name=name,
            ticker=ticker,
            payee=text,
        )

    m = _FIDELITY_RE.match(text)
    if m:
        rest = (m.group(3) or "").strip()
        ticker = _ticker_from_parens(rest)
        name = rest
        paren = _PAREN_TOKEN_RE.search(rest)
        if paren:
            name = rest[: paren.start()].strip()
        name = re.sub(r"\s+", " ", name).strip(" -")
        qty = None
        raw_qty = m.group(2)
        if raw_qty:
            try:
                qty = float(raw_qty.replace(",", ""))
            except ValueError:
                qty = None
        price = None
        pm = _PRICE_AT_RE.search(text)
        if pm:
            try:
                price = float(pm.group(1).replace(",", ""))
            except ValueError:
                price = None
        if ticker is None:
            ticker = _alias_ticker(name)
        if ticker is None and _looks_like_ticker(name):
            ticker = name.upper()
        if not name and ticker:
            name = ticker
        if not name:
            return None
        side = "buy" if m.group(1).lower() == "bought" else "sell"
        return Trade(
            side=side,
            qty=qty,
            price=price,
            date=day,
            name=name,
            ticker=ticker,
            payee=text,
        )

    return None


def trade_matches_holding(trade: Trade, holding: dict) -> bool:
    symbol = (holding.get("symbol") or "").strip()
    desc = (holding.get("description") or "").strip()
    name = (trade.name or "").strip()
    ticker = (trade.ticker or "").strip()
    fold_name = _fold(name)
    fold_desc = _fold(desc)
    fold_sym = _fold(symbol)

    if fold_sym:
        if fold_name and fold_sym == fold_name:
            return True
        if ticker and fold_sym == _fold(ticker):
            return True
        alias = _alias_ticker(name) or (ticker and _alias_ticker(ticker))
        if alias and fold_sym == _fold(alias):
            return True

    if fold_name and fold_desc:
        if fold_name in fold_desc or fold_desc in fold_name:
            return True
    return False


def _shares(holding: dict) -> float | None:
    raw = holding.get("shares")
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _qty_close(a: float, b: float) -> bool:
    return abs(a - b) <= max(abs(b) * _QTY_MATCH_REL, _QTY_MATCH_ABS)


def _fifo_remaining(trades: list[Trade]) -> list[dict]:
    ordered = sorted(
        trades,
        key=lambda t: (t.date, 0 if t.side == "buy" else 1, t.payee),
    )
    lots: list[dict] = []
    for t in ordered:
        if t.qty is None or t.qty <= 0:
            continue
        if t.side == "buy":
            lots.append({"date": t.date, "qty": t.qty, "price": t.price or 0.0})
            continue
        left = t.qty
        while left > 1e-12 and lots:
            lot = lots[0]
            take = min(lot["qty"], left)
            lot["qty"] -= take
            left -= take
            if lot["qty"] <= 1e-12:
                lots.pop(0)
    return [lot for lot in lots if lot["qty"] > 1e-12]


def _newest_lots(lots: list[dict], held: float) -> list[dict]:
    need = held
    out: list[dict] = []
    for lot in reversed(lots):
        if need <= 1e-12:
            break
        take = min(lot["qty"], need)
        out.append({"date": lot["date"], "qty": take, "price": lot["price"]})
        need -= take
    out.reverse()
    return out


def _format_purchase_date(lots: list[dict]) -> str | None:
    dates = sorted({str(lot["date"]) for lot in lots if lot.get("date")})
    if not dates:
        return None
    if len(dates) == 1:
        return dates[0]
    return f"{dates[0]} – {dates[-1]}"


def _lot_payload(lot: dict) -> dict:
    qty = float(lot["qty"])
    price = float(lot["price"] or 0.0)
    return {
        "date": str(lot.get("date") or "")[:10],
        "shares": qty,
        "purchase_price_cents": int(round(price * 100)),
        "cost_basis_cents": int(round(qty * price * 100)),
    }


def _apply_lots(holding: dict, lots: list[dict]) -> dict:
    ordered = sorted(lots, key=lambda lot: (str(lot.get("date") or ""),))
    holding["purchase_date"] = _format_purchase_date(ordered)
    total_qty = sum(float(lot["qty"]) for lot in ordered)
    total_cost = sum(float(lot["qty"]) * float(lot.get("price") or 0.0) for lot in ordered)
    if total_qty > 0:
        holding["purchase_price_cents"] = int(round((total_cost / total_qty) * 100))
        holding["cost_basis_cents"] = int(round(total_cost * 100))
    holding["lots"] = [_lot_payload(lot) for lot in ordered]
    return holding


def _unique_buy_date(matched: list[Trade]) -> str | None:
    buys = [t for t in matched if t.side == "buy"]
    dates = {t.date for t in buys if t.date}
    if len(dates) == 1:
        return next(iter(dates))
    return None


def _enrich_one(holding: dict, trades: list[Trade]) -> dict:
    out = dict(holding)
    out.setdefault("purchase_date", None)
    matched = [t for t in trades if trade_matches_holding(t, out)]
    held = _shares(out)
    qty_trades = [t for t in matched if t.qty is not None and t.qty > 0]

    if held is not None and qty_trades:
        remaining = _fifo_remaining(qty_trades)
        rem_qty = sum(lot["qty"] for lot in remaining)
        if _qty_close(rem_qty, held):
            if remaining:
                return _apply_lots(out, remaining)
        elif rem_qty > held and held > 0:
            newest = _newest_lots(remaining, held)
            if newest:
                return _apply_lots(out, newest)
        # remaining < held: missing buys — do not invent a date; keep MX cost

    if not qty_trades:
        day = _unique_buy_date(matched)
        if day:
            out["purchase_date"] = day
    return out


def enrich_holdings(holdings: list | None, transactions: list | None) -> list[dict]:
    """Return holding dicts with purchase_date (and lot price/cost when rebuilt)."""
    trades: list[Trade] = []
    for raw in transactions or []:
        parsed = parse_trade(
            raw.get("payee") if isinstance(raw, dict) else None,
            raw.get("date") if isinstance(raw, dict) else None,
            raw.get("amount_cents") if isinstance(raw, dict) else None,
        )
        if parsed:
            trades.append(parsed)
    return [_enrich_one(h if isinstance(h, dict) else {}, trades) for h in holdings or []]
