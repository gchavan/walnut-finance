"""aiohttp dashboard: one-page app + JSON API. No web framework beyond aiohttp.

Cache-Control: no-cache on every response. Missing SimpleFIN access URL does
not crash startup — the UI explains how to paste a setup token.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, timedelta
from pathlib import Path
import sqlite3

from aiohttp import web

from . import db
from . import recurring as rec
from . import simplefin
from .config import Config

logger = logging.getLogger("walnut.server")
STATIC_DIR = Path(__file__).resolve().parent / "static"


async def revalidate(request: web.Request, response: web.StreamResponse) -> None:
    response.headers.setdefault("Cache-Control", "no-cache")


def _json(data, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda o: json.dumps(o, default=str))


def _errlist(conn: sqlite3.Connection) -> list:
    raw = db.get_meta(conn, "simplefin_errlist")
    if not raw:
        return []
    try:
        val = json.loads(raw)
        return val if isinstance(val, list) else []
    except json.JSONDecodeError:
        return []


def _source_payload(cfg: Config, conn: sqlite3.Connection) -> dict:
    live = cfg.live_source
    return {
        "source": live,
        "live_source": live,
        "simplefin_ready": cfg.simplefin_ready,
        "errlist": _errlist(conn) if cfg.simplefin_ready else [],
    }


async def read_json(request: web.Request) -> dict:
    if not request.can_read_body:
        return {}
    try:
        data = await request.json()
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


async def run_live_sync(cfg: Config, conn: sqlite3.Connection) -> dict:
    from . import categories as cats
    result = await simplefin.sync_all(cfg, conn)
    db.recompute_spending(conn)
    cats.apply_to_uncategorized(conn)
    rec.refresh(conn)
    return result


async def handle_index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(STATIC_DIR / "index.html")


async def handle_status(request: web.Request) -> web.Response:
    cfg: Config = request.app["cfg"]
    conn: sqlite3.Connection = request.app["conn"]
    cov = db.coverage(conn)
    last = db.last_sync(conn)
    payload = {
        "coverage": cov,
        "last_sync": last,
        "host": cfg.web_host,
        "port": cfg.web_port,
        "account_count": db.account_count(conn),
    }
    payload.update(_source_payload(cfg, conn))
    return _json(payload)


async def handle_sync(request: web.Request) -> web.Response:
    cfg: Config = request.app["cfg"]
    conn: sqlite3.Connection = request.app["conn"]
    if not cfg.simplefin_ready:
        return _json({
            "error": "SimpleFIN is not configured",
            "setup": True,
            "source": "simplefin",
        }, status=503)
    try:
        async with request.app["sync_lock"]:
            result = await run_live_sync(cfg, conn)
        return _json(result)
    except simplefin.SimplefinError as exc:
        return _json({"error": str(exc), "code": exc.code, "source": "simplefin"}, status=400)


async def handle_simplefin_claim(request: web.Request) -> web.Response:
    cfg: Config = request.app["cfg"]
    conn: sqlite3.Connection = request.app["conn"]
    body = await read_json(request)
    token = str(body.get("token") or "").strip()
    if not token:
        return _json({"error": "Paste a SimpleFIN setup token.", "setup": True}, status=400)
    try:
        access_url = await simplefin.claim_setup_token(token)
    except simplefin.SimplefinError as exc:
        payload = {"error": str(exc), "code": exc.code, "ok": False}
        if exc.status == 403 or exc.code == "claim_used":
            payload["compromised"] = True
            payload["error"] = (
                "This setup token was already used or is invalid. "
                "Revoke it on SimpleFIN Bridge and create a new one."
            )
        return _json(payload, status=400)
    simplefin.store_access_url(cfg, access_url)
    try:
        async with request.app["sync_lock"]:
            result = await run_live_sync(cfg, conn)
        return _json({
            "ok": bool(result.get("ok")),
            "n_added": result.get("n_added", 0),
            "n_modified": result.get("n_modified", 0),
            "n_removed": result.get("n_removed", 0),
            "errors": result.get("errors") or [],
            "errlist": result.get("errlist") or [],
            "coverage": result.get("coverage"),
            "n_accounts": result.get("n_accounts"),
            "source": "simplefin",
            "simplefin_ready": True,
        })
    except simplefin.SimplefinError as exc:
        return _json({
            "ok": False,
            "error": str(exc),
            "code": exc.code,
            "simplefin_ready": True,
            "source": "simplefin",
        }, status=400)


async def handle_overview(request: web.Request) -> web.Response:
    conn: sqlite3.Connection = request.app["conn"]
    cfg: Config = request.app["cfg"]
    today = date.today()
    this_start = today.replace(day=1)
    last_end = this_start
    last_start = (this_start - timedelta(days=1)).replace(day=1)
    nw = db.net_worth(conn)
    this_cents = db.spending_in_range(conn, this_start.isoformat(), _next_month(this_start))
    last_cents = db.spending_in_range(conn, last_start.isoformat(), last_end.isoformat())
    upcoming = [
        r for r in db.list_recurring(conn)
        if r.get("next_date") and r["next_date"] >= today.isoformat()
    ][:8]
    return _json({
        "net_worth": nw,
        "this_month_spending_cents": this_cents,
        "last_month_spending_cents": last_cents,
        "this_month": this_start.strftime("%Y-%m"),
        "last_month": last_start.strftime("%Y-%m"),
        "upcoming": upcoming,
        "account_count": db.account_count(conn),
        "simplefin_ready": cfg.simplefin_ready,
    })


def _next_month(d: date) -> str:
    if d.month == 12:
        return date(d.year + 1, 1, 1).isoformat()
    return date(d.year, d.month + 1, 1).isoformat()


def _as_bool(val) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "on")
    return bool(val)


def _dollars_to_cents(val) -> int:
    """Dollars float/string → cents int. Empty/null → 0."""
    if val is None or val == "":
        return 0
    return db.dollars_to_cents(val)


def _cents_field(body: dict, dollars_key: str, cents_key: str) -> int | None:
    """Parse dollars or *_cents if either key is present; empty/null → 0."""
    if not isinstance(body, dict):
        return None
    if cents_key in body and body[cents_key] not in (None, ""):
        try:
            return int(round(float(body[cents_key])))
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid amount") from exc
    if dollars_key in body:
        return _dollars_to_cents(body[dollars_key])
    if cents_key in body:
        return 0
    return None


def _rate_bps_field(body: dict, *, required: bool = False) -> int | None:
    """rate percent (6.25) → bps via round(pct*100), or accept rate_bps."""
    if not isinstance(body, dict):
        body = {}
    has = body.get("rate_bps") not in (None, "") or body.get("rate") not in (None, "")
    if has:
        return db.parse_rate_bps(body)
    if required:
        raise ValueError("rate is required")
    return None


async def handle_accounts(request: web.Request) -> web.Response:
    conn: sqlite3.Connection = request.app["conn"]
    hh = db.household(conn)
    return _json({
        "accounts": db.list_accounts(conn),
        "owners": db.list_owners(conn),
        "categories": db.list_account_categories(conn),
        "net_worth": db.net_worth(conn),
        "net_worth_history": db.net_worth_history(conn),
        "household": {
            "self": hh["self"],
            "partner": hh["partner"],
            "kids": hh["kids"],
            "joint_label": hh["joint_label"],
        },
        "properties": db.list_properties(conn),
    })


async def handle_account_update(request: web.Request) -> web.Response:
    conn: sqlite3.Connection = request.app["conn"]
    account_id = request.match_info["account_id"]
    body = await read_json(request)
    try:
        if db.is_manual_account_id(account_id):
            loan_kw = {}
            if "name" in body:
                loan_kw["name"] = body.get("name")
            owed = _cents_field(body, "balance", "owed_cents")
            if owed is None:
                owed = _cents_field(body, "balance", "balance_cents")
            if owed is not None:
                loan_kw["owed_cents"] = owed
            original = _cents_field(body, "original", "original_cents")
            if original is not None:
                loan_kw["original_cents"] = original
            if "originated_on" in body:
                loan_kw["originated_on"] = body.get("originated_on")
            rate_bps = _rate_bps_field(body)
            if rate_bps is not None:
                loan_kw["rate_bps"] = rate_bps
            if "term_years" in body:
                loan_kw["term_years"] = body.get("term_years")
            if "property_id" in body:
                loan_kw["property_id"] = body.get("property_id")
            if loan_kw:
                db.update_manual_loan(conn, account_id, **loan_kw)
        kwargs = {}
        if "nickname" in body:
            kwargs["nickname"] = body.get("nickname")
        if "owner" in body:
            kwargs["owner"] = body.get("owner")
        if "hidden" in body:
            kwargs["hidden"] = _as_bool(body.get("hidden"))
        if "visibility" in body:
            kwargs["visibility"] = body.get("visibility")
        if "on_blueprint" in body:
            kwargs["on_blueprint"] = _as_bool(body.get("on_blueprint"))
        if "category" in body:
            kwargs["category"] = body.get("category")
        if "property_id" in body:
            kwargs["property_id"] = body.get("property_id")
        if kwargs:
            acct = db.update_account_prefs(conn, account_id, **kwargs)
            if acct is None:
                return _json({"error": "account not found"}, status=404)
            return _json(acct)
        acct = db.get_account(conn, account_id)
        if acct is None:
            return _json({"error": "account not found"}, status=404)
        return _json(acct)
    except KeyError:
        return _json({"error": "account not found"}, status=404)
    except ValueError as exc:
        return _json({"error": str(exc)}, status=400)


async def handle_account_delete(request: web.Request) -> web.Response:
    conn: sqlite3.Connection = request.app["conn"]
    account_id = request.match_info["account_id"]
    try:
        db.delete_manual_account(conn, account_id)
    except KeyError:
        return _json({"error": "account not found"}, status=404)
    except ValueError as exc:
        return _json({"error": str(exc)}, status=400)
    return _json({"ok": True})


async def handle_manual_account_create(request: web.Request) -> web.Response:
    conn: sqlite3.Connection = request.app["conn"]
    body = await read_json(request)
    kind = str(body.get("kind") or "").strip().lower()
    if kind != "loan":
        return _json({"error": "kind must be loan"}, status=400)
    try:
        owed = _cents_field(body, "balance", "owed_cents")
        if owed is None:
            owed = _cents_field(body, "balance", "balance_cents")
        original = _cents_field(body, "original", "original_cents")
        acct = db.create_manual_loan(
            conn,
            name=str(body.get("name") or ""),
            category=str(body.get("category") or ""),
            owed_cents=0 if owed is None else owed,
            original_cents=0 if original is None else original,
            originated_on=str(body.get("originated_on") or ""),
            rate_bps=_rate_bps_field(body, required=True),
            term_years=body.get("term_years"),
            owner=body.get("owner"),
            property_id=body.get("property_id"),
        )
    except KeyError:
        return _json({"error": "account not found"}, status=404)
    except ValueError as exc:
        return _json({"error": str(exc)}, status=400)
    return _json(acct)


async def handle_properties(request: web.Request) -> web.Response:
    conn: sqlite3.Connection = request.app["conn"]
    return _json({
        "properties": db.list_properties(conn),
        "kinds": list(db.PROPERTY_KINDS),
    })


async def handle_property_create(request: web.Request) -> web.Response:
    conn: sqlite3.Connection = request.app["conn"]
    body = await read_json(request)
    try:
        value_cents = _cents_field(body, "value", "value_cents")
        tax_cents = _cents_field(body, "tax", "tax_cents")
        insurance_cents = _cents_field(body, "insurance", "insurance_cents")
        hoa_cents = _cents_field(body, "hoa", "hoa_cents")
        kwargs = {
            "name": str(body.get("name") or ""),
            "kind": str(body.get("kind") or ""),
            "value_cents": 0 if value_cents is None else value_cents,
        }
        if "owner" in body:
            kwargs["owner"] = body.get("owner")
        if tax_cents is not None:
            kwargs["tax_cents"] = tax_cents
        if insurance_cents is not None:
            kwargs["insurance_cents"] = insurance_cents
        if hoa_cents is not None:
            kwargs["hoa_cents"] = hoa_cents
        if "hidden" in body:
            kwargs["hidden"] = _as_bool(body.get("hidden"))
        if "on_blueprint" in body:
            kwargs["on_blueprint"] = _as_bool(body.get("on_blueprint"))
        prop = db.create_property(conn, **kwargs)
    except KeyError:
        return _json({"error": "property not found"}, status=404)
    except ValueError as exc:
        return _json({"error": str(exc)}, status=400)
    return _json(prop)


async def handle_property_update(request: web.Request) -> web.Response:
    conn: sqlite3.Connection = request.app["conn"]
    property_id = request.match_info["property_id"]
    body = await read_json(request)
    kwargs = {}
    if "name" in body:
        kwargs["name"] = body.get("name")
    if "kind" in body:
        kwargs["kind"] = body.get("kind")
    value_cents = _cents_field(body, "value", "value_cents")
    if value_cents is not None:
        kwargs["value_cents"] = value_cents
    if "owner" in body:
        kwargs["owner"] = body.get("owner")
    tax_cents = _cents_field(body, "tax", "tax_cents")
    if tax_cents is not None:
        kwargs["tax_cents"] = tax_cents
    insurance_cents = _cents_field(body, "insurance", "insurance_cents")
    if insurance_cents is not None:
        kwargs["insurance_cents"] = insurance_cents
    hoa_cents = _cents_field(body, "hoa", "hoa_cents")
    if hoa_cents is not None:
        kwargs["hoa_cents"] = hoa_cents
    if "hidden" in body:
        kwargs["hidden"] = _as_bool(body.get("hidden"))
    if "on_blueprint" in body:
        kwargs["on_blueprint"] = _as_bool(body.get("on_blueprint"))
    try:
        prop = db.update_property(conn, property_id, **kwargs)
    except KeyError:
        return _json({"error": "property not found"}, status=404)
    except ValueError as exc:
        return _json({"error": str(exc)}, status=400)
    return _json(prop)


async def handle_property_delete(request: web.Request) -> web.Response:
    conn: sqlite3.Connection = request.app["conn"]
    property_id = request.match_info["property_id"]
    try:
        db.delete_property(conn, property_id)
    except KeyError:
        return _json({"error": "property not found"}, status=404)
    except ValueError as exc:
        return _json({"error": str(exc)}, status=400)
    return _json({"ok": True})


async def handle_transactions(request: web.Request) -> web.Response:
    conn: sqlite3.Connection = request.app["conn"]
    window = request.query.get("window", "30d")
    if window not in ("30d", "90d", "12m", "all"):
        window = "30d"
    q = request.query.get("q", "").strip()
    category = request.query.get("category", "").strip()
    start = request.query.get("start", "").strip()
    end = request.query.get("end", "").strip()
    include_investments = request.query.get("include_investments", "").strip().lower() in (
        "1", "true", "yes",
    )
    spending_only = request.query.get("spending_only", "").strip().lower() in (
        "1", "true", "yes",
    )
    txns = db.list_transactions(
        conn, window=window, q=q, category=category,
        include_investments=include_investments,
        start=start or None, end=end or None,
        spending_only=spending_only,
    )
    return _json({
        "transactions": txns,
        "window": window,
        "coverage": db.coverage(conn),
        "q": q,
        "category": category,
        "include_investments": include_investments,
        "categories": db.list_categories_used(conn),
    })


async def handle_categories_get(request: web.Request) -> web.Response:
    from . import categories as cats
    conn: sqlite3.Connection = request.app["conn"]
    all_flag = request.query.get("all", "").strip() in ("1", "true", "yes")
    return _json(cats.payload(conn, uncategorized_only=not all_flag))


async def handle_categories_apply(request: web.Request) -> web.Response:
    from . import categories as cats
    conn: sqlite3.Connection = request.app["conn"]
    body = await read_json(request)
    try:
        result = cats.apply_to_matching(
            conn,
            category=str(body.get("category") or ""),
            cluster_key=(str(body["cluster_key"]) if body.get("cluster_key") else None),
            pattern=(str(body["pattern"]) if body.get("pattern") else None),
        )
    except ValueError as exc:
        return _json({"error": str(exc)}, status=400)
    cats.apply_to_uncategorized(conn)
    return _json(result)



async def handle_categories_rule_update(request: web.Request) -> web.Response:
    from . import categories as cats
    conn: sqlite3.Connection = request.app["conn"]
    rule_id = request.match_info["rule_id"]
    body = await read_json(request)
    kw = {}
    if "category" in body:
        kw["category"] = str(body.get("category") or "")
    if "pattern" in body:
        kw["pattern"] = str(body.get("pattern") or "")
    try:
        result = cats.update_rule(conn, rule_id, **kw)
    except ValueError as exc:
        return _json({"error": str(exc)}, status=400)
    except KeyError:
        return _json({"error": "rule not found"}, status=404)
    return _json(result)


async def handle_categories_suggest_ignore(request: web.Request) -> web.Response:
    from . import categories as cats
    conn: sqlite3.Connection = request.app["conn"]
    body = await read_json(request)
    try:
        result = cats.ignore_suggestion(conn, str(body.get("id") or ""))
    except ValueError as exc:
        return _json({"error": str(exc)}, status=400)
    return _json(result)


async def handle_categories_suggest_apply(request: web.Request) -> web.Response:
    from . import categories as cats
    conn: sqlite3.Connection = request.app["conn"]
    body = await read_json(request)
    drops = body.get("drop_patterns")
    try:
        result = cats.apply_suggestion(
            conn,
            suggestion_id=(str(body["id"]) if body.get("id") else None),
            action=(str(body["action"]) if body.get("action") else None),
            pattern=(str(body["pattern"]) if body.get("pattern") else None),
            new_pattern=(str(body["new_pattern"]) if body.get("new_pattern") else None),
            category=(str(body["category"]) if body.get("category") else None),
            drop_patterns=drops if isinstance(drops, list) else drops,
        )
    except ValueError as exc:
        return _json({"error": str(exc)}, status=400)
    except KeyError:
        return _json({"error": "suggestion not found"}, status=404)
    return _json(result)


async def handle_categories_tag(request: web.Request) -> web.Response:
    from . import categories as cats
    conn: sqlite3.Connection = request.app["conn"]
    body = await read_json(request)
    try:
        result = cats.tag_one(
            conn,
            str(body.get("transaction_id") or ""),
            str(body.get("category") or ""),
        )
    except ValueError as exc:
        return _json({"error": str(exc)}, status=400)
    except KeyError:
        return _json({"error": "transaction not found"}, status=404)
    return _json(result)


async def handle_spending(request: web.Request) -> web.Response:
    conn: sqlite3.Connection = request.app["conn"]
    period = (request.query.get("period") or "this_month").strip().lower()
    start = (request.query.get("start") or "").strip() or None
    end = (request.query.get("end") or "").strip() or None
    try:
        payload = db.spending_report(conn, period=period, start=start, end=end)
    except ValueError as exc:
        return _json({"error": str(exc)}, status=400)
    payload["coverage"] = db.coverage(conn)
    return _json(payload)


async def handle_recurring(request: web.Request) -> web.Response:
    conn: sqlite3.Connection = request.app["conn"]
    return _json({"recurring": db.list_recurring(conn)})


async def handle_recurring_mark(request: web.Request) -> web.Response:
    conn: sqlite3.Connection = request.app["conn"]
    stream_id = request.match_info["stream_id"]
    body = await read_json(request)
    flag = bool(body.get("is_subscription", True))
    db.set_subscription(conn, stream_id, flag)
    return _json({"ok": True, "stream_id": stream_id, "is_subscription": flag})


async def handle_recurring_ignore(request: web.Request) -> web.Response:
    conn: sqlite3.Connection = request.app["conn"]
    stream_id = request.match_info["stream_id"]
    try:
        result = db.ignore_recurring(conn, stream_id)
    except ValueError as exc:
        return _json({"error": str(exc)}, status=400)
    except KeyError:
        return _json({"error": "recurring stream not found"}, status=404)
    return _json(result)


async def handle_budgets_get(request: web.Request) -> web.Response:
    conn: sqlite3.Connection = request.app["conn"]
    cfg: Config = request.app["cfg"]
    ym = db.get_meta(conn, "budget_month") or date.today().strftime("%Y-%m")
    return _json({
        "month": ym,
        "budgets": [],
        "readonly": True,
        "source": "simplefin",
    })



def _person_id(request: web.Request) -> int:
    raw = request.match_info.get("person_id") or ""
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ValueError("person not found") from None


async def handle_household(request: web.Request) -> web.Response:
    conn: sqlite3.Connection = request.app["conn"]
    return _json(db.household(conn))


async def handle_person_add(request: web.Request) -> web.Response:
    conn: sqlite3.Connection = request.app["conn"]
    body = await read_json(request)
    try:
        hh = db.add_person(
            conn,
            str(body.get("name") or ""),
            str(body.get("role") or ""),
        )
    except ValueError as exc:
        return _json({"error": str(exc)}, status=400)
    return _json(hh)


async def handle_person_update(request: web.Request) -> web.Response:
    conn: sqlite3.Connection = request.app["conn"]
    body = await read_json(request)
    try:
        pid = _person_id(request)
        hh = db.update_person(conn, pid, str(body.get("name") or ""))
    except ValueError as exc:
        msg = str(exc)
        status = 404 if msg == "person not found" else 400
        return _json({"error": msg}, status=status)
    except KeyError:
        return _json({"error": "person not found"}, status=404)
    return _json(hh)


async def handle_person_delete(request: web.Request) -> web.Response:
    conn: sqlite3.Connection = request.app["conn"]
    try:
        pid = _person_id(request)
        hh = db.delete_person(conn, pid)
    except ValueError as exc:
        msg = str(exc)
        status = 404 if msg == "person not found" else 400
        return _json({"error": msg}, status=status)
    except KeyError:
        return _json({"error": "person not found"}, status=404)
    return _json(hh)


def build_app(cfg: Config, conn: sqlite3.Connection) -> web.Application:
    app = web.Application()
    app["cfg"] = cfg
    app["conn"] = conn
    app["sync_lock"] = asyncio.Lock()

    app.on_response_prepare.append(revalidate)
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/status", handle_status)
    app.router.add_post("/api/sync", handle_sync)
    app.router.add_post("/api/simplefin/claim", handle_simplefin_claim)
    app.router.add_get("/api/overview", handle_overview)
    app.router.add_get("/api/accounts", handle_accounts)
    app.router.add_get("/api/properties", handle_properties)
    app.router.add_post("/api/properties", handle_property_create)
    app.router.add_patch("/api/properties/{property_id:.+}", handle_property_update)
    app.router.add_delete("/api/properties/{property_id:.+}", handle_property_delete)
    app.router.add_get("/api/household", handle_household)
    app.router.add_post("/api/household/people", handle_person_add)
    app.router.add_patch("/api/household/people/{person_id}", handle_person_update)
    app.router.add_delete("/api/household/people/{person_id}", handle_person_delete)
    app.router.add_post("/api/accounts/manual", handle_manual_account_create)
    app.router.add_patch("/api/accounts/{account_id:.+}", handle_account_update)
    app.router.add_post("/api/accounts/{account_id:.+}", handle_account_update)
    app.router.add_delete("/api/accounts/{account_id:.+}", handle_account_delete)
    app.router.add_get("/api/transactions", handle_transactions)
    app.router.add_get("/api/spending", handle_spending)
    app.router.add_get("/api/categories", handle_categories_get)
    app.router.add_post("/api/categories/apply", handle_categories_apply)
    app.router.add_post("/api/categories/tag", handle_categories_tag)
    app.router.add_post("/api/categories/suggest/apply", handle_categories_suggest_apply)
    app.router.add_post("/api/categories/suggest/ignore", handle_categories_suggest_ignore)
    app.router.add_patch("/api/categories/rules/{rule_id:.+}", handle_categories_rule_update)
    app.router.add_post("/api/categories/rules/{rule_id:.+}", handle_categories_rule_update)
    app.router.add_get("/api/recurring", handle_recurring)
    app.router.add_post("/api/recurring/{stream_id:.+}/subscription", handle_recurring_mark)
    app.router.add_post("/api/recurring/{stream_id:.+}/ignore", handle_recurring_ignore)
    app.router.add_get("/api/budgets", handle_budgets_get)
    app.router.add_static("/static/", STATIC_DIR)
    return app
