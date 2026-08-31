"""SimpleFIN Bridge client (protocol v2) and sync.

Live bank source. Setup token (base64 claim URL) is claimed once via POST;
the Access URL is stored only in gitignored .env as SIMPLEFIN_ACCESS_URL.

Never log, print, or persist Access URLs, userinfo, or setup tokens.
Redact userinfo if a URL must appear in an error path.
"""

from __future__ import annotations

import base64
import json
import sqlite3
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import unquote, urlparse, urlunparse

import aiohttp

from . import db
from .config import Config, upsert_env_var

logger = logging.getLogger("walnut.simplefin")

_TIMEOUT = aiohttp.ClientTimeout(total=120, sock_connect=20, sock_read=90)
_DEFAULT_HISTORY_DAYS = 90
_CREATE_URL = "https://bridge.simplefin.org/simplefin/create"

_CARD_NEEDLES = (
    "credit card", "creditcard", "line of credit", "lineofcredit",
    "visa", "mastercard", "american express", "amex",
    "freedom unlimited", "freedom flex", "sapphire", "reserve card",
    "double cash", "venture", "preferred card",
)
_LOAN_NEEDLES = (
    "mortgage", "heloc", "auto loan", "student loan", "personal loan",
    "home loan", "liability",
)
_CASH_NEEDLES = (
    "checking", "savings", "money market", "spend", "cash management",
    "checking account", "savings account",
)


class SimplefinError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


def redact_url(url: str | None) -> str:
    """Strip userinfo so logs never contain Access URL credentials."""
    if not url:
        return ""
    try:
        p = urlparse(url)
        if not (p.username or p.password):
            return f"{p.scheme}://{p.hostname or ''}{p.path or ''}"
        host = p.hostname or ""
        if p.port:
            host = f"{host}:{p.port}"
        return f"{p.scheme}://***@{host}{p.path or ''}"
    except Exception:
        return "[redacted]"


def sanitize_error_text(msg: str) -> str:
    """Strip tags and anything that looks like an Access URL or token."""
    text = re.sub(r"<[^>]+>", "", str(msg or ""))
    text = re.sub(r"https://[^/\s:]+:[^@/\s]+@", "https://***@", text)
    if "simplefin_access_url" in text.lower() or "bearer " in text.lower():
        return "SimpleFIN request failed (details omitted to avoid leaking secrets)"
    return text.strip()


def _pad_b64(token: str) -> str:
    raw = re.sub(r"\s+", "", token)
    return raw + ("=" * ((4 - len(raw) % 4) % 4))


def decode_setup_token(token: str) -> str:
    """Base64-decode a setup token to a claim URL. Never include the token in errors."""
    blob = (token or "").strip()
    if not blob:
        raise SimplefinError("Paste a SimpleFIN setup token.", code="empty_token")
    try:
        decoded = base64.b64decode(_pad_b64(blob), validate=False)
        claim = decoded.decode("utf-8").strip()
    except Exception as exc:
        raise SimplefinError("That setup token is not valid base64.", code="bad_token") from exc
    parsed = urlparse(claim)
    if parsed.scheme != "https" or not parsed.hostname:
        raise SimplefinError(
            "That setup token did not decode to an HTTPS claim URL.",
            code="bad_token",
        )
    return claim


def split_access_url(access_url: str) -> tuple[str, str, str]:
    """Return (https base without userinfo, username, password). TLS-only."""
    parsed = urlparse((access_url or "").strip())
    if parsed.scheme != "https" or not parsed.hostname:
        raise SimplefinError("Access URL must be HTTPS.", code="insecure")
    user = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    host = parsed.hostname
    netloc = f"{host}:{parsed.port}" if parsed.port else host
    path = parsed.path.rstrip("/")
    base = urlunparse(("https", netloc, path, "", "", ""))
    return base, user, password


def sfin_amount_to_cents(amount: Any) -> int:
    """SimpleFIN positive = money in → Walnut positive = money out."""
    try:
        return int(round(-float(amount) * 100))
    except (TypeError, ValueError):
        return 0


def sfin_balance_to_cents(balance: Any, *, liability: bool) -> int | None:
    """MX card/loan balances are usually positive owed → store negative."""
    if balance is None or balance == "":
        return None
    try:
        cents = int(round(float(balance) * 100))
    except (TypeError, ValueError):
        return None
    if liability:
        return -abs(cents)
    return cents


def unix_to_date(ts: Any) -> str | None:
    try:
        n = int(float(ts))
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return datetime.fromtimestamp(n, tz=timezone.utc).date().isoformat()


def classify_simplefin(
    name: str | None,
    org: str | None,
    extra: dict | None = None,
) -> tuple[str, str, int]:
    """Return (group_key, type, on_budget) from name/org/extra. Do not invent institutions."""
    extra = extra if isinstance(extra, dict) else {}
    extra_type = str(
        extra.get("type")
        or extra.get("account-type")
        or extra.get("account_type")
        or extra.get("subtype")
        or ""
    ).lower().replace("_", " ").replace("-", " ")
    bits = [name or "", org or "", extra_type]
    for v in extra.values():
        if isinstance(v, str):
            bits.append(v)
    blob = " ".join(bits).lower()

    if any(k in blob for k in db._RETIREMENT_NEEDLES):
        return "retirement", "otherAsset", 0
    if any(k in blob for k in db._INVEST_NEEDLES):
        return "investments", "otherAsset", 0
    if extra_type in ("loan", "mortgage", "liability", "other liability") or any(
        k in blob for k in _LOAN_NEEDLES
    ):
        return "loans", "otherLiability", 0
    if extra_type in ("credit", "credit card", "creditcard", "line of credit") or any(
        k in blob for k in _CARD_NEEDLES
    ):
        return "cards", "creditCard", 1
    if re.search(r"\bcredit\b", blob) and "credit union" not in blob:
        return "cards", "creditCard", 1
    if extra_type in ("checking", "savings", "depository", "cash", "money market") or any(
        k in blob for k in _CASH_NEEDLES
    ):
        atype = "savings" if "saving" in blob else "checking"
        return "cash", atype, 1
    return "other", extra_type or "other", 1


def _org_for_account(raw: dict, connections: dict[str, dict]) -> str:
    conn_id = raw.get("conn_id")
    c = connections.get(conn_id) if conn_id else None
    if isinstance(c, dict):
        for key in ("name", "org_name"):
            if c.get(key):
                return str(c[key])
    if raw.get("conn_name"):
        return str(raw["conn_name"])
    org = raw.get("org")
    if isinstance(org, dict):
        return str(org.get("name") or org.get("domain") or "")
    return ""


def normalize_errlist(payload: dict | None) -> list[dict]:
    out: list[dict] = []
    body = payload or {}
    for e in body.get("errlist") or []:
        if isinstance(e, dict):
            msg = sanitize_error_text(str(e.get("msg") or ""))
            if not msg:
                continue
            out.append({
                "code": str(e.get("code") or ""),
                "msg": msg,
                "conn_id": e.get("conn_id"),
                "account_id": e.get("account_id"),
            })
        elif isinstance(e, str) and e.strip():
            out.append({"code": "", "msg": sanitize_error_text(e)})
    if not out:
        for e in body.get("errors") or []:
            if e:
                out.append({"code": "", "msg": sanitize_error_text(str(e))})
    return out


def store_access_url(cfg: Config, access_url: str) -> None:
    """Write SIMPLEFIN_ACCESS_URL into gitignored .env (mode 0600) and memory."""
    split_access_url(access_url)  # validate HTTPS + host
    upsert_env_var(cfg.env_path, "SIMPLEFIN_ACCESS_URL", access_url.strip())
    os.environ["SIMPLEFIN_ACCESS_URL"] = access_url.strip()
    cfg.simplefin_access_url = access_url.strip()


async def claim_setup_token(token: str) -> str:
    """POST empty body to the decoded claim URL once. Returns the Access URL."""
    claim_url = decode_setup_token(token)
    timeout = _TIMEOUT
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                claim_url,
                data=b"",
                headers={"Content-Length": "0"},
                allow_redirects=False,
                ssl=True,
            ) as resp:
                body = await resp.text()
                if resp.status == 403:
                    raise SimplefinError(
                        "This setup token was already used or is invalid. "
                        "Revoke it on SimpleFIN Bridge and create a new one.",
                        status=403,
                        code="claim_used",
                    )
                if resp.status >= 400:
                    raise SimplefinError(
                        "SimpleFIN claim failed. Create a new setup token and try again.",
                        status=resp.status,
                        code="claim_failed",
                    )
                access_url = (body or "").strip()
    except SimplefinError:
        raise
    except aiohttp.ClientError as exc:
        raise SimplefinError(
            f"SimpleFIN claim network error: {type(exc).__name__}",
            code="network",
        ) from exc
    try:
        split_access_url(access_url)
    except SimplefinError as exc:
        raise SimplefinError(
            "SimpleFIN claim did not return an Access URL.",
            code="bad_claim",
        ) from exc
    return access_url


async def fetch_accounts(
    cfg: Config,
    *,
    start_unix: int | None = None,
) -> dict:
    if not cfg.simplefin_access_url:
        raise SimplefinError(
            "SIMPLEFIN_ACCESS_URL is not set. Paste a setup token in the Walnut UI.",
            code="not_configured",
        )
    base, user, password = split_access_url(cfg.simplefin_access_url)
    if start_unix is None:
        start_unix = int((datetime.now(tz=timezone.utc) - timedelta(days=_DEFAULT_HISTORY_DAYS)).timestamp())
    url = f"{base}/accounts"
    params = {
        "start-date": str(start_unix),
        "pending": "1",
        "version": "2",
    }
    timeout = _TIMEOUT
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                url,
                auth=aiohttp.BasicAuth(user, password),
                params=params,
                ssl=True,
            ) as resp:
                if resp.status == 403:
                    raise SimplefinError(
                        "SimpleFIN access was rejected or revoked. "
                        "Create a new setup token at "
                        f"{_CREATE_URL} and reconnect in Walnut.",
                        status=403,
                        code="revoked",
                    )
                if resp.status == 402:
                    raise SimplefinError(
                        "SimpleFIN payment required. Check your SimpleFIN Bridge plan.",
                        status=402,
                        code="payment_required",
                    )
                if resp.status >= 400:
                    raise SimplefinError(
                        "SimpleFIN accounts request failed.",
                        status=resp.status,
                        code=str(resp.status),
                    )
                try:
                    payload = await resp.json(content_type=None)
                except Exception as exc:
                    raise SimplefinError(
                        "SimpleFIN returned a non-JSON accounts body.",
                        code="bad_json",
                    ) from exc
                return payload if isinstance(payload, dict) else {}
    except SimplefinError:
        raise
    except aiohttp.ClientError as exc:
        raise SimplefinError(
            f"SimpleFIN network error: {type(exc).__name__}",
            code="network",
        ) from exc


def persist_account_set(conn, payload: dict) -> tuple[int, int, int]:
    """Persist accounts + transactions. Returns (added, modified, removed)."""
    ts = db.now_ms()
    n_added = n_modified = n_removed = 0
    connections = {
        c.get("conn_id"): c
        for c in (payload.get("connections") or [])
        if isinstance(c, dict) and c.get("conn_id")
    }
    for raw in payload.get("accounts") or []:
        if not isinstance(raw, dict) or not raw.get("id"):
            continue
        extra = raw.get("extra") if isinstance(raw.get("extra"), dict) else {}
        org = _org_for_account(raw, connections)
        group_key, atype, on_budget = classify_simplefin(raw.get("name"), org, extra)
        liability = group_key in ("cards", "loans")
        currency = raw.get("currency") or "USD"
        if isinstance(currency, str) and currency.startswith("http"):
            currency = "USD"
        raw_aid = str(raw["id"])
        account_id = f"sfin:{raw_aid}"
        bal_ts = unix_to_date(raw.get("balance-date"))
        rec = {
            "account_id": account_id,
            "name": raw.get("name") or "",
            "type": atype,
            "group_key": group_key,
            "on_budget": 1 if on_budget else 0,
            "closed": 0,
            "note": org or None,
            "current_cents": sfin_balance_to_cents(raw.get("balance"), liability=liability),
            "cleared_cents": sfin_balance_to_cents(
                raw.get("available-balance") if raw.get("available-balance") not in (None, "") else raw.get("balance"),
                liability=liability,
            ),
            "iso_currency": currency,
            "updated_at_ms": ts,
        }
        raw_bal = raw.get("balance-date")
        if raw_bal not in (None, ""):
            try:
                rec["updated_at_ms"] = int(float(raw_bal) * 1000)
            except (TypeError, ValueError):
                rec["updated_at_ms"] = ts
        db.upsert_account(conn, rec)
        a, m, r = persist_transactions(
            conn,
            raw.get("transactions") or [],
            raw_account_id=raw_aid,
            account_id=account_id,
            account_name=rec["name"],
            group_key=group_key,
            iso=currency,
            ts=ts,
        )
        n_added += a
        n_modified += m
        n_removed += r
        if bal_ts:
            db.set_meta(conn, f"balance_date_{account_id}", bal_ts)
    return n_added, n_modified, n_removed


def persist_transactions(
    conn,
    txns: list,
    *,
    raw_account_id: str,
    account_id: str,
    iso: str | None,
    ts: int,
    account_name: str | None = None,
    group_key: str | None = None,
) -> tuple[int, int, int]:
    from . import categories as cats
    rules = cats.list_rules(conn)
    n_added = n_modified = n_removed = 0
    for raw in txns:
        if not isinstance(raw, dict) or not raw.get("id"):
            continue
        try:
            posted = int(float(raw.get("posted") or 0))
        except (TypeError, ValueError):
            posted = 0
        pending = bool(raw.get("pending")) or posted == 0
        day = unix_to_date(posted) or unix_to_date(raw.get("transacted_at")) or date.today().isoformat()
        payee = raw.get("description") or raw.get("payee") or ""
        amount_cents = sfin_amount_to_cents(raw.get("amount"))
        tid = f"sfin:{raw_account_id}:{raw['id']}"
        existed = conn.execute(
            "SELECT 1 FROM transactions WHERE transaction_id = ?",
            (tid,),
        ).fetchone()
        cat = None
        if not existed and not db.account_is_investment_spend(account_name, group_key):
            cat = cats.apply_category_to_new(payee, None, rules)
        rec = {
            "transaction_id": tid,
            "account_id": account_id,
            "date": day,
            "amount_cents": amount_cents,
            "iso_currency": iso,
            "pending": 1 if pending else 0,
            "payee": payee,
            "memo": raw.get("memo") if isinstance(raw.get("memo"), str) else None,
            "category_id": None,
            "category_name": cat,
            "transfer_account_id": None,
            "is_transfer": 0,
            "is_spending": db.is_spending_flag(
                pending=pending,
                amount_cents=amount_cents,
                is_transfer=False,
                payee=payee,
                category_name=None,
                account_name=account_name,
                group_key=group_key,
            ),
            "cleared": "uncleared" if pending else "cleared",
            "approved": 1,
            "parent_transaction_id": None,
            "first_seen_ms": ts,
            "last_seen_ms": ts,
        }
        action = db.upsert_transaction(conn, rec)
        if action == "added":
            n_added += 1
        else:
            n_modified += 1
    return n_added, n_modified, n_removed


def default_start_unix() -> int:
    return int((datetime.now(tz=timezone.utc) - timedelta(days=_DEFAULT_HISTORY_DAYS)).timestamp())


async def sync_all(cfg: Config, conn) -> dict:
    started = db.now_ms()
    sync_id = db.insert_sync(conn, started)
    if not cfg.simplefin_ready:
        db.finish_sync(
            conn, sync_id, n_added=0, n_modified=0, n_removed=0,
            ok=False, error="SIMPLEFIN_ACCESS_URL missing",
        )
        return {
            "ok": False,
            "n_added": 0, "n_modified": 0, "n_removed": 0,
            "errors": ["SIMPLEFIN_ACCESS_URL missing"],
            "errlist": [],
            "coverage": db.coverage(conn),
            "setup": True,
            "source": "simplefin",
        }

    start_unix = default_start_unix()
    start_iso = datetime.fromtimestamp(start_unix, tz=timezone.utc).date().isoformat()
    errors: list[str] = []
    errlist: list[dict] = []
    n_added = n_modified = n_removed = 0

    try:
        payload = await fetch_accounts(cfg, start_unix=start_unix)
    except SimplefinError as exc:
        logger.warning("simplefin fetch failed: %s", sanitize_error_text(str(exc)))
        db.finish_sync(
            conn, sync_id, n_added=0, n_modified=0, n_removed=0,
            ok=False, error=str(exc),
        )
        return {
            "ok": False,
            "n_added": 0, "n_modified": 0, "n_removed": 0,
            "errors": [str(exc)],
            "errlist": [],
            "coverage": db.coverage(conn),
            "code": exc.code,
            "source": "simplefin",
        }

    orig_sync_id = sync_id
    try:
        conn.execute("BEGIN IMMEDIATE")
        # First successful SimpleFIN sync: drop leftover ledger rows so ids
        # do not mix with sfin: ids. wipe_ledger also clears syncs, so
        # re-open the current sync row afterwards.
        if db.get_meta(conn, "source") != "simplefin":
            logger.info("switching live source to simplefin; wiping previous ledger rows")
            db.wipe_ledger(conn)
            sync_id = db.insert_sync(conn, started)
        db.set_meta(conn, "source", "simplefin")
        db.set_meta(conn, "transactions_since", start_iso)
        db.set_meta(conn, "plan_name", "SimpleFIN")
        from . import categories as cats
        cats.ensure_seeded(conn)
        n_added, n_modified, n_removed = persist_account_set(conn, payload)
        db.recompute_spending(conn)
        cats.apply_to_uncategorized(conn)
        errlist = normalize_errlist(payload)
        db.set_meta(conn, "simplefin_errlist", json.dumps(errlist))
        conn.execute("COMMIT")
    except Exception as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        err = sanitize_error_text(str(exc)) or type(exc).__name__
        logger.warning("simplefin persist failed: %s", err)
        db.finish_sync(
            conn, orig_sync_id, n_added=0, n_modified=0, n_removed=0,
            ok=False, error=err,
        )
        return {
            "ok": False,
            "n_added": 0, "n_modified": 0, "n_removed": 0,
            "errors": [err],
            "errlist": [],
            "coverage": db.coverage(conn),
            "source": "simplefin",
        }

    logger.info(
        "simplefin accounts=%s txns +%s ~%s -%s errlist=%s",
        db.account_count(conn), n_added, n_modified, n_removed, len(errlist),
    )

    ok = True
    db.finish_sync(
        conn, sync_id,
        n_added=n_added, n_modified=n_modified, n_removed=n_removed,
        ok=ok, error=None,
    )
    cov = db.coverage(conn)
    return {
        "ok": ok,
        "partial": bool(errlist),
        "n_added": n_added,
        "n_modified": n_modified,
        "n_removed": n_removed,
        "errors": errors,
        "errlist": errlist,
        "coverage": cov,
        "n_accounts": db.account_count(conn),
        "source": "simplefin",
        "plan_name": "SimpleFIN",
    }
