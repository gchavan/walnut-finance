"""SQLite storage for accounts, transactions, recurring, budgets.

Amount convention
-----------------
This app stores integer cents with this sign:
**positive = money out**, **negative = money in**.

Account balances: credit-card owed is negative. Net worth is the sum of
non-closed account balances.

is_spending is 1 only for posted outflows that are household spend on
cash/cards: uncleared rows are listed but excluded from totals; transfers
(`transfer_account_id`) are listed but not spend; Starting Balance /
reconciliation noise is not spend; brokerage / IRA / Robinhood / Crypto
activity is never counted as spend. Robinhood (any account name) and the
Crypto account are hidden from the default Transactions list; they still
count toward net worth. Pass include_investments=1 to list them.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import uuid
from datetime import date, timedelta
from pathlib import Path

SCHEMA_VERSION = "walnut-1"
LEGACY_SCHEMA_VERSIONS = ("ynab-1",)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key               TEXT PRIMARY KEY,
    value             TEXT
);

CREATE TABLE IF NOT EXISTS accounts (
    account_id        TEXT PRIMARY KEY,
    name              TEXT,
    type              TEXT,
    group_key         TEXT,
    on_budget         INTEGER NOT NULL DEFAULT 1,
    closed            INTEGER NOT NULL DEFAULT 0,
    note              TEXT,
    current_cents     INTEGER,
    cleared_cents     INTEGER,
    iso_currency      TEXT,
    updated_at_ms     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS account_prefs (
    account_id        TEXT PRIMARY KEY,
    nickname          TEXT,
    owner             TEXT,
    hidden            INTEGER NOT NULL DEFAULT 0,
    category          TEXT,
    visibility        TEXT,
    on_blueprint      INTEGER NOT NULL DEFAULT 1,
    property_id       TEXT
);

CREATE TABLE IF NOT EXISTS categories (
    category_id       TEXT PRIMARY KEY,
    group_id          TEXT,
    group_name        TEXT,
    name              TEXT,
    hidden            INTEGER NOT NULL DEFAULT 0,
    deleted           INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS transactions (
    transaction_id          TEXT PRIMARY KEY,
    account_id              TEXT NOT NULL,
    date                    TEXT NOT NULL,
    amount_cents            INTEGER NOT NULL,
    iso_currency            TEXT,
    pending                 INTEGER NOT NULL DEFAULT 0,
    payee                   TEXT,
    memo                    TEXT,
    category_id             TEXT,
    category_name           TEXT,
    transfer_account_id     TEXT,
    is_transfer             INTEGER NOT NULL DEFAULT 0,
    is_spending             INTEGER NOT NULL DEFAULT 0,
    cleared                 TEXT,
    approved                INTEGER NOT NULL DEFAULT 1,
    parent_transaction_id   TEXT,
    first_seen_ms           INTEGER NOT NULL,
    last_seen_ms            INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_txn_date ON transactions(date);
CREATE INDEX IF NOT EXISTS idx_txn_spend ON transactions(is_spending, pending, date);
CREATE INDEX IF NOT EXISTS idx_txn_payee ON transactions(payee);
CREATE INDEX IF NOT EXISTS idx_txn_cat ON transactions(category_name);
CREATE INDEX IF NOT EXISTS idx_txn_acct ON transactions(account_id);

CREATE TABLE IF NOT EXISTS recurring (
    stream_id         TEXT PRIMARY KEY,
    source            TEXT,
    merchant          TEXT,
    average_cents     INTEGER,
    frequency         TEXT,
    is_subscription   INTEGER NOT NULL DEFAULT 0,
    category          TEXT,
    next_date         TEXT,
    status            TEXT,
    last_seen_ms      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS recurring_prefs (
    stream_id         TEXT PRIMARY KEY,
    is_subscription   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS budget_categories (
    category_id           TEXT NOT NULL,
    month                 TEXT NOT NULL,
    name                  TEXT,
    group_name            TEXT,
    hidden                INTEGER NOT NULL DEFAULT 0,
    budgeted_cents        INTEGER NOT NULL DEFAULT 0,
    activity_cents        INTEGER NOT NULL DEFAULT 0,
    remaining_cents       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (category_id, month)
);

CREATE TABLE IF NOT EXISTS syncs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at_ms     INTEGER NOT NULL,
    finished_at_ms    INTEGER,
    n_added           INTEGER,
    n_modified        INTEGER,
    n_removed         INTEGER,
    ok                INTEGER NOT NULL DEFAULT 0,
    error             TEXT,
    min_date          TEXT,
    max_date          TEXT,
    server_knowledge  INTEGER
);

CREATE TABLE IF NOT EXISTS category_rules (
    rule_id           TEXT PRIMARY KEY,
    pattern           TEXT NOT NULL,
    category          TEXT NOT NULL,
    source            TEXT,
    updated_at_ms     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cat_rules_pattern ON category_rules(pattern);

CREATE TABLE IF NOT EXISTS people (
    id                INTEGER PRIMARY KEY,
    name              TEXT NOT NULL,
    role              TEXT NOT NULL CHECK (role IN ('self','partner','kid')),
    sort_order        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS properties (
    property_id       TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    kind              TEXT NOT NULL,
    value_cents       INTEGER NOT NULL DEFAULT 0,
    owner             TEXT,
    tax_cents         INTEGER NOT NULL DEFAULT 0,
    insurance_cents   INTEGER NOT NULL DEFAULT 0,
    hoa_cents         INTEGER NOT NULL DEFAULT 0,
    hidden            INTEGER NOT NULL DEFAULT 0,
    on_blueprint      INTEGER NOT NULL DEFAULT 1,
    updated_at_ms     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS loan_terms (
    account_id        TEXT PRIMARY KEY,
    property_id       TEXT,
    original_cents    INTEGER NOT NULL,
    originated_on     TEXT NOT NULL,
    rate_bps          INTEGER NOT NULL,
    term_months       INTEGER NOT NULL
);
"""

_STARTING_BALANCE_PAYEES = (
    "starting balance",
    "reconciliation balance adjustment",
    "manual balance adjustment",
)

_RETIREMENT_NEEDLES = (
    "401k", "401(k)", "401 (k)", "403b", "403(b)", "ira", "roth",
    "retirement", "tsp", "pension", "hsa", "health savings",
)
_INVEST_NEEDLES = (
    "robinhood", "brokerage", "crypto", "investment", "broker", "stock",
    "etf", "mutual",
)

_SEED_PEOPLE = (
    ("You", "self", 0),
)
KNOWN_ACCOUNT_CATEGORIES = (
    "Checking", "Savings", "Credit card", "Taxable",
    "401(k)", "Roth IRA", "Traditional IRA", "HSA", "Crypto",
    "Mortgage", "Auto loan", "Student loan", "Other",
)
PROPERTY_KINDS = (
    "Primary Home", "Investment Rental", "Vacation Home", "Land",
)
LOAN_CATEGORIES = ("Mortgage", "Auto loan", "Student loan")
_ORIGINATED_ON_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_OWNER_MAX_LEN = 40
_UNSET = object()

_ACCOUNT_SELECT = """
SELECT a.*,
       p.nickname AS nickname,
       COALESCE(NULLIF(TRIM(p.owner), ''), (SELECT name FROM people WHERE role='self' LIMIT 1)) AS owner,
       IFNULL(p.hidden, 0) AS hidden,
       IFNULL(p.on_blueprint, 1) AS on_blueprint,
       COALESCE(NULLIF(TRIM(p.visibility), ''), CASE
         WHEN IFNULL(p.hidden, 0) THEN 'exclude'
         WHEN IFNULL(p.on_blueprint, 1) = 0 THEN 'hide'
         ELSE 'include' END) AS visibility,
       COALESCE(NULLIF(TRIM(p.category), ''), 'Other') AS category,
       COALESCE(NULLIF(TRIM(p.nickname), ''), a.name) AS display_name,
       p.property_id AS property_id
FROM accounts a
LEFT JOIN account_prefs p ON p.account_id = a.account_id
"""

_PREFS_JOIN = "LEFT JOIN account_prefs p ON p.account_id = t.account_id"
_NOT_HIDDEN = "IFNULL(p.hidden, 0) = 0"


def now_ms() -> int:
    return int(time.time() * 1000)


def seed_owner_from_name(name: str | None, conn: sqlite3.Connection | None = None) -> str:
    n = (name or "").lower()
    if conn is not None:
        people = _list_people(conn)
        if people:
            kids = [p for p in people if p["role"] == "kid"]
            kids.sort(key=lambda p: len(p["name"] or ""), reverse=True)
            for p in kids:
                nm = (p["name"] or "").strip()
                if nm and nm.lower() in n:
                    return p["name"]
            partner = next((p for p in people if p["role"] == "partner"), None)
            if partner and partner["name"] and partner["name"].lower() in n:
                return partner["name"]
            self_p = next((p for p in people if p["role"] == "self"), None)
            if "joint" in n:
                return "Joint" if partner else (self_p["name"] if self_p else "")
            if self_p and self_p["name"]:
                return self_p["name"]
    if "joint" in n:
        return "Joint"
    return "You"


def seed_category_from_name(name: str | None, group_key: str | None = None) -> str:
    """Guess Checking / Taxable / 401(k) / Roth IRA / etc. from the bank name."""
    n = (name or "").lower().replace("–", "-").replace("—", "-")
    gk = (group_key or "").strip().lower()
    if "roth" in n:
        return "Roth IRA"
    if "401" in n or "403b" in n or "403(b)" in n:
        return "401(k)"
    if "hsa" in n or "health savings" in n:
        return "HSA"
    if "crypto" in n:
        return "Crypto"
    if "ira" in n or "rollover" in n:
        return "Traditional IRA"
    if gk == "cards" or any(
        k in n for k in ("visa", "sapphire", "mastercard", "amex", "credit card")
    ):
        return "Credit card"
    if "saving" in n or "money market" in n:
        return "Savings"
    if "checking" in n:
        return "Checking"
    if gk == "cash":
        return "Checking"
    if gk == "loans":
        return "Other"
    if gk == "retirement":
        return "Traditional IRA"
    if gk == "investments":
        return "Taxable"
    return "Other"



def _owner_is_kid(conn: sqlite3.Connection | None, owner: str) -> bool:
    want = (owner or "").strip().lower()
    if not want or conn is None:
        return False
    for p in _list_people(conn):
        if p["role"] == "kid" and (p["name"] or "").strip().lower() == want:
            return True
    return False


def seed_hidden_from_name(name: str | None, conn: sqlite3.Connection | None = None) -> int:
    """Kids default Include off. Adult Roths stay included; kid Roths stay hidden."""
    owner = seed_owner_from_name(name, conn)
    if _owner_is_kid(conn, owner):
        return 1
    n = (name or "").lower().replace("–", "-").replace("—", "-")
    if "roth" not in n:
        return 0
    needles = []
    if conn is not None:
        needles = [
            (p["name"] or "").strip().lower()
            for p in _list_people(conn)
            if p["role"] == "kid" and (p["name"] or "").strip()
        ]
    if not needles:
        needles = []
    for needle in needles:
        if needle and needle in n:
            return 1
    return 0


def ensure_account_prefs(
    conn: sqlite3.Connection,
    account_id: str,
    name: str | None,
    group_key: str | None = None,
) -> None:
    hidden = seed_hidden_from_name(name, conn)
    conn.execute(
        """
        INSERT OR IGNORE INTO account_prefs
            (account_id, nickname, owner, hidden, category, visibility, on_blueprint)
        VALUES (?, NULL, ?, ?, ?, ?, 1)
        """,
        (
            account_id,
            seed_owner_from_name(name, conn),
            hidden,
            seed_category_from_name(name, group_key),
            "exclude" if hidden else "include",
        ),
    )


def seed_missing_prefs(conn: sqlite3.Connection) -> None:
    for r in conn.execute("SELECT account_id, name, group_key FROM accounts").fetchall():
        ensure_account_prefs(conn, r["account_id"], r["name"], r["group_key"])
        conn.execute(
            """
            UPDATE account_prefs
            SET category = ?
            WHERE account_id = ? AND (category IS NULL OR TRIM(category) = '')
            """,
            (seed_category_from_name(r["name"], r["group_key"]), r["account_id"]),
        )
        conn.execute(
            """
            UPDATE account_prefs
            SET visibility = CASE WHEN IFNULL(hidden, 0) = 1 THEN 'exclude' ELSE 'include' END
            WHERE account_id = ? AND (visibility IS NULL OR TRIM(visibility) = '')
            """,
            (r["account_id"],),
        )
        if seed_owner_from_name(r["name"], conn) == "Joint":
            self_name = default_owner_name(conn)
            conn.execute(
                """
                UPDATE account_prefs
                SET owner = 'Joint'
                WHERE account_id = ?
                  AND (owner IS NULL OR TRIM(owner) = '' OR owner = ?)
                """,
                (r["account_id"], self_name),
            )


def validate_owner(owner: str) -> str:
    s = (owner or "").strip()
    if not s or len(s) > _OWNER_MAX_LEN or "\n" in s or "\r" in s:
        raise ValueError("owner must be a non-empty short string")
    return s


def validate_category(category: str) -> str:
    s = (category or "").strip()
    if not s or len(s) > _OWNER_MAX_LEN or "\n" in s or "\r" in s:
        raise ValueError("category must be a non-empty short string")
    return s


def validate_visibility(visibility: str) -> str:
    s = (visibility or "").strip().lower()
    if s in ("included", "include"):
        return "include"
    if s in ("excluded", "exclude"):
        return "exclude"
    if s in ("hidden", "hide"):
        return "hide"
    raise ValueError("visibility must be include, exclude, or hide")


def account_group(acct_type: str | None, name: str | None, on_budget: bool) -> str:
    t = (acct_type or "").lower().replace(" ", "").replace("_", "")
    n = (name or "").lower()
    if t in ("creditcard", "lineofcredit"):
        return "cards"
    if any(k in n for k in _RETIREMENT_NEEDLES):
        return "retirement"
    if t in ("otherasset",) or any(k in n for k in _INVEST_NEEDLES):
        return "investments"
    if t in (
        "mortgage", "autoloan", "studentloan", "personalloan",
        "medicaldebt", "otherdebt", "otherliability",
    ):
        return "loans"
    if t in ("checking", "savings", "cash") or on_budget:
        return "cash"
    if not on_budget:
        return "investments"
    return "other"


def is_noise_payee(payee: str | None) -> bool:
    p = (payee or "").strip().lower()
    if not p:
        return False
    if p in _STARTING_BALANCE_PAYEES:
        return True
    if "starting balance" in p:
        return True
    if p.startswith("reconciliation"):
        return True
    return False


def account_is_investment_spend(name: str | None, group_key: str | None) -> bool:
    """True if this account is brokerage/IRA/crypto — not household spend.

    Robinhood (any account whose name contains 'robinhood'), Robinhood Crypto
    (name contains 'crypto'), and anything grouped as investments/retirement
    is_spending=0. Robinhood / Crypto rows are hidden from the default
    Transactions list (include_investments=1 to show them).
    """
    gk = (group_key or "").strip().lower()
    if gk in ("investments", "retirement"):
        return True
    n = (name or "").lower()
    if "robinhood" in n:
        return True
    if "crypto" in n:
        return True
    return False


def is_spending_flag(
    *,
    pending: bool,
    amount_cents: int,
    is_transfer: bool,
    payee: str | None,
    category_name: str | None,
    deleted: bool = False,
    account_name: str | None = None,
    group_key: str | None = None,
) -> int:
    """1 if this posted outflow should count toward spending totals.

    Cash + cards only. Transfers, pending, starting-balance noise, and
    investment/retirement/Robinhood activity are listed but not spend.
    """
    if deleted or pending or is_transfer:
        return 0
    if amount_cents <= 0:
        return 0
    if is_noise_payee(payee):
        return 0
    if "robinhood" in (payee or "").lower():
        return 0
    if account_is_investment_spend(account_name, group_key):
        return 0
    gk = (group_key or "").strip().lower()
    if gk and gk not in ("cash", "cards"):
        return 0
    cat = (category_name or "").strip().lower()
    if "starting balance" in cat:
        return 0
    if cat.startswith("inflow:"):
        return 0
    if cat in ("transfer (not spend)", "transfer"):
        return 0
    return 1


def recompute_spending(conn: sqlite3.Connection) -> int:
    """Recompute is_spending for every stored transaction. Returns rows visited."""
    rows_ = conn.execute(
        """
        SELECT t.transaction_id, t.pending, t.amount_cents, t.is_transfer,
               t.payee, t.category_name, a.name AS account_name, a.group_key
        FROM transactions t
        LEFT JOIN accounts a ON a.account_id = t.account_id
        """
    ).fetchall()
    n = 0
    for r in rows_:
        flag = is_spending_flag(
            pending=bool(r["pending"]),
            amount_cents=int(r["amount_cents"] or 0),
            is_transfer=bool(r["is_transfer"]),
            payee=r["payee"],
            category_name=r["category_name"],
            account_name=r["account_name"],
            group_key=r["group_key"],
        )
        conn.execute(
            "UPDATE transactions SET is_spending = ? WHERE transaction_id = ?",
            (flag, r["transaction_id"]),
        )
        n += 1
    return n


def _chmod_private(path: Path, mode: int = 0o600) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _chmod_private(db_path.parent, 0o700)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    _chmod_private(db_path, 0o600)
    for suffix in ("-wal", "-shm"):
        side = Path(str(db_path) + suffix)
        if side.is_file():
            _chmod_private(side, 0o600)
    return conn


def _migrate_account_prefs(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(account_prefs)")}
    if "category" not in cols:
        conn.execute("ALTER TABLE account_prefs ADD COLUMN category TEXT")
    if "visibility" not in cols:
        conn.execute("ALTER TABLE account_prefs ADD COLUMN visibility TEXT")
        conn.execute(
            """
            UPDATE account_prefs
            SET visibility = CASE WHEN IFNULL(hidden, 0) = 1 THEN 'exclude' ELSE 'include' END
            WHERE visibility IS NULL OR TRIM(visibility) = ''
            """
        )
    cols = {r[1] for r in conn.execute("PRAGMA table_info(account_prefs)")}
    if "on_blueprint" not in cols:
        conn.execute(
            "ALTER TABLE account_prefs ADD COLUMN on_blueprint INTEGER NOT NULL DEFAULT 1"
        )
        conn.execute(
            """
            UPDATE account_prefs
            SET on_blueprint = 0, hidden = 0
            WHERE visibility = 'hide'
            """
        )
    cols = {r[1] for r in conn.execute("PRAGMA table_info(account_prefs)")}
    if "property_id" not in cols:
        conn.execute("ALTER TABLE account_prefs ADD COLUMN property_id TEXT")


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def init_db(conn: sqlite3.Connection) -> None:
    tables = _table_names(conn)
    ver = None
    if "meta" in tables:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        ver = row[0] if row else None
    if ver is None:
        conn.executescript(SCHEMA)
        set_meta(conn, "schema_version", SCHEMA_VERSION)
    elif ver == SCHEMA_VERSION or ver in LEGACY_SCHEMA_VERSIONS:
        conn.executescript(SCHEMA)
        if ver != SCHEMA_VERSION:
            set_meta(conn, "schema_version", SCHEMA_VERSION)
    else:
        raise RuntimeError(
            f"Walnut database was left untouched: found schema_version {ver!r}, "
            f"expected {SCHEMA_VERSION!r}"
        )
    _ensure_people(conn)
    _migrate_account_prefs(conn)
    seed_missing_prefs(conn)


def wipe_ledger(conn: sqlite3.Connection) -> None:
    """Drop synced ledger rows. Keep schema_version, manual accounts, properties.

    Used on the first successful SimpleFIN sync so leftover imported rows
    do not mix with sfin: ids. Manual loans (`manual:%`), properties, and
    loan_terms are user-entered and survive the wipe.
    """
    schema = get_meta(conn, "schema_version")
    conn.execute("DELETE FROM transactions")
    conn.execute("DELETE FROM recurring")
    conn.execute("DELETE FROM budget_categories")
    conn.execute("DELETE FROM categories")
    conn.execute("DELETE FROM syncs")
    conn.execute("DELETE FROM account_prefs WHERE account_id NOT LIKE 'manual:%'")
    conn.execute("DELETE FROM accounts WHERE account_id NOT LIKE 'manual:%'")
    conn.execute("DELETE FROM meta WHERE key != 'schema_version'")
    if schema:
        set_meta(conn, "schema_version", schema)


def row_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def rows(cur) -> list[dict]:
    return [{k: r[k] for k in r.keys()} for r in cur.fetchall()]


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str | int | None) -> None:
    if value is None:
        conn.execute("DELETE FROM meta WHERE key = ?", (key,))
        return
    conn.execute(
        """
        INSERT INTO meta (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, str(value)),
    )


def get_server_knowledge(conn: sqlite3.Connection, which: str) -> int | None:
    raw = get_meta(conn, f"server_knowledge_{which}")
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def set_server_knowledge(conn: sqlite3.Connection, which: str, value: int | None) -> None:
    if value is None:
        return
    set_meta(conn, f"server_knowledge_{which}", int(value))


# ----- accounts / categories ------------------------------------------------

def upsert_account(conn: sqlite3.Connection, rec: dict) -> None:
    conn.execute(
        """
        INSERT INTO accounts (
            account_id, name, type, group_key, on_budget, closed, note,
            current_cents, cleared_cents, iso_currency, updated_at_ms
        ) VALUES (
            :account_id, :name, :type, :group_key, :on_budget, :closed, :note,
            :current_cents, :cleared_cents, :iso_currency, :updated_at_ms
        )
        ON CONFLICT(account_id) DO UPDATE SET
            name = excluded.name,
            type = excluded.type,
            group_key = excluded.group_key,
            on_budget = excluded.on_budget,
            closed = excluded.closed,
            note = excluded.note,
            current_cents = excluded.current_cents,
            cleared_cents = excluded.cleared_cents,
            iso_currency = excluded.iso_currency,
            updated_at_ms = excluded.updated_at_ms
        """,
        rec,
    )
    ensure_account_prefs(conn, rec["account_id"], rec.get("name"), rec.get("group_key"))


def list_accounts(conn: sqlite3.Connection, *, include_closed: bool = True) -> list[dict]:
    extra = "" if include_closed else "WHERE a.closed = 0"
    out = rows(conn.execute(
        f"{_ACCOUNT_SELECT} {extra} ORDER BY category, display_name"
    ))
    return [_attach_loan(conn, a) for a in out]


def get_account(conn: sqlite3.Connection, account_id: str) -> dict | None:
    found = rows(conn.execute(
        f"{_ACCOUNT_SELECT} WHERE a.account_id = ?",
        (account_id,),
    ))
    if not found:
        return None
    return _attach_loan(conn, found[0])



def is_manual_account_id(account_id: str | None) -> bool:
    return (account_id or "").startswith("manual:")


def validate_property_kind(kind: str) -> str:
    s = (kind or "").strip()
    if s not in PROPERTY_KINDS:
        raise ValueError(
            "kind must be Primary Home, Investment Rental, Vacation Home, or Land"
        )
    return s


def validate_loan_category(category: str) -> str:
    s = (category or "").strip()
    if s not in LOAN_CATEGORIES:
        raise ValueError("category must be Mortgage, Auto loan, or Student loan")
    return s


def validate_originated_on(value: str) -> str:
    s = (value or "").strip()
    if not _ORIGINATED_ON_RE.match(s):
        raise ValueError("originated_on must be YYYY-MM")
    return s


def validate_rate_bps(rate_bps: int) -> int:
    bps = int(rate_bps)
    if bps < 0 or bps > 3000:
        raise ValueError("rate must be between 0 and 30%")
    return bps


def validate_term_years(term_years) -> int:
    try:
        years = int(term_years)
    except (TypeError, ValueError) as exc:
        raise ValueError("term_years must be 1 to 40") from exc
    if years < 1 or years > 40:
        raise ValueError("term_years must be 1 to 40")
    return years


def dollars_to_cents(val) -> int:
    if val is None or val == "":
        raise ValueError("amount is required")
    if isinstance(val, bool):
        raise ValueError("invalid amount")
    if isinstance(val, int):
        return val * 100
    if isinstance(val, float):
        return int(round(val * 100))
    s = str(val).strip().replace("$", "").replace(",", "").replace(" ", "")
    if not s:
        raise ValueError("amount is required")
    try:
        return int(round(float(s) * 100))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid amount") from exc


def body_cents(body: dict, dollars_key: str, cents_key: str, *, required: bool = False, default: int | None = None) -> int | None:
    if not isinstance(body, dict):
        body = {}
    if cents_key in body and body[cents_key] not in (None, ""):
        try:
            return int(round(float(body[cents_key])))
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid amount") from exc
    if dollars_key in body and body[dollars_key] not in (None, ""):
        return dollars_to_cents(body[dollars_key])
    if required:
        raise ValueError(f"{dollars_key} is required")
    return default


def parse_rate_bps(body: dict) -> int:
    if not isinstance(body, dict):
        body = {}
    if body.get("rate_bps") not in (None, ""):
        try:
            return validate_rate_bps(int(round(float(body["rate_bps"]))))
        except (TypeError, ValueError) as exc:
            raise ValueError("rate must be between 0 and 30%") from exc
    if body.get("rate") not in (None, ""):
        try:
            pct = float(str(body["rate"]).strip().replace("%", "").replace(",", ""))
        except (TypeError, ValueError) as exc:
            raise ValueError("rate must be between 0 and 30%") from exc
        return validate_rate_bps(int(round(pct * 100)))
    raise ValueError("rate is required")


def amortizing_payment_cents(original_cents: int, rate_bps: int, term_months: int) -> int:
    n = int(term_months)
    p = int(original_cents)
    if n <= 0:
        return 0
    r = (rate_bps / 10000.0) / 12.0
    if r == 0:
        return int(round(p / n))
    one = (1 + r) ** n
    denom = one - 1
    if denom == 0:
        return int(round(p / n))
    return int(round(p * r * one / denom))


def remaining_loan_months(originated_on: str, term_months: int, today: date | None = None) -> int:
    today = today or date.today()
    try:
        y, m = originated_on.split("-")
        start_y, start_m = int(y), int(m)
    except (TypeError, ValueError):
        return max(0, int(term_months))
    elapsed = (today.year - start_y) * 12 + (today.month - start_m)
    elapsed = max(0, elapsed)
    return max(0, int(term_months) - elapsed)


def loan_math(
    *,
    original_cents: int,
    current_cents: int | None,
    rate_bps: int,
    term_months: int,
    originated_on: str,
    category: str | None = None,
    tax_cents: int = 0,
    insurance_cents: int = 0,
    hoa_cents: int = 0,
) -> dict:
    payment = amortizing_payment_cents(original_cents, rate_bps, term_months)
    r = (rate_bps / 10000.0) / 12.0
    interest = int(round(abs(int(current_cents or 0)) * r))
    principal = max(0, payment - interest)
    is_mortgage = (category or "").strip() == "Mortgage"
    extra = (int(tax_cents or 0) + int(insurance_cents or 0) + int(hoa_cents or 0)) if is_mortgage else 0
    piti = payment + extra
    return {
        "payment_cents": payment,
        "interest_cents": interest,
        "principal_cents": principal,
        "piti_cents": piti,
        "remaining_months": remaining_loan_months(originated_on, term_months),
    }


def _property_row(conn: sqlite3.Connection, property_id: str | None) -> dict | None:
    if not property_id:
        return None
    found = rows(conn.execute(
        """
        SELECT property_id, name, kind, value_cents, owner, tax_cents,
               insurance_cents, hoa_cents, hidden, on_blueprint, updated_at_ms
        FROM properties WHERE property_id = ?
        """,
        (property_id,),
    ))
    return found[0] if found else None


def _loan_view(conn: sqlite3.Connection, account: dict) -> dict | None:
    aid = account.get("account_id")
    if not aid:
        return None
    row = conn.execute(
        """
        SELECT account_id, property_id, original_cents, originated_on,
               rate_bps, term_months
        FROM loan_terms WHERE account_id = ?
        """,
        (aid,),
    ).fetchone()
    if row is None:
        return None
    prop = _property_row(conn, row["property_id"])
    tax = int(prop["tax_cents"]) if prop else 0
    ins = int(prop["insurance_cents"]) if prop else 0
    hoa = int(prop["hoa_cents"]) if prop else 0
    math = loan_math(
        original_cents=int(row["original_cents"]),
        current_cents=account.get("current_cents"),
        rate_bps=int(row["rate_bps"]),
        term_months=int(row["term_months"]),
        originated_on=row["originated_on"],
        category=account.get("category"),
        tax_cents=tax,
        insurance_cents=ins,
        hoa_cents=hoa,
    )
    bps = int(row["rate_bps"])
    months = int(row["term_months"])
    return {
        "original": int(row["original_cents"]),
        "originated_on": row["originated_on"],
        "rate_bps": bps,
        "rate_pct": bps / 100.0,
        "term_years": months // 12,
        "payment_cents": math["payment_cents"],
        "interest_cents": math["interest_cents"],
        "principal_cents": math["principal_cents"],
        "piti_cents": math["piti_cents"],
        "tax_cents": tax,
        "insurance_cents": ins,
        "hoa_cents": hoa,
        "property_id": row["property_id"],
        "property_name": (prop["name"] if prop else None),
        "remaining_months": math["remaining_months"],
    }


def _attach_loan(conn: sqlite3.Connection, account: dict) -> dict:
    loan = _loan_view(conn, account)
    if loan is not None:
        account["loan"] = loan
    return account


def list_properties(conn: sqlite3.Connection) -> list[dict]:
    try:
        out = rows(conn.execute(
            """
            SELECT property_id, name, kind, value_cents,
                   COALESCE(NULLIF(TRIM(owner), ''),
                            (SELECT name FROM people WHERE role='self' LIMIT 1)) AS owner,
                   tax_cents, insurance_cents, hoa_cents,
                   IFNULL(hidden, 0) AS hidden,
                   IFNULL(on_blueprint, 1) AS on_blueprint,
                   updated_at_ms
            FROM properties
            ORDER BY kind, name
            """
        ))
    except sqlite3.OperationalError:
        return []
    return out


def get_property(conn: sqlite3.Connection, property_id: str) -> dict | None:
    found = [p for p in list_properties(conn) if p["property_id"] == property_id]
    return found[0] if found else None


def _default_hidden_for_owner(conn: sqlite3.Connection, owner: str | None) -> int:
    if _owner_is_kid(conn, owner or ""):
        return 1
    return 0


def create_property(
    conn: sqlite3.Connection,
    *,
    name: str,
    kind: str,
    value_cents: int,
    owner: str | None = None,
    tax_cents: int = 0,
    insurance_cents: int = 0,
    hoa_cents: int = 0,
    hidden=_UNSET,
    on_blueprint=_UNSET,
) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("name is required")
    kind = validate_property_kind(kind)
    value_cents = int(value_cents)
    if value_cents < 0:
        raise ValueError("value cannot be negative")
    tax_cents = int(tax_cents or 0)
    insurance_cents = int(insurance_cents or 0)
    hoa_cents = int(hoa_cents or 0)
    if tax_cents < 0 or insurance_cents < 0 or hoa_cents < 0:
        raise ValueError("monthly amounts cannot be negative")
    owner_s = validate_owner(owner) if owner not in (None, "") else default_owner_name(conn)
    hid = 0
    on_bp = 1
    pid = "prop:" + str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO properties (
            property_id, name, kind, value_cents, owner,
            tax_cents, insurance_cents, hoa_cents, hidden, on_blueprint, updated_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pid, name, kind, value_cents, owner_s,
            tax_cents, insurance_cents, hoa_cents, hid, on_bp, now_ms(),
        ),
    )
    return get_property(conn, pid)


def update_property(
    conn: sqlite3.Connection,
    property_id: str,
    *,
    name=_UNSET,
    kind=_UNSET,
    value_cents=_UNSET,
    owner=_UNSET,
    tax_cents=_UNSET,
    insurance_cents=_UNSET,
    hoa_cents=_UNSET,
    hidden=_UNSET,
    on_blueprint=_UNSET,
) -> dict:
    row = conn.execute(
        "SELECT property_id FROM properties WHERE property_id = ?",
        (property_id,),
    ).fetchone()
    if row is None:
        raise KeyError("property not found")
    sets: list[str] = []
    params: list = []
    if name is not _UNSET:
        s = ("" if name is None else str(name)).strip()
        if not s:
            raise ValueError("name is required")
        sets.append("name = ?")
        params.append(s)
    if kind is not _UNSET:
        sets.append("kind = ?")
        params.append(validate_property_kind(str(kind) if kind is not None else ""))
    if value_cents is not _UNSET:
        v = int(value_cents)
        if v < 0:
            raise ValueError("value cannot be negative")
        sets.append("value_cents = ?")
        params.append(v)
    if owner is not _UNSET:
        new_owner = validate_owner(str(owner) if owner is not None else "")
        sets.append("owner = ?")
        params.append(new_owner)
    if tax_cents is not _UNSET:
        v = int(tax_cents)
        if v < 0:
            raise ValueError("monthly amounts cannot be negative")
        sets.append("tax_cents = ?")
        params.append(v)
    if insurance_cents is not _UNSET:
        v = int(insurance_cents)
        if v < 0:
            raise ValueError("monthly amounts cannot be negative")
        sets.append("insurance_cents = ?")
        params.append(v)
    if hoa_cents is not _UNSET:
        v = int(hoa_cents)
        if v < 0:
            raise ValueError("monthly amounts cannot be negative")
        sets.append("hoa_cents = ?")
        params.append(v)
    if hidden is not _UNSET:
        sets.append("hidden = ?")
        params.append(1 if hidden else 0)
    if on_blueprint is not _UNSET:
        sets.append("on_blueprint = ?")
        params.append(1 if on_blueprint else 0)
    if sets:
        sets.append("updated_at_ms = ?")
        params.append(now_ms())
        params.append(property_id)
        conn.execute(
            f"UPDATE properties SET {', '.join(sets)} WHERE property_id = ?",
            params,
        )
    found = get_property(conn, property_id)
    if found is None:
        raise KeyError("property not found")
    return found


def delete_property(conn: sqlite3.Connection, property_id: str) -> None:
    row = conn.execute(
        "SELECT property_id FROM properties WHERE property_id = ?",
        (property_id,),
    ).fetchone()
    if row is None:
        raise KeyError("property not found")
    conn.execute(
        "UPDATE loan_terms SET property_id = NULL WHERE property_id = ?",
        (property_id,),
    )
    conn.execute(
        "UPDATE account_prefs SET property_id = NULL WHERE property_id = ?",
        (property_id,),
    )
    conn.execute("DELETE FROM properties WHERE property_id = ?", (property_id,))


def _resolve_property_id(conn: sqlite3.Connection, property_id) -> str | None:
    if property_id is None:
        return None
    s = str(property_id).strip()
    if not s:
        return None
    found = conn.execute(
        "SELECT property_id FROM properties WHERE property_id = ?",
        (s,),
    ).fetchone()
    if found is None:
        raise ValueError("property not found")
    return s


def create_manual_loan(
    conn: sqlite3.Connection,
    *,
    name: str,
    category: str,
    owed_cents: int,
    original_cents: int,
    originated_on: str,
    rate_bps: int,
    term_years: int,
    owner: str | None = None,
    property_id: str | None = None,
    hidden=_UNSET,
    on_blueprint=_UNSET,
) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("name is required")
    category = validate_loan_category(category)
    owed_cents = int(owed_cents)
    if owed_cents < 0:
        raise ValueError("balance cannot be negative")
    original_cents = int(original_cents)
    if original_cents < 0:
        raise ValueError("original cannot be negative")
    originated_on = validate_originated_on(originated_on)
    rate_bps = validate_rate_bps(rate_bps)
    years = validate_term_years(term_years)
    owner_s = validate_owner(owner) if owner not in (None, "") else default_owner_name(conn)
    prop_id = _resolve_property_id(conn, property_id)
    hid = _default_hidden_for_owner(conn, owner_s) if hidden is _UNSET else (1 if hidden else 0)
    on_bp = 1 if on_blueprint is _UNSET else (1 if on_blueprint else 0)
    aid = "manual:loan:" + str(uuid.uuid4())
    current = -owed_cents
    ts = now_ms()
    upsert_account(conn, {
        "account_id": aid,
        "name": name,
        "type": "manual_loan",
        "group_key": "loans",
        "on_budget": 0,
        "closed": 0,
        "note": None,
        "current_cents": current,
        "cleared_cents": current,
        "iso_currency": "USD",
        "updated_at_ms": ts,
    })
    update_account_prefs(
        conn, aid,
        owner=owner_s,
        category=category,
        hidden=hid,
        on_blueprint=on_bp,
    )
    conn.execute(
        """
        INSERT INTO loan_terms (
            account_id, property_id, original_cents, originated_on,
            rate_bps, term_months
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (aid, prop_id, original_cents, originated_on, rate_bps, years * 12),
    )
    found = get_account(conn, aid)
    if found is None:
        raise RuntimeError("failed to create loan")
    return found


def update_manual_loan(
    conn: sqlite3.Connection,
    account_id: str,
    *,
    name=_UNSET,
    owed_cents=_UNSET,
    original_cents=_UNSET,
    originated_on=_UNSET,
    rate_bps=_UNSET,
    term_years=_UNSET,
    property_id=_UNSET,
) -> dict:
    if not is_manual_account_id(account_id):
        raise ValueError("only manual accounts can be updated this way")
    row = conn.execute(
        "SELECT account_id, type FROM accounts WHERE account_id = ?",
        (account_id,),
    ).fetchone()
    if row is None:
        raise KeyError("account not found")
    sets_acct: list[str] = []
    params_acct: list = []
    if name is not _UNSET:
        s = ("" if name is None else str(name)).strip()
        if not s:
            raise ValueError("name is required")
        sets_acct.append("name = ?")
        params_acct.append(s)
    if owed_cents is not _UNSET:
        v = int(owed_cents)
        if v < 0:
            raise ValueError("balance cannot be negative")
        sets_acct.append("current_cents = ?")
        params_acct.append(-v)
        sets_acct.append("cleared_cents = ?")
        params_acct.append(-v)
    if sets_acct:
        sets_acct.append("updated_at_ms = ?")
        params_acct.append(now_ms())
        params_acct.append(account_id)
        conn.execute(
            f"UPDATE accounts SET {', '.join(sets_acct)} WHERE account_id = ?",
            params_acct,
        )
    terms = conn.execute(
        "SELECT account_id FROM loan_terms WHERE account_id = ?",
        (account_id,),
    ).fetchone()
    if terms is None and any(
        x is not _UNSET for x in (original_cents, originated_on, rate_bps, term_years, property_id)
    ):
        raise KeyError("loan terms not found")
    if terms is not None:
        sets_t: list[str] = []
        params_t: list = []
        if original_cents is not _UNSET:
            v = int(original_cents)
            if v < 0:
                raise ValueError("original cannot be negative")
            sets_t.append("original_cents = ?")
            params_t.append(v)
        if originated_on is not _UNSET:
            sets_t.append("originated_on = ?")
            params_t.append(validate_originated_on(str(originated_on) if originated_on is not None else ""))
        if rate_bps is not _UNSET:
            sets_t.append("rate_bps = ?")
            params_t.append(validate_rate_bps(rate_bps))
        if term_years is not _UNSET:
            years = validate_term_years(term_years)
            sets_t.append("term_months = ?")
            params_t.append(years * 12)
        if property_id is not _UNSET:
            sets_t.append("property_id = ?")
            params_t.append(_resolve_property_id(conn, property_id))
        if sets_t:
            params_t.append(account_id)
            conn.execute(
                f"UPDATE loan_terms SET {', '.join(sets_t)} WHERE account_id = ?",
                params_t,
            )
    found = get_account(conn, account_id)
    if found is None:
        raise KeyError("account not found")
    return found


def delete_manual_account(conn: sqlite3.Connection, account_id: str) -> None:
    if not is_manual_account_id(account_id):
        raise ValueError("only manual accounts can be deleted")
    row = conn.execute(
        "SELECT account_id FROM accounts WHERE account_id = ?",
        (account_id,),
    ).fetchone()
    if row is None:
        raise KeyError("account not found")
    conn.execute("DELETE FROM loan_terms WHERE account_id = ?", (account_id,))
    conn.execute("DELETE FROM account_prefs WHERE account_id = ?", (account_id,))
    conn.execute("DELETE FROM accounts WHERE account_id = ?", (account_id,))


def account_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute(
        f"""
        SELECT COUNT(*) FROM accounts a
        {_PREFS_JOIN.replace('t.account_id', 'a.account_id')}
        WHERE a.closed = 0 AND {_NOT_HIDDEN}
        """
    ).fetchone()[0])


def _person_dict(row: sqlite3.Row | dict) -> dict:
    return {"id": int(row["id"]), "name": row["name"], "role": row["role"]}


def _list_people(conn: sqlite3.Connection) -> list[dict]:
    try:
        cur = conn.execute(
            """
            SELECT id, name, role, sort_order
            FROM people
            ORDER BY sort_order, id
            """
        )
    except sqlite3.OperationalError:
        return []
    return [_person_dict(r) | {"sort_order": r["sort_order"]} for r in cur.fetchall()]


def _validate_person_name(name: str) -> str:
    s = (name or "").strip()
    if not s or len(s) > _OWNER_MAX_LEN or "\n" in s or "\r" in s:
        raise ValueError("name must be a non-empty short string")
    if s.lower() == "joint":
        raise ValueError("Joint is reserved for shared accounts")
    return s


def _ensure_people(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS people (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('self','partner','kid')),
            sort_order INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    n = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    if n:
        return
    conn.executemany(
        "INSERT INTO people (name, role, sort_order) VALUES (?, ?, ?)",
        _SEED_PEOPLE,
    )


def household(conn: sqlite3.Connection) -> dict:
    people = _list_people(conn)
    self_p = next((p for p in people if p["role"] == "self"), None)
    partner = next((p for p in people if p["role"] == "partner"), None)
    kids = [p for p in people if p["role"] == "kid"]
    self_out = None if self_p is None else {
        "id": self_p["id"], "name": self_p["name"], "role": self_p["role"]
    }
    partner_out = None if partner is None else {
        "id": partner["id"], "name": partner["name"], "role": partner["role"]
    }
    kids_out = [{"id": k["id"], "name": k["name"], "role": k["role"]} for k in kids]
    owners: list[str] = []
    if self_out:
        owners.append(self_out["name"])
    if partner_out:
        owners.append(partner_out["name"])
        owners.append("Joint")
    owners.extend(k["name"] for k in kids_out)
    joint_label = None
    if self_out and partner_out:
        joint_label = f"Joint ({self_out['name']} & {partner_out['name']})"
    return {
        "self": self_out,
        "partner": partner_out,
        "kids": kids_out,
        "owners": owners,
        "joint_label": joint_label,
    }


def default_owner_name(conn: sqlite3.Connection) -> str:
    try:
        row = conn.execute(
            "SELECT name FROM people WHERE role = 'self' LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    return (row["name"] if row and row["name"] else "") or ""


def _name_taken(conn: sqlite3.Connection, name: str, *, except_id: int | None = None) -> bool:
    want = name.lower()
    for p in _list_people(conn):
        if except_id is not None and p["id"] == except_id:
            continue
        if (p["name"] or "").lower() == want:
            return True
    return False


def add_person(conn: sqlite3.Connection, name: str, role: str) -> dict:
    role = (role or "").strip().lower()
    if role not in ("partner", "kid"):
        raise ValueError("role must be partner or kid")
    name = _validate_person_name(name)
    if role == "partner":
        existing = conn.execute(
            "SELECT id FROM people WHERE role = 'partner' LIMIT 1"
        ).fetchone()
        if existing is not None:
            raise ValueError("partner already exists")
    if _name_taken(conn, name):
        raise ValueError("that name is already in the household")
    row = conn.execute(
        "SELECT COALESCE(MAX(sort_order), -1) FROM people"
    ).fetchone()
    sort_order = int(row[0]) + 1
    conn.execute(
        "INSERT INTO people (name, role, sort_order) VALUES (?, ?, ?)",
        (name, role, sort_order),
    )
    return household(conn)


def update_person(conn: sqlite3.Connection, person_id: int, name: str) -> dict:
    row = conn.execute(
        "SELECT id, name, role FROM people WHERE id = ?",
        (person_id,),
    ).fetchone()
    if row is None:
        raise KeyError("person not found")
    name = _validate_person_name(name)
    old = row["name"]
    if name != old:
        if _name_taken(conn, name, except_id=int(row["id"])):
            raise ValueError("that name is already in the household")
        conn.execute("UPDATE people SET name = ? WHERE id = ?", (name, person_id))
        conn.execute(
            "UPDATE account_prefs SET owner = ? WHERE owner = ?",
            (name, old),
        )
        conn.execute(
            "UPDATE properties SET owner = ? WHERE owner = ?",
            (name, old),
        )
    return household(conn)


def delete_person(conn: sqlite3.Connection, person_id: int) -> dict:
    row = conn.execute(
        "SELECT id, name, role FROM people WHERE id = ?",
        (person_id,),
    ).fetchone()
    if row is None:
        raise KeyError("person not found")
    if row["role"] == "self":
        raise ValueError("cannot delete self")
    self_name = default_owner_name(conn)
    conn.execute(
        "UPDATE account_prefs SET owner = ? WHERE owner = ?",
        (self_name, row["name"]),
    )
    conn.execute(
        "UPDATE properties SET owner = ? WHERE owner = ?",
        (self_name, row["name"]),
    )
    if row["role"] == "partner":
        conn.execute(
            "UPDATE account_prefs SET owner = ? WHERE owner = 'Joint'",
            (self_name,),
        )
        conn.execute(
            "UPDATE properties SET owner = ? WHERE owner = 'Joint'",
            (self_name,),
        )
    conn.execute("DELETE FROM people WHERE id = ?", (person_id,))
    return household(conn)


def list_owners(conn: sqlite3.Connection) -> list[str]:
    hh = household(conn)
    out = list(hh["owners"])
    seen = {o.lower() for o in out}
    extra = [
        r[0]
        for r in conn.execute(
            """
            SELECT DISTINCT owner FROM account_prefs
            WHERE owner IS NOT NULL AND TRIM(owner) != ''
            UNION
            SELECT DISTINCT owner FROM properties
            WHERE owner IS NOT NULL AND TRIM(owner) != ''
            ORDER BY owner COLLATE NOCASE
            """
        )
    ]
    for o in extra:
        if o.lower() not in seen:
            seen.add(o.lower())
            out.append(o)
    return out


def list_account_categories(conn: sqlite3.Connection) -> list[str]:
    extra = [
        r[0]
        for r in conn.execute(
            """
            SELECT DISTINCT category FROM account_prefs
            WHERE category IS NOT NULL AND TRIM(category) != ''
            ORDER BY category COLLATE NOCASE
            """
        )
    ]
    seen = {c.lower() for c in KNOWN_ACCOUNT_CATEGORIES}
    out = list(KNOWN_ACCOUNT_CATEGORIES)
    for c in extra:
        if c.lower() not in seen:
            seen.add(c.lower())
            out.append(c)
    return out


def update_account_prefs(
    conn: sqlite3.Connection,
    account_id: str,
    *,
    nickname=_UNSET,
    owner=_UNSET,
    hidden=_UNSET,
    category=_UNSET,
    visibility=_UNSET,
    on_blueprint=_UNSET,
    property_id=_UNSET,
) -> dict | None:
    row = conn.execute(
        "SELECT name, group_key FROM accounts WHERE account_id = ?",
        (account_id,),
    ).fetchone()
    if row is None:
        return None
    ensure_account_prefs(conn, account_id, row["name"], row["group_key"])
    sets: list[str] = []
    params: list = []
    if nickname is not _UNSET:
        nick = None if nickname is None else str(nickname).strip() or None
        sets.append("nickname = ?")
        params.append(nick)
    if owner is not _UNSET:
        new_owner = validate_owner(str(owner) if owner is not None else "")
        sets.append("owner = ?")
        params.append(new_owner)
        if hidden is _UNSET and visibility is _UNSET:
            cur_hidden = conn.execute(
                "SELECT hidden FROM account_prefs WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            currently_included = (
                cur_hidden is None or int(cur_hidden["hidden"] or 0) == 0
            )
            if currently_included and _owner_is_kid(conn, new_owner):
                sets.append("hidden = ?")
                params.append(1)
    if visibility is not _UNSET:
        vis = validate_visibility(str(visibility) if visibility is not None else "")
        if vis == "exclude":
            sets.append("hidden = ?")
            params.append(1)
            sets.append("on_blueprint = ?")
            params.append(1)
        elif vis == "hide":
            sets.append("hidden = ?")
            params.append(0)
            sets.append("on_blueprint = ?")
            params.append(0)
        else:
            sets.append("hidden = ?")
            params.append(0)
            sets.append("on_blueprint = ?")
            params.append(1)
    else:
        if hidden is not _UNSET:
            sets.append("hidden = ?")
            params.append(1 if hidden else 0)
        if on_blueprint is not _UNSET:
            sets.append("on_blueprint = ?")
            params.append(1 if on_blueprint else 0)
    if category is not _UNSET:
        cat = validate_category(str(category) if category is not None else "")
        sets.append("category = ?")
        params.append(cat)
        if cat != "Mortgage" and property_id is _UNSET:
            sets.append("property_id = ?")
            params.append(None)
    if property_id is not _UNSET:
        sets.append("property_id = ?")
        params.append(_resolve_property_id(conn, property_id))
    if sets:
        params.append(account_id)
        conn.execute(
            f"UPDATE account_prefs SET {', '.join(sets)} WHERE account_id = ?",
            params,
        )
        conn.execute(
            """
            UPDATE account_prefs
            SET visibility = CASE
              WHEN IFNULL(hidden, 0) = 1 THEN 'exclude'
              WHEN IFNULL(on_blueprint, 1) = 0 THEN 'hide'
              ELSE 'include' END
            WHERE account_id = ?
            """,
            (account_id,),
        )
    return get_account(conn, account_id)


def upsert_category(conn: sqlite3.Connection, rec: dict) -> None:
    conn.execute(
        """
        INSERT INTO categories (category_id, group_id, group_name, name, hidden, deleted)
        VALUES (:category_id, :group_id, :group_name, :name, :hidden, :deleted)
        ON CONFLICT(category_id) DO UPDATE SET
            group_id = excluded.group_id,
            group_name = excluded.group_name,
            name = excluded.name,
            hidden = excluded.hidden,
            deleted = excluded.deleted
        """,
        rec,
    )


# ----- transactions ---------------------------------------------------------

def upsert_transaction(conn: sqlite3.Connection, rec: dict) -> str:
    """Idempotent on transaction id. Returns 'added' or 'modified'."""
    existing = conn.execute(
        "SELECT first_seen_ms FROM transactions WHERE transaction_id = ?",
        (rec["transaction_id"],),
    ).fetchone()
    if existing:
        rec = dict(rec)
        rec["first_seen_ms"] = existing["first_seen_ms"]
        action = "modified"
    else:
        action = "added"
    conn.execute(
        """
        INSERT INTO transactions (
            transaction_id, account_id, date, amount_cents, iso_currency,
            pending, payee, memo, category_id, category_name,
            transfer_account_id, is_transfer, is_spending, cleared, approved,
            parent_transaction_id, first_seen_ms, last_seen_ms
        ) VALUES (
            :transaction_id, :account_id, :date, :amount_cents, :iso_currency,
            :pending, :payee, :memo, :category_id, :category_name,
            :transfer_account_id, :is_transfer, :is_spending, :cleared, :approved,
            :parent_transaction_id, :first_seen_ms, :last_seen_ms
        )
        ON CONFLICT(transaction_id) DO UPDATE SET
            account_id = excluded.account_id,
            date = excluded.date,
            amount_cents = excluded.amount_cents,
            iso_currency = excluded.iso_currency,
            pending = excluded.pending,
            payee = excluded.payee,
            memo = excluded.memo,
            category_id = excluded.category_id,
            category_name = CASE
                WHEN excluded.category_name IS NOT NULL
                     AND excluded.category_name != ''
                THEN excluded.category_name
                ELSE transactions.category_name
            END,
            transfer_account_id = excluded.transfer_account_id,
            is_transfer = excluded.is_transfer,
            is_spending = excluded.is_spending,
            cleared = excluded.cleared,
            approved = excluded.approved,
            parent_transaction_id = excluded.parent_transaction_id,
            last_seen_ms = excluded.last_seen_ms
        """,
        rec,
    )
    return action


def delete_transaction(conn: sqlite3.Connection, transaction_id: str) -> None:
    conn.execute("DELETE FROM transactions WHERE transaction_id = ?", (transaction_id,))
    conn.execute(
        "DELETE FROM transactions WHERE parent_transaction_id = ?",
        (transaction_id,),
    )


def coverage(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT MIN(date) AS min_date, MAX(date) AS max_date, COUNT(*) AS n FROM transactions"
    ).fetchone()
    posted = conn.execute(
        "SELECT MIN(date) AS min_date, MAX(date) AS max_date, COUNT(*) AS n "
        "FROM transactions WHERE pending = 0"
    ).fetchone()
    n_on = int(conn.execute(
        "SELECT COUNT(*) FROM accounts WHERE closed = 0 AND on_budget = 1"
    ).fetchone()[0])
    n_track = int(conn.execute(
        "SELECT COUNT(*) FROM accounts WHERE closed = 0 AND on_budget = 0"
    ).fetchone()[0])
    first_month = get_meta(conn, "plan_first_month")
    last_month = get_meta(conn, "plan_last_month")
    since = get_meta(conn, "transactions_since")
    source = get_meta(conn, "source") or "simplefin"
    min_date = row["min_date"]
    partial = False
    reason = None
    if source == "simplefin":
        # SimpleFIN is never full history (typical window ~90 days).
        partial = True
        if min_date and row["max_date"]:
            reason = (
                f"SimpleFIN typically returns about 90 days, not full history "
                f"(holding {min_date} → {row['max_date']})"
            )
        else:
            reason = "SimpleFIN typically returns about 90 days, not full history"
    else:
        # Partial only if OUR request floor may have cut off older data.
        if min_date and since and min_date <= since:
            if first_month and first_month < since:
                partial = True
                reason = f"import floor {since} is after plan first_month {first_month}"
            elif not first_month:
                partial = True
                reason = f"oldest stored txn sits on the import floor {since}"
    return {
        "min_date": min_date,
        "max_date": row["max_date"],
        "n": row["n"],
        "posted_min_date": posted["min_date"],
        "posted_max_date": posted["max_date"],
        "posted_n": posted["n"],
        "on_budget_accounts": n_on,
        "tracking_accounts": n_track,
        "plan_first_month": first_month,
        "plan_last_month": last_month,
        "transactions_since": since,
        "partial": partial,
        "partial_reason": reason,
        "plan_name": get_meta(conn, "plan_name"),
        "server_knowledge": get_server_knowledge(conn, "transactions"),
        "source": source,
    }


def window_clause(window: str) -> tuple[str, list]:
    today = date.today()
    if window == "30d":
        return "AND date >= ?", [(today - timedelta(days=30)).isoformat()]
    if window == "90d":
        return "AND date >= ?", [(today - timedelta(days=90)).isoformat()]
    if window == "12m":
        return "AND date >= ?", [(today - timedelta(days=365)).isoformat()]
    return "", []


def _hide_robinhood_sql() -> str:
    """Exclude Robinhood (any name) and the Crypto account from listings."""
    return (
        " AND lower(IFNULL(a.name, '')) NOT LIKE '%robinhood%'"
        " AND lower(IFNULL(a.name, '')) NOT LIKE '%crypto%'"
    )


def list_transactions(
    conn: sqlite3.Connection,
    *,
    window: str = "30d",
    q: str = "",
    category: str = "",
    limit: int = 500,
    include_investments: bool = False,
    start: str | None = None,
    end: str | None = None,
    spending_only: bool = False,
) -> list[dict]:
    start_s = (start or "").strip()[:10]
    end_s = (end or "").strip()[:10]
    if start_s and end_s:
        extra, params = "AND t.date >= ? AND t.date <= ?", [start_s, end_s]
    else:
        extra, params = window_clause(window)
        extra = extra.replace("AND date", "AND t.date")
    if spending_only:
        extra += " AND t.is_spending = 1 AND t.pending = 0"
    hide = f" AND {_NOT_HIDDEN}"
    if not include_investments:
        hide += _hide_robinhood_sql()
    sql = f"""
        SELECT t.*, a.name AS account_name, a.type AS account_type,
               a.on_budget AS account_on_budget, a.group_key AS account_group,
               p.nickname AS nickname, p.owner AS owner,
               IFNULL(p.hidden, 0) AS hidden,
               COALESCE(NULLIF(TRIM(p.nickname), ''), a.name) AS display_name
        FROM transactions t
        LEFT JOIN accounts a ON a.account_id = t.account_id
        {_PREFS_JOIN}
        WHERE 1=1 {extra}{hide}
    """
    if q:
        sql += (
            " AND (t.payee LIKE ? OR t.memo LIKE ? OR t.category_name LIKE ?"
            " OR a.name LIKE ? OR IFNULL(p.nickname, '') LIKE ?)"
        )
        like = f"%{q}%"
        params.extend([like, like, like, like, like])
    if category:
        if category.strip().lower() == "uncategorized":
            sql += " AND (t.category_name IS NULL OR TRIM(t.category_name) = '' OR t.category_name = 'Uncategorized')"
        else:
            sql += " AND t.category_name = ?"
            params.append(category)
    sql += " ORDER BY t.date DESC, t.transaction_id DESC LIMIT ?"
    params.append(limit)
    return rows(conn.execute(sql, params))


def list_categories_used(conn: sqlite3.Connection) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT category_name FROM transactions "
            "WHERE category_name IS NOT NULL AND category_name != '' "
            "ORDER BY 1"
        )
    ]


def spending_by_category(conn: sqlite3.Connection, window: str) -> list[dict]:
    extra, params = window_clause(window)
    return rows(conn.execute(
        f"""
        SELECT COALESCE(NULLIF(t.category_name, ''), 'Uncategorized') AS category,
               SUM(t.amount_cents) AS cents, COUNT(*) AS n
        FROM transactions t
        {_PREFS_JOIN}
        WHERE t.is_spending = 1 AND t.pending = 0 AND {_NOT_HIDDEN} {extra.replace("AND date", "AND t.date")}
        GROUP BY 1
        ORDER BY cents DESC
        """,
        params,
    ))


def spending_by_merchant(conn: sqlite3.Connection, window: str, limit: int = 20) -> list[dict]:
    extra, params = window_clause(window)
    params.append(limit)
    date_extra = extra.replace("AND date", "AND t.date")
    return rows(conn.execute(
        f"""
        SELECT COALESCE(NULLIF(t.payee, ''), 'Unknown') AS merchant,
               SUM(t.amount_cents) AS cents, COUNT(*) AS n
        FROM transactions t
        {_PREFS_JOIN}
        WHERE t.is_spending = 1 AND t.pending = 0 AND {_NOT_HIDDEN} {date_extra}
        GROUP BY 1
        ORDER BY cents DESC
        LIMIT ?
        """,
        params,
    ))


def spending_by_month(conn: sqlite3.Connection, window: str) -> list[dict]:
    extra, params = window_clause(window)
    date_extra = extra.replace("AND date", "AND t.date")
    return rows(conn.execute(
        f"""
        SELECT substr(t.date, 1, 7) AS month,
               SUM(t.amount_cents) AS cents, COUNT(*) AS n
        FROM transactions t
        {_PREFS_JOIN}
        WHERE t.is_spending = 1 AND t.pending = 0 AND {_NOT_HIDDEN} {date_extra}
        GROUP BY 1
        ORDER BY 1
        """,
        params,
    ))


def _next_month(year_month: str) -> str:
    y, m = map(int, year_month.split("-"))
    if m == 12:
        return f"{y + 1:04d}-01-01"
    return f"{y:04d}-{m + 1:02d}-01"


def spending_in_range(conn: sqlite3.Connection, start: str, end: str) -> int:
    row = conn.execute(
        """
        SELECT COALESCE(SUM(t.amount_cents), 0) FROM transactions t
        LEFT JOIN account_prefs p ON p.account_id = t.account_id
        WHERE t.is_spending = 1 AND t.pending = 0 AND t.date >= ? AND t.date < ?
          AND IFNULL(p.hidden, 0) = 0
        """,
        (start, end),
    ).fetchone()
    return int(row[0])


# ----- spending page -------------------------------------------------------

_PAGE_EXCLUDED_CATS = ("tax deductible", "reimbursements")
_TRANSFER_CATS = ("transfer (not spend)", "transfer")
_BILLS_CAT = "Bills & Utilities"
_UNCATEGORIZED = frozenset(("", "uncategorized", "uncategorised"))


def _shift_month(d: date, months: int) -> date:
    """First of the month `months` away from d's month."""
    y = d.year
    m = d.month + months
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return date(y, m, 1)


def _parse_iso_date(raw: str | None) -> date | None:
    if raw is None:
        return None
    s = str(raw).strip()[:10]
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise ValueError(f"invalid date: {raw}") from None


def _month_list_ending(end_month: str, n: int = 6) -> list[str]:
    y, m = map(int, end_month.split("-"))
    out: list[str] = []
    for i in range(n - 1, -1, -1):
        yy = y
        mm = m - i
        while mm <= 0:
            mm += 12
            yy -= 1
        out.append(f"{yy:04d}-{mm:02d}")
    return out


def period_ranges(
    period: str = "this_month",
    start: str | None = None,
    end: str | None = None,
    *,
    today: date | None = None,
) -> dict:
    """Inclusive display dates + exclusive SQL `end` for a spending period.

    period: this_month | last_month | custom
    Custom uses start/end query dates (end inclusive). Previous period is the
    same length immediately before start. Calendar months use the prior month.
    """
    today = today or date.today()
    key = (period or "this_month").strip().lower()
    if key not in ("this_month", "last_month", "custom"):
        key = "this_month"

    if key == "last_month":
        cur_start = _shift_month(today, -1)
        cur_end = _shift_month(today, 0)
    elif key == "custom":
        cur_start = _parse_iso_date(start) or today.replace(day=1)
        end_d = _parse_iso_date(end) or today
        if end_d < cur_start:
            raise ValueError("end must be on or after start")
        cur_end = end_d + timedelta(days=1)
    else:
        key = "this_month"
        cur_start = today.replace(day=1)
        cur_end = _shift_month(cur_start, 1)

    if key == "custom":
        days = (cur_end - cur_start).days
        prev_end = cur_start
        prev_start = prev_end - timedelta(days=days)
    else:
        prev_end = cur_start
        prev_start = _shift_month(cur_start, -1)

    last_day = cur_end - timedelta(days=1)
    return {
        "period": key,
        "start": cur_start.isoformat(),
        "end": last_day.isoformat(),
        "end_exclusive": cur_end.isoformat(),
        "prev_start": prev_start.isoformat(),
        "prev_end_exclusive": prev_end.isoformat(),
        "selected_month": last_day.strftime("%Y-%m"),
    }


def _cat_label(name: str | None) -> str:
    s = (name or "").strip()
    return s if s else "Uncategorized"


def _is_uncategorized(name: str | None) -> bool:
    return (name or "").strip().lower() in _UNCATEGORIZED


def _change_pct(cur: int, prev: int) -> float | None:
    if prev == 0:
        return None
    return round(100.0 * (cur - prev) / prev, 1)


def _sum_one(conn: sqlite3.Connection, sql: str, params: tuple) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0] or 0)


def _sql_in_lower(values: tuple[str, ...]) -> str:
    return ", ".join("'" + v.replace("'", "''") + "'" for v in values)


def _page_spend_filter() -> str:
    """Posted household spend, excluding tax-deductible / reimbursement tags."""
    excluded = _sql_in_lower(_PAGE_EXCLUDED_CATS)
    return f"""
        t.is_spending = 1 AND t.pending = 0
        AND lower(IFNULL(t.category_name, '')) NOT IN ({excluded})
    """


def _spend_by_category_range(conn: sqlite3.Connection, start: str, end: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in conn.execute(
        f"""
        SELECT COALESCE(NULLIF(TRIM(t.category_name), ''), 'Uncategorized') AS category,
               SUM(t.amount_cents) AS cents
        FROM transactions t
        LEFT JOIN account_prefs p ON p.account_id = t.account_id
        WHERE {_page_spend_filter()} AND t.date >= ? AND t.date < ?
          AND IFNULL(p.hidden, 0) = 0
        GROUP BY 1
        """,
        (start, end),
    ):
        out[_cat_label(r["category"])] = int(r["cents"] or 0)
    return out


def _income_in_range(conn: sqlite3.Connection, start: str, end: str) -> int:
    """Cash/card inflows as a positive cents total. Not spend."""
    return _sum_one(
        conn,
        """
        SELECT COALESCE(SUM(-t.amount_cents), 0)
        FROM transactions t
        LEFT JOIN accounts a ON a.account_id = t.account_id
        LEFT JOIN account_prefs p ON p.account_id = t.account_id
        WHERE t.pending = 0 AND t.is_transfer = 0 AND t.amount_cents < 0
          AND t.date >= ? AND t.date < ?
          AND IFNULL(a.group_key, 'cash') IN ('cash', 'cards')
          AND IFNULL(p.hidden, 0) = 0
          AND lower(IFNULL(t.payee, '')) NOT IN (
                'starting balance',
                'reconciliation balance adjustment',
                'manual balance adjustment'
              )
          AND lower(IFNULL(t.payee, '')) NOT LIKE '%starting balance%'
          AND lower(IFNULL(t.payee, '')) NOT LIKE 'reconciliation%'
        """,
        (start, end),
    )


def _transfer_in_range(conn: sqlite3.Connection, start: str, end: str) -> int:
    """Internal transfer outflows (both flagged transfers and tagged)."""
    return _sum_one(
        conn,
        f"""
        SELECT COALESCE(SUM(t.amount_cents), 0)
        FROM transactions t
        LEFT JOIN account_prefs p ON p.account_id = t.account_id
        WHERE t.pending = 0 AND t.amount_cents > 0
          AND t.date >= ? AND t.date < ?
          AND IFNULL(p.hidden, 0) = 0
          AND (
            t.is_transfer = 1
            OR lower(IFNULL(t.category_name, '')) IN ({_sql_in_lower(_TRANSFER_CATS)})
          )
        """,
        (start, end),
    )


def _ignored_in_range(conn: sqlite3.Connection, start: str, end: str) -> int:
    """Investment / Robinhood / retirement outflows — listed, not spend."""
    return _sum_one(
        conn,
        """
        SELECT COALESCE(SUM(t.amount_cents), 0)
        FROM transactions t
        LEFT JOIN accounts a ON a.account_id = t.account_id
        LEFT JOIN account_prefs p ON p.account_id = t.account_id
        WHERE t.pending = 0 AND t.amount_cents > 0 AND t.is_transfer = 0
          AND t.date >= ? AND t.date < ?
          AND IFNULL(p.hidden, 0) = 0
          AND (
            IFNULL(a.group_key, '') IN ('investments', 'retirement')
            OR lower(IFNULL(a.name, '')) LIKE '%robinhood%'
            OR lower(IFNULL(a.name, '')) LIKE '%crypto%'
            OR lower(IFNULL(t.payee, '')) LIKE '%robinhood%'
          )
        """,
        (start, end),
    )


def _tagged_outflow_in_range(conn: sqlite3.Connection, start: str, end: str, category: str) -> int:
    return _sum_one(
        conn,
        """
        SELECT COALESCE(SUM(t.amount_cents), 0)
        FROM transactions t
        LEFT JOIN account_prefs p ON p.account_id = t.account_id
        WHERE t.pending = 0 AND t.amount_cents > 0
          AND t.date >= ? AND t.date < ?
          AND IFNULL(p.hidden, 0) = 0
          AND lower(IFNULL(t.category_name, '')) = ?
        """,
        (start, end, category.lower()),
    )


def spending_report(
    conn: sqlite3.Connection,
    *,
    period: str = "this_month",
    start: str | None = None,
    end: str | None = None,
    today: date | None = None,
) -> dict:
    """Payload for GET /api/spending. Spend is cash/cards, posted, non-transfer."""
    today = today or date.today()
    rng = period_ranges(period, start, end, today=today)
    a, b = rng["start"], rng["end_exclusive"]
    pa, pb = rng["prev_start"], rng["prev_end_exclusive"]

    cur_map = _spend_by_category_range(conn, a, b)
    prev_map = _spend_by_category_range(conn, pa, pb)
    names = set(cur_map) | set(prev_map)
    total = sum(cur_map.values())
    prev_total = sum(prev_map.values())

    by_category: list[dict] = []
    for name in names:
        cents = int(cur_map.get(name, 0) or 0)
        prev_cents = int(prev_map.get(name, 0) or 0)
        if cents == 0 and prev_cents == 0:
            continue
        pct = round(100.0 * cents / total, 1) if total else 0.0
        by_category.append({
            "category": name,
            "cents": cents,
            "pct": pct,
            "prev_cents": prev_cents,
            "change_pct": _change_pct(cents, prev_cents),
        })
    by_category.sort(
        key=lambda r: (
            1 if _is_uncategorized(r["category"]) else 0,
            -r["cents"],
            r["category"].lower(),
        )
    )

    months = _month_list_ending(today.strftime("%Y-%m"), 6)
    month_start = f"{months[0]}-01"
    month_end = _next_month(months[-1])
    month_map = {
        r["month"]: int(r["cents"] or 0)
        for r in conn.execute(
            f"""
            SELECT substr(t.date, 1, 7) AS month, SUM(t.amount_cents) AS cents
            FROM transactions t
            LEFT JOIN account_prefs p ON p.account_id = t.account_id
            WHERE {_page_spend_filter()} AND t.date >= ? AND t.date < ?
              AND IFNULL(p.hidden, 0) = 0
            GROUP BY 1
            """,
            (month_start, month_end),
        )
    }
    by_month = [{"month": m, "cents": int(month_map.get(m, 0) or 0)} for m in months]

    income_cents = _income_in_range(conn, a, b)
    prev_income_cents = _income_in_range(conn, pa, pb)
    bills_cents = int(cur_map.get(_BILLS_CAT, 0) or 0)
    prev_bills_cents = int(prev_map.get(_BILLS_CAT, 0) or 0)
    transfer_cents = _transfer_in_range(conn, a, b)
    ignored_cents = _ignored_in_range(conn, a, b)
    tax_cents = _tagged_outflow_in_range(conn, a, b, "Tax Deductible")
    reimb_cents = _tagged_outflow_in_range(conn, a, b, "Reimbursements")

    frequent = rows(conn.execute(
        f"""
        SELECT COALESCE(NULLIF(TRIM(t.payee), ''), 'Unknown') AS merchant,
               COUNT(*) AS n,
               CAST(ROUND(AVG(t.amount_cents)) AS INTEGER) AS avg_cents,
               SUM(t.amount_cents) AS cents
        FROM transactions t
        LEFT JOIN account_prefs p ON p.account_id = t.account_id
        WHERE {_page_spend_filter()} AND t.date >= ? AND t.date < ?
          AND IFNULL(p.hidden, 0) = 0
        GROUP BY 1
        ORDER BY n DESC, cents DESC
        LIMIT 8
        """,
        (a, b),
    ))
    largest = rows(conn.execute(
        f"""
        SELECT COALESCE(NULLIF(TRIM(t.payee), ''), 'Unknown') AS payee,
               t.date AS date,
               t.amount_cents AS amount_cents,
               COALESCE(NULLIF(TRIM(t.category_name), ''), 'Uncategorized') AS category
        FROM transactions t
        LEFT JOIN account_prefs p ON p.account_id = t.account_id
        WHERE {_page_spend_filter()} AND t.date >= ? AND t.date < ?
          AND IFNULL(p.hidden, 0) = 0
        ORDER BY t.amount_cents DESC, t.date DESC
        LIMIT 5
        """,
        (a, b),
    ))

    return {
        "period": rng["period"],
        "start": rng["start"],
        "end": rng["end"],
        "selected_month": rng["selected_month"],
        "total_spend_cents": total,
        "prev_total_cents": prev_total,
        "by_category": by_category,
        "by_month": by_month,
        "income_cents": income_cents,
        "prev_income_cents": prev_income_cents,
        "bills_cents": bills_cents,
        "prev_bills_cents": prev_bills_cents,
        "transfer_cents": transfer_cents,
        "ignored_cents": ignored_cents,
        "tax_deductible_cents": tax_cents,
        "reimbursements_cents": reimb_cents,
        "frequent": frequent,
        "largest": largest,
    }


def net_worth(conn: sqlite3.Connection) -> dict:
    """Sum non-closed, non-hidden balances. Credit cards are already negative (owed)."""
    accts = list_accounts(conn, include_closed=False)
    assets = 0
    liabilities = 0
    for a in accts:
        if a.get("hidden"):
            continue
        cents = a["current_cents"]
        if cents is None:
            continue
        if cents >= 0:
            assets += cents
        else:
            liabilities += -cents
    for p in list_properties(conn):
        assets += int(p.get("value_cents") or 0)
    return {
        "assets_cents": assets,
        "liabilities_cents": liabilities,
        "net_worth_cents": assets - liabilities,
    }


def _nw_owner_keys(conn: sqlite3.Connection) -> tuple[str, list[str]]:
    hh = household(conn)
    self_name = (hh["self"] or {}).get("name") or ""
    keys: list[str] = []
    if self_name:
        keys.append(self_name)
    if hh.get("partner"):
        keys.append(hh["partner"]["name"])
        keys.append("Joint")
    return self_name, keys


def net_worth_history(conn: sqlite3.Connection) -> dict:
    """Walk cash/card balances backward; hold investments at today's mark.

    Same account set as Retirement Net worth (included, open, no Savings).
    SimpleFIN has no historical market prices. Brokerage buy/sell notional
    is not a balance change (cash out, shares in), so applying those txns
    on top of today's value wildly overstates older points.
    """
    self_name, owner_keys = _nw_owner_keys(conn)
    accts = [
        a for a in list_accounts(conn, include_closed=False)
        if not a.get("hidden") and (a.get("category") or "") != "Savings"
    ]
    props = list(list_properties(conn))
    empty = {"dates": [], "household": [], **{k: [] for k in owner_keys}}
    if not accts and not props:
        return empty
    ids = [a["account_id"] for a in accts]
    min_raw = None
    if ids:
        ph = ",".join("?" * len(ids))
        min_row = conn.execute(
            f"SELECT MIN(date) FROM transactions WHERE account_id IN ({ph}) AND pending = 0",
            ids,
        ).fetchone()
        min_raw = min_row[0] if min_row else None
    today = date.today()
    if not min_raw:
        cur = {k: 0 for k in owner_keys}
        hh = 0
        for a in accts:
            c = a.get("current_cents") or 0
            hh += c
            o = a.get("owner") or self_name
            if o in cur:
                cur[o] += c
        for p in props:
            c = int(p.get("value_cents") or 0)
            hh += c
            o = p.get("owner") or self_name
            if o in cur:
                cur[o] += c
        return {
            "dates": [today.isoformat()],
            "household": [hh],
            **{k: [cur.get(k, 0)] for k in owner_keys},
        }
    start = date.fromisoformat(str(min_raw)[:10])
    points = []
    d = today
    while d > start and len(points) < 14:
        points.append(d)
        d -= timedelta(days=7)
    points.append(start)
    points = sorted(set(points))
    txns = conn.execute(
        f"""
        SELECT account_id, date, amount_cents
        FROM transactions
        WHERE account_id IN ({ph}) AND pending = 0
        """,
        ids,
    ).fetchall()
    by_acct: dict[str, list[tuple[str, int]]] = {i: [] for i in ids}
    for r in txns:
        by_acct[r["account_id"]].append((str(r["date"])[:10], int(r["amount_cents"] or 0)))
    series = {"household": [], **{k: [] for k in owner_keys}}
    dates = []
    for pt in points:
        ts = pt.isoformat()
        hh = 0
        own = {k: 0 for k in owner_keys}
        for a in accts:
            bal = int(a.get("current_cents") or 0)
            aid = a.get("account_id") or ""
            hold = (
                aid.startswith("manual:")
                or a.get("type") == "manual_loan"
                or account_is_investment_spend(a.get("name"), a.get("group_key"))
            )
            if not hold:
                extra = sum(amt for dt, amt in by_acct[a["account_id"]] if dt > ts)
                bal += extra
            hh += bal
            o = a.get("owner") or self_name
            if o in own:
                own[o] += bal
        for p in props:
            c = int(p.get("value_cents") or 0)
            hh += c
            o = p.get("owner") or self_name
            if o in own:
                own[o] += c
        dates.append(ts)
        series["household"].append(hh)
        for o in own:
            series[o].append(own[o])
    return {"dates": dates, **series}


# ----- recurring / budgets / syncs ------------------------------------------

def upsert_recurring(conn: sqlite3.Connection, rec: dict, *, keep_subscription: bool = True) -> None:
    existing = conn.execute(
        "SELECT is_subscription FROM recurring WHERE stream_id = ?",
        (rec["stream_id"],),
    ).fetchone()
    if existing is not None and keep_subscription:
        rec = dict(rec)
        rec["is_subscription"] = existing["is_subscription"]
    conn.execute(
        """
        INSERT INTO recurring (stream_id, source, merchant, average_cents, frequency,
                               is_subscription, category, next_date, status, last_seen_ms)
        VALUES (:stream_id, :source, :merchant, :average_cents, :frequency,
                :is_subscription, :category, :next_date, :status, :last_seen_ms)
        ON CONFLICT(stream_id) DO UPDATE SET
            source = excluded.source,
            merchant = excluded.merchant,
            average_cents = excluded.average_cents,
            frequency = excluded.frequency,
            is_subscription = excluded.is_subscription,
            category = excluded.category,
            next_date = excluded.next_date,
            status = excluded.status,
            last_seen_ms = excluded.last_seen_ms
        """,
        rec,
    )


_IGNORED_REC_META = "ignored_recurring"


def ignored_recurring_keys(conn: sqlite3.Connection) -> set[str]:
    raw = get_meta(conn, _IGNORED_REC_META) or "[]"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return set()
    if not isinstance(data, list):
        return set()
    return {str(x).strip().lower() for x in data if x}


def is_ignored_recurring(
    conn: sqlite3.Connection,
    stream_id: str | None = None,
    merchant: str | None = None,
) -> bool:
    ign = ignored_recurring_keys(conn)
    if stream_id and stream_id.strip().lower() in ign:
        return True
    if merchant and merchant.strip().lower() in ign:
        return True
    return False


def ignore_recurring(conn: sqlite3.Connection, stream_id: str) -> dict:
    sid = (stream_id or "").strip()
    if not sid:
        raise ValueError("stream_id is required")
    row = conn.execute(
        "SELECT stream_id, merchant FROM recurring WHERE stream_id = ?",
        (sid,),
    ).fetchone()
    if row is None:
        raise KeyError("recurring stream not found")
    ign = ignored_recurring_keys(conn)
    ign.add(sid.lower())
    merch = (row["merchant"] or "").strip().lower()
    if merch:
        ign.add(merch)
    set_meta(conn, _IGNORED_REC_META, json.dumps(sorted(ign)))
    delete_recurring(conn, sid)
    return {"ok": True, "stream_id": sid}


def list_recurring(conn: sqlite3.Connection) -> list[dict]:
    ign = ignored_recurring_keys(conn)
    out = []
    for r in rows(conn.execute(
        "SELECT * FROM recurring ORDER BY is_subscription DESC, average_cents DESC"
    )):
        sid = (r.get("stream_id") or "").strip().lower()
        merch = (r.get("merchant") or "").strip().lower()
        if sid in ign or merch in ign:
            continue
        out.append(r)
    return out


def _subscription_pref(conn: sqlite3.Connection, stream_id: str) -> int | None:
    row = conn.execute(
        "SELECT is_subscription FROM recurring_prefs WHERE stream_id = ?",
        (stream_id,),
    ).fetchone()
    if row is None:
        return None
    return 1 if row["is_subscription"] else 0


def set_subscription(conn: sqlite3.Connection, stream_id: str, is_subscription: bool) -> None:
    flag = 1 if is_subscription else 0
    conn.execute(
        "UPDATE recurring SET is_subscription = ? WHERE stream_id = ?",
        (flag, stream_id),
    )
    conn.execute(
        """
        INSERT INTO recurring_prefs (stream_id, is_subscription)
        VALUES (?, ?)
        ON CONFLICT(stream_id) DO UPDATE SET
            is_subscription = excluded.is_subscription
        """,
        (stream_id, flag),
    )


def replace_local_recurring(conn: sqlite3.Connection, recs: list[dict]) -> None:
    detected: set[str] = set()
    for rec in recs:
        sid = rec.get("stream_id")
        if is_ignored_recurring(conn, sid, rec.get("merchant")):
            continue
        upsert_recurring(conn, rec, keep_subscription=True)
        if sid:
            pref = _subscription_pref(conn, sid)
            if pref is not None:
                conn.execute(
                    "UPDATE recurring SET is_subscription = ? WHERE stream_id = ?",
                    (pref, sid),
                )
            detected.add(sid)
    for row in conn.execute(
        "SELECT stream_id FROM recurring WHERE stream_id LIKE 'local:%'"
    ).fetchall():
        sid = row["stream_id"]
        if sid in detected:
            continue
        pref = _subscription_pref(conn, sid)
        if pref is None:
            conn.execute("DELETE FROM recurring WHERE stream_id = ?", (sid,))
        else:
            conn.execute(
                "UPDATE recurring SET is_subscription = ? WHERE stream_id = ?",
                (pref, sid),
            )


def delete_recurring(conn: sqlite3.Connection, stream_id: str) -> None:
    conn.execute("DELETE FROM recurring WHERE stream_id = ?", (stream_id,))


def replace_budget_month(conn: sqlite3.Connection, month: str, cats: list[dict]) -> None:
    conn.execute("DELETE FROM budget_categories WHERE month = ?", (month,))
    for rec in cats:
        conn.execute(
            """
            INSERT INTO budget_categories (
                category_id, month, name, group_name, hidden,
                budgeted_cents, activity_cents, remaining_cents
            ) VALUES (
                :category_id, :month, :name, :group_name, :hidden,
                :budgeted_cents, :activity_cents, :remaining_cents
            )
            """,
            rec,
        )


def list_budget_month(conn: sqlite3.Connection, month: str) -> list[dict]:
    return rows(conn.execute(
        """
        SELECT * FROM budget_categories
        WHERE month = ? AND hidden = 0
          AND IFNULL(group_name, '') NOT IN ('Internal Master Category', 'Hidden Categories')
          AND IFNULL(name, '') NOT LIKE 'Inflow:%'
        ORDER BY group_name, name
        """,
        (month,),
    ))


def insert_sync(conn: sqlite3.Connection, started_at_ms: int) -> int:
    cur = conn.execute(
        "INSERT INTO syncs (started_at_ms, ok) VALUES (?, 0)",
        (started_at_ms,),
    )
    return int(cur.lastrowid)


def finish_sync(
    conn: sqlite3.Connection,
    sync_id: int,
    *,
    n_added: int,
    n_modified: int,
    n_removed: int,
    ok: bool,
    error: str | None,
    server_knowledge: int | None = None,
) -> None:
    cov = coverage(conn)
    conn.execute(
        """
        UPDATE syncs SET finished_at_ms = ?, n_added = ?, n_modified = ?, n_removed = ?,
                         ok = ?, error = ?, min_date = ?, max_date = ?, server_knowledge = ?
        WHERE id = ?
        """,
        (
            now_ms(), n_added, n_modified, n_removed, 1 if ok else 0, error,
            cov["min_date"], cov["max_date"], server_knowledge, sync_id,
        ),
    )


def last_sync(conn: sqlite3.Connection) -> dict | None:
    return row_dict(conn.execute(
        "SELECT * FROM syncs ORDER BY id DESC LIMIT 1"
    ).fetchone())
